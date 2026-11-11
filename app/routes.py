from __future__ import annotations

import asyncio
import colorsys
import json
import math
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.auth import (
    LOGIN_CSRF_COOKIE, LOGIN_CSRF_MAX_AGE, SESSION_COOKIE, cookie_samesite,
    cookies_secure, family_password, login_allowed, make_login_csrf, make_session,
    new_csrf_token, password_matches, read_login_csrf, record_login_failure, verify_csrf,
    CONTEXT_COOKIE, make_context_cookie, read_context_cookie, SESSION_MAX_AGE,
)
from app.database import get_session_factory
from app.models import Account, AppSetting, BudgetAdjustment, Transaction
from app.repository import (
    ConcurrentEditError, delete_transaction, backup_payload, cashflow_statuses, csv_export,
    insert_transaction, latest_activity_id, latest_family_activity, legacy_statuses, read_snapshot, save_accounts,
    reallocate_future_reserve, record_activity_event, save_cards, save_cashflow_status, save_lookup, save_settings,
    stop_transaction_from_month,
    set_month_closed, update_transaction,
)
from app.services.finance_core import (
    FAMILY, apply_payment_statuses, as_date, card_limits, cashflow_items, cashflow_summary,
    confirmed_pot_usage, daily_activity, month_from_id, month_id, month_key, month_label, month_start,
    opening_account_balances, pot_monthly_status, projection, status_natural_key,
)
from app.services.charts import (
    build_category_chart, build_daily_cash_chart, build_line_chart, build_stacked_bar_chart,
    build_waterfall_chart, classify_pot, group_pots,
)
from app.services.visual_analytics import category_comparison, waterfall_steps
from app.services.fgts_projection import project_fgts
from app.services.finance_insights import suggest_reallocation
from app.services.assistant_agent import AIAssistantError, MAX_QUESTION_CHARS, ask_finance_agent, ask_finance_proposals, assistant_agent_available, sanitize_preferences
from app.services.finance_ai_data import build_financial_summary
from app.services.horizon import effective_end
from app.services.daily_review import (
    annotate_review_actions, build_daily_review_context, build_return_url,
    normalize_month as normalize_review_month, normalize_return_to,
    status_change_block_reason, validate_item_confirmation, validate_paid_on,
)
from app.services.entry_review import probable_duplicates, sign_review, read_review, values_digest
from app.services.assets import asset_url
from app.services.card_billing import (
    card_billing_context, cards_with_closing_days, first_bill_month_for_form,
    validate_card_closing_days_form,
)
from app.services.family_planning import installment_number_for_month
from app.services.member_balances import member_bank_series, member_bank_row, member_bank_rows_from
from app.services.reserve_activity import daily_reserve_summary, reserve_usage_details

ROOT = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(ROOT / "app" / "templates"))
router = APIRouter()
PERSONS = (FAMILY, "Hener", "Ju")
TRANSACTION_TYPES = ("Receita", "Fixo", "Débito/Pix", "Crédito à vista", "Parcelado", "Reserva", "Caixinha", "Saque Caixinha", "Transferência")
CARD_TYPES = {"Crédito à vista", "Parcelado"}
RECURRING_TYPES = {"Receita", "Fixo", "Reserva", "Caixinha", "Saque Caixinha", "Transferência"}


def brl(value: Any) -> str:
    try:
        amount = float(value or 0)
    except (ValueError, TypeError):
        amount = 0.0
    sign = "-" if amount < 0 else ""
    return sign + "R$ " + f"{abs(amount):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _movement_visual(row: dict[str, Any]) -> dict[str, str]:
    category = str(row.get("category") or "").casefold()
    description = str(row.get("description") or "").casefold()
    kind = str(row.get("type") or "")
    if kind == "Receita" or kind == "Saque Caixinha":
        impact_symbol, impact_label, impact_tone = "+", "Entrada", "income"
    elif kind == "Transferência":
        impact_symbol, impact_label, impact_tone = "↔", "Transferência interna", "transfer"
    elif kind == "Reserva":
        impact_symbol, impact_label, impact_tone = "·", "Verba reservada; não é gasto confirmado", "neutral"
    else:
        impact_symbol, impact_label, impact_tone = "−", "Saída", "expense"
    if kind == "Receita":
        return {"icon": "+", "tone": "income", "label": "Receita", "impact_symbol": impact_symbol, "impact_label": impact_label, "impact_tone": impact_tone}
    if kind == "Transferência":
        return {"icon": "↔", "tone": "transfer", "label": "Transferência", "impact_symbol": impact_symbol, "impact_label": impact_label, "impact_tone": impact_tone}
    if kind == "Reserva":
        return {"icon": "▣", "tone": "neutral", "label": "Verba reservada", "impact_symbol": impact_symbol, "impact_label": impact_label, "impact_tone": impact_tone}
    if kind in {"Caixinha", "Saque Caixinha"}:
        return {"icon": "▣", "tone": "saving", "label": "Reserva ou caixinha", "impact_symbol": impact_symbol, "impact_label": impact_label, "impact_tone": impact_tone}
    if kind in {"Crédito à vista", "Parcelado"} or row.get("card_flag"):
        return {"icon": "▤", "tone": "credit", "label": "Compra no cartão", "impact_symbol": impact_symbol, "impact_label": impact_label, "impact_tone": impact_tone}
    groups = (
        (("moradia", "casa", "aluguel", "condomínio", "condominio", "imóvel", "imovel"), "⌂", "housing", "Moradia"),
        (("alimentação", "alimentacao", "mercado", "restaurante", "padaria", "comida"), "◉", "food", "Alimentação"),
        (("transporte", "gasolina", "combustível", "combustivel", "uber", "carro"), "↗", "transport", "Transporte"),
        (("lazer", "viagem", "hobby", "entretenimento", "cinema"), "✦", "fun", "Lazer"),
        (("saúde", "saude", "farmácia", "farmacia", "médico", "medico"), "♥", "health", "Saúde"),
        (("vestuário", "vestuario", "roupa", "compras", "shopping"), "▤", "shopping", "Compras"),
    )
    for terms, icon, tone, label in groups:
        if any(term in category or term in description for term in terms):
            return {"icon": icon, "tone": tone, "label": label, "impact_symbol": impact_symbol, "impact_label": impact_label, "impact_tone": impact_tone}
    return {"icon": "•", "tone": "neutral", "label": category.title() if category else "Sem categoria", "impact_symbol": impact_symbol, "impact_label": impact_label, "impact_tone": impact_tone}


def _group_category_suggestions(categories) -> list[dict[str, Any]]:
    groups = {
        "Transporte": ("transporte", "gasolina", "combust", "auto", "carro", "uber", "estacionamento"),
        "Casa e contas": ("casa", "moradia", "aluguel", "condom", "luz", "energia", "água", "agua", "internet", "telefone", "gás", "gas"),
        "Alimentação": ("alimenta", "mercado", "supermerc", "padaria", "restaurante", "comida"),
        "Saúde": ("saúde", "saude", "farmácia", "farmacia", "médico", "medico", "remédio", "remedio", "consulta"),
        "Família e educação": ("família", "familia", "filho", "escola", "educa", "criança", "crianca"),
        "Lazer e compras": ("lazer", "viagem", "presente", "roupa", "vestuário", "vestuario", "luxo", "passeio", "cinema", "hobby", "entretenimento", "shopping"),
        "Renda e transferências": ("salário", "salario", "renda", "reembolso", "transfer"),
        "Outras categorias": (),
    }
    result = {name: [] for name in groups}
    for category in sorted({str(value).strip() for value in categories or [] if str(value).strip()}, key=str.casefold):
        normalized = category.casefold()
        target = next((name for name, terms in groups.items() if terms and any(term in normalized for term in terms)), "Outras categorias")
        result[target].append(category)
    return [{"name": name, "categories": values} for name, values in result.items() if values]


def parse_amount(value: Any) -> float:
    text = str(value or "").strip().replace("R$", "").replace(" ", "")
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    return float(text)


def display_date(value: Any) -> str:
    day = as_date(value)
    return day.strftime("%d/%m/%Y") if day else "—"


templates.env.filters["brl"] = brl
templates.env.filters["day"] = display_date
templates.env.filters["month_label"] = month_label
templates.env.globals["asset_url"] = asset_url


def _nav_context(request: Request, form=None) -> dict[str, str]:
    form = form or {}
    query = request.query_params
    saved = read_context_cookie(request.cookies.get(CONTEXT_COOKIE))
    raw = form.get("_context_month") or query.get("month") or saved.get("month") or date.today().strftime("%Y-%m")
    try:
        selected = month_key(raw)
    except (ValueError, TypeError):
        selected = date.today().strftime("%Y-%m")
    person = str(form.get("_context_person") or query.get("person") or query.get("member") or saved.get("person") or FAMILY)
    if person not in PERSONS:
        person = FAMILY
    horizon = str(form.get("_context_horizon") or query.get("horizon") or saved.get("horizon") or "12")
    return {"month": selected, "person": person, "horizon": "all" if horizon == "all" else "12"}


def _return_url(request: Request, path: str, form=None, **filters) -> str:
    params = _nav_context(request, form)
    params.update({key: str(value) for key, value in filters.items() if value})
    return f"{path}?{urlencode(params)}"


def _transaction_return_target(source) -> tuple[str, dict[str, str]]:
    source = source or {}
    page = str(source.get("return_page", "")).strip()
    if page == "cards":
        card = str(source.get("return_card", "")).strip()
        if not card:
            return "/transactions", {}
        month = str(source.get("return_month", "")).strip()
        return "/cards/calendar", ({"card": card, "month": month} if month else {"card": card})
    if page == "today":
        return "/today", {}
    if page == "open-finance":
        return "/open-finance", {}
    if page == "cash":
        return "/today", {}
    if page == "transactions":
        month = str(source.get("return_month", "")).strip()
        return "/transactions", ({"movement_month": month} if month else {})
    return "/transactions", {}


def family_sync_state() -> dict[str, str]:
    """Return a minimal shared-update marker; never infer who or which device changed it."""
    try:
        with get_session_factory()() as db:
            events = latest_family_activity(db, 1)
        if not events:
            return {"revision": "0", "updated_at": "", "label": "Histórico ativo a partir desta versão."}
        event = events[0]
        updated = datetime.fromisoformat(str(event.created_at).replace("Z", "+00:00"))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        local = updated.astimezone(ZoneInfo("America/Sao_Paulo"))
        return {
            "revision": str(event.id),
            "updated_at": local.isoformat(),
            "label": f"Última atualização {local.strftime('%d/%m %H:%M')}",
        }
    except Exception:
        return {"revision": "", "updated_at": "", "label": "Sincronização indisponível no momento."}


def render(request: Request, name: str, title: str, **extra: Any):
    session = getattr(request.state, "family_session", None) or {}
    nav = _nav_context(request)
    context = {
        "request": request, "title": title, "csrf": session.get("csrf", ""),
        "month_label": month_label, "today": date.today().isoformat(),
        "active_path": request.url.path, "nav_context": nav, "nav_query": urlencode(nav), "nav_people": PERSONS,
        "family_sync": bool(session), "family_sync_info": family_sync_state() if session else {},
    }
    context.update(extra)
    response = templates.TemplateResponse(request=request, name=name, context=context)
    if session:
        response.set_cookie(CONTEXT_COOKIE, make_context_cookie(context["nav_context"]),
                            max_age=SESSION_MAX_AGE, httponly=True, secure=cookies_secure(),
                            samesite=cookie_samesite(), path="/")
    return response


async def checked_form(request: Request):
    form = await request.form()
    session = getattr(request.state, "family_session", None) or {}
    if not verify_csrf(session.get("csrf"), str(form.get("_csrf", ""))):
        raise HTTPException(status_code=403, detail="Formulário expirado. Atualize a página e tente novamente.")
    return form


def _login_page(request: Request, error: str = "", status_code: int = 200):
    if not family_password():
        response = render(request, "login.html", "Acesso familiar", error="O acesso ainda não foi configurado pelo responsável.", login_csrf="")
        response.status_code = 503
        return response
    token = new_csrf_token()
    response = render(request, "login.html", "Acesso familiar", error=error, login_csrf=token)
    response.status_code = status_code
    response.set_cookie(
        LOGIN_CSRF_COOKIE, make_login_csrf(token), max_age=LOGIN_CSRF_MAX_AGE,
        httponly=True, secure=cookies_secure(), samesite=cookie_samesite(), path="/",
    )
    return response


def _snapshot():
    with get_session_factory()() as db:
        return read_snapshot(db)


def _selected_month(request: Request, settings: dict[str, Any]) -> date:
    value = _nav_context(request)["month"]
    try:
        return month_start(value) or date.today().replace(day=1)
    except ValueError:
        return date.today().replace(day=1)


def _person(request: Request) -> str:
    return _nav_context(request)["person"]


def _chart_toggle_url(request: Request, path: str, show_full: bool) -> str:
    params = {key: value for key, value in request.query_params.items() if key != "horizon"}
    params["horizon"] = "12" if show_full else "all"
    return f"{path}?{urlencode(params)}"


def _forecast(txs, accounts, settings, closed, person=FAMILY, scenarios=None, requested=None):
    return projection(
        txs, accounts, settings.get("base_month", "2026-08-01"),
        effective_end(txs, settings, requested=requested, scenarios=scenarios), person,
        closed_months=closed,
        minimum_balance=float(settings.get("minimum_balance") or 5000),
        scenarios=scenarios or [],
    )


def _row_for(rows, month):
    wanted = month_key(month)
    return next((row for row in rows if month_key(row["month"]) == wanted), None)


def _future_rows(rows, month):
    idx = month_id(month)
    if idx is None:
        return []
    return [row for row in rows if (month_idx := month_id(row["month"])) is not None and month_idx >= idx]


def _month_is_at_least(row, month_idx: int) -> bool:
    row_idx = month_id(row.get("month"))
    return row_idx is not None and row_idx >= month_idx


def _cashbox_accumulation(txs, accounts, cards, settings, closed, forecast_rows, person=FAMILY, selected_month=None):
    """Build confirmed and projected cashbox balances, overall and by category."""
    start = month_start(settings.get("base_month", "2026-08-01")) or date(2026, 8, 1)
    end = month_start(forecast_rows[-1]["month"]) if forecast_rows else effective_end(txs, settings)
    actual_month = date.today().replace(day=1)
    current = min(month_start(selected_month) or actual_month, actual_month)
    start_idx, current_idx, end_idx = month_id(start), month_id(current), month_id(end)
    if start_idx is None or current_idx is None or end_idx is None or end_idx < current_idx:
        return {"confirmed": 0.0, "terminal": 0.0, "rows": [], "integrity_ok": True}

    def cents(value: Any) -> int:
        return int((Decimal(str(value or 0)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    confirmed_by_category: dict[str, int] = {}
    paid_current_by_category: dict[str, int] = {}
    transaction_by_id = {int(tx["id"]): tx for tx in txs if tx.get("id") is not None}
    with get_session_factory()() as db:
        for index in range(start_idx, min(current_idx, end_idx) + 1):
            month = month_from_id(index)
            if month is None:
                continue
            items = apply_payment_statuses(
                cashflow_items(txs, cards, accounts, month, person),
                legacy_statuses(db, month), cashflow_statuses(db, month),
            )
            for item in items:
                if not item.get("paid") or item.get("type") not in ("Caixinha", "Saque Caixinha"):
                    continue
                tx = transaction_by_id.get(int(item["transaction_id"])) if item.get("transaction_id") is not None else None
                category = str((tx or {}).get("category") or "Caixinha sem categoria")
                signed_cents = cents(item.get("amount")) * (1 if item["type"] == "Caixinha" else -1)
                confirmed_by_category[category] = confirmed_by_category.get(category, 0) + signed_cents
                if index == current_idx:
                    paid_current_by_category[category] = paid_current_by_category.get(category, 0) + signed_cents

    rows_by_month = {month_id(row["month"]): row for row in forecast_rows if month_id(row["month"]) is not None}
    current_projection = rows_by_month.get(current_idx, {})
    confirmed_cents = sum(confirmed_by_category.values())
    paid_current_cents = sum(paid_current_by_category.values())
    current_by_category = current_projection.get("savings_by_category") or {}
    current_by_category_cents = {str(name): cents(value) for name, value in current_by_category.items()}
    integrity_ok = sum(current_by_category_cents.values()) == cents(current_projection.get("savings_net", 0))
    projected_by_category_cents = dict(confirmed_by_category)
    for category, amount_cents in current_by_category_cents.items():
        projected_by_category_cents[category] = projected_by_category_cents.get(category, 0) + amount_cents
    for category, amount_cents in paid_current_by_category.items():
        projected_by_category_cents[category] = projected_by_category_cents.get(category, 0) - amount_cents
    projected_cents = confirmed_cents + cents(current_projection.get("savings_net", 0)) - paid_current_cents
    if sum(projected_by_category_cents.values()) != projected_cents:
        integrity_ok = False
    current_date = month_from_id(current_idx)
    result = [{
        "month": current_date, "confirmed": confirmed_cents / 100, "projected": projected_cents / 100,
        "confirmed_by_category": {name: amount / 100 for name, amount in confirmed_by_category.items()},
        "projected_by_category": {name: amount / 100 for name, amount in projected_by_category_cents.items()},
        "closed": bool(closed.get(month_key(current_date))) if current_date else False,
        "status": "Confirmado e previsão do mês",
    }] if current_date else []
    for index in range(current_idx + 1, end_idx + 1):
        month = month_from_id(index)
        forecast = rows_by_month.get(index)
        if month is None or forecast is None:
            continue
        monthly_by_category = forecast.get("savings_by_category") or {}
        monthly_by_category_cents = {str(name): cents(value) for name, value in monthly_by_category.items()}
        if sum(monthly_by_category_cents.values()) != cents(forecast.get("savings_net", 0)):
            integrity_ok = False
        for category, amount_cents in monthly_by_category_cents.items():
            projected_by_category_cents[category] = projected_by_category_cents.get(category, 0) + amount_cents
        projected_cents += cents(forecast.get("savings_net", 0))
        if sum(projected_by_category_cents.values()) != projected_cents:
            integrity_ok = False
        result.append({"month": month, "confirmed": None, "projected": projected_cents / 100,
                       "confirmed_by_category": {},
                       "projected_by_category": {name: amount / 100 for name, amount in projected_by_category_cents.items()},
                       "closed": bool(forecast.get("closed")), "status": "Previsto"})
    return {
        "confirmed": confirmed_cents / 100,
        "terminal": result[-1]["projected"] if result else confirmed_cents / 100,
        "rows": result,
        "current_month": current,
        "base_month": start,
        "integrity_ok": integrity_ok,
    }


@router.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/dashboard", status_code=303)


@router.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    return _login_page(request)


@router.post("/login", response_class=HTMLResponse)
async def login_post(request: Request):
    form = await request.form()
    supplied_csrf = str(form.get("_csrf", ""))
    cookie_csrf = read_login_csrf(request.cookies.get(LOGIN_CSRF_COOKIE))
    if not verify_csrf(cookie_csrf, supplied_csrf):
        return _login_page(request, "Sessão de acesso expirada. Tente novamente.", 403)
    if not family_password():
        return _login_page(request, "O acesso ainda não foi configurado.", 503)
    client_key = request.client.host if request.client else "unknown"
    if not login_allowed(client_key):
        return _login_page(request, "Muitas tentativas. Aguarde 15 minutos antes de tentar novamente.", 429)
    if not password_matches(str(form.get("password", ""))):
        record_login_failure(client_key)
        return _login_page(request, "Senha incorreta. Tente novamente.", 401)
    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie(
        SESSION_COOKIE, make_session(new_csrf_token()), max_age=12 * 60 * 60,
        httponly=True, secure=cookies_secure(), samesite=cookie_samesite(), path="/",
    )
    response.delete_cookie(LOGIN_CSRF_COOKIE, path="/", secure=cookies_secure(), samesite=cookie_samesite())
    return response


@router.post("/logout")
async def logout(request: Request):
    await checked_form(request)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/", secure=cookies_secure(), httponly=True, samesite=cookie_samesite())
    response.delete_cookie(CONTEXT_COOKIE, path="/", secure=cookies_secure(), httponly=True, samesite=cookie_samesite())
    return response


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    txs, accounts, cards, lookups, settings, closed = _snapshot()
    month = _selected_month(request, settings)
    person = _person(request)
    rows = _forecast(txs, accounts, settings, closed, person, requested=month)
    current = _row_for(rows, month) or (rows[0] if rows else None)
    if current is None:
        return render(request, "empty.html", "Visão geral", message="Ainda não há lançamentos migrados para esta família.")
    pots = pot_monthly_status(txs, month, person, settings.get("pot_order") or [])
    previous_idx = (month_id(month) or 0) - 1
    previous_month = month_from_id(previous_idx) if previous_idx > 0 else None
    with get_session_factory()() as db:
        statuses = cashflow_statuses(db, month)
        legacy_rows = legacy_statuses(db, month)
        current_cash_items = apply_payment_statuses(
            cashflow_items(txs, cards, accounts, month, person),
            legacy_rows, statuses,
        )
        previous_cash_items = []
        if previous_month is not None:
            previous_cash_items = apply_payment_statuses(
                cashflow_items(txs, cards, accounts, previous_month, person),
                legacy_statuses(db, previous_month), cashflow_statuses(db, previous_month),
            )
    pending = [item for item in current_cash_items if not item["paid"]]
    waterfall_data = waterfall_steps({**current, "person": person}, txs, current_cash_items)
    waterfall_chart = build_waterfall_chart(
        waterfall_data, "dashboard-waterfall",
        f"Cascata do saldo de {month_label(month)}: saldo anterior, receitas, despesas por categoria e saldo final",
    )
    category_rows = category_comparison(txs, month, person, current_cash_items, previous_cash_items)
    category_chart = build_category_chart(
        category_rows, "dashboard-category-spend",
        f"Categorias de despesas em {month_label(month)}: previsto, confirmado e mês anterior",
    )
    future = _future_rows(rows, month)
    show_full_charts = _nav_context(request)["horizon"] == "all"
    chart_rows = future if show_full_charts else future[:12]
    personal_bank = _personal_bank_context(
        txs, accounts, cards, settings, closed, person, month,
        rows[-1]["month"], show_full_charts, "dashboard-member-bank",
    )
    first_below_floor = next((row for row in future if row["gap_to_minimum"] < 0), None)
    cards_status = card_limits(txs, cards, date.today() if month == date.today().replace(day=1) else month)
    attention = {
        "below_floor": current["gap_to_minimum"] < 0,
        "first_below_floor": first_below_floor,
        "exceeded_pots": [row for row in pots["rows"] if row["available"] < -0.005],
        "pending_count": len(pending),
        "pending_total": sum(float(item["amount"]) for item in pending),
    }
    monthly_chart = build_line_chart(
        chart_rows,
        [
            ("projected_ending_balance", "Saldo bancário projetado", "#123b63", False),
            ("minimum_balance", "Piso de segurança", "#a9433d", True),
        ],
        "dashboard-monthly", "Saldo bancário projetado e piso de segurança", max_x_labels=5,
    )
    invoice_chart = build_line_chart(
        chart_rows,
        [("card_total", "Fatura mensal", "#4e91c1", False)],
        "dashboard-invoices", "Fatura de cartão por competência", max_x_labels=5,
    )
    cashboxes = _cashbox_accumulation(txs, accounts, cards, settings, closed, rows, person, month)
    cashbox_future = _future_rows(cashboxes["rows"], month)
    cashbox_chart_rows = cashbox_future if show_full_charts else cashbox_future[:12]
    cashboxes["visible_end"] = cashbox_chart_rows[-1] if cashbox_chart_rows else None
    cashbox_chart = build_line_chart(
        cashbox_chart_rows,
        [
            ("confirmed", "Acumulado confirmado", "#123b63", False),
            ("projected", "Saldo estimado ao fim do mês", "#79a7d0", True),
        ],
        "cashbox-accumulated", "Caixinhas: saldo confirmado e evolução estimada", max_x_labels=5,
    )
    return render(request, "dashboard.html", "Visão geral", txs=txs, month=month_key(month), person=person,
                  current=current, future=future if show_full_charts else future[:12], cards_status=cards_status, attention=attention,
                  pots=pots, accounts=accounts, people=PERSONS, monthly_chart=monthly_chart, invoice_chart=invoice_chart,
                  waterfall_steps=waterfall_data, waterfall_chart=waterfall_chart, category_rows=category_rows,
                  category_chart=category_chart,
                  cashboxes=cashboxes, cashbox_chart=cashbox_chart, chart_horizon="all" if show_full_charts else "12",
                  chart_toggle_url=_chart_toggle_url(request, "/dashboard", show_full_charts), **personal_bank)


@router.get("/today", response_class=HTMLResponse)
def today_page(request: Request):
    person = _person(request)
    with get_session_factory()() as db:
        txs, accounts, cards, lookups, settings, closed = read_snapshot(db)
        month = _selected_month(request, settings)
        from calendar import monthrange
        start = month
        end = date(month.year, month.month, monthrange(month.year, month.month)[1])
        all_events = daily_activity(txs, start, end, person)
        event_banks = sorted({event["account_card"] for event in all_events})
        query, bank = request.query_params.get("q", "").strip(), request.query_params.get("bank", "").strip()
        status_filter = request.query_params.get("status", "all")
        if status_filter not in ("all", "confirmed", "pending", "predicted"):
            status_filter = "all"
        events = [event for event in all_events if (not query or query.casefold() in (event["description"] + " " + event["category"]).casefold())
                  and (not bank or event["account_card"] == bank)]
        statuses = cashflow_statuses(db, month)
        legacy_rows = legacy_statuses(db, month)
        review_context = build_daily_review_context(
            (txs, accounts, cards, lookups, settings, closed), month, person,
            statuses=statuses, legacy_rows=legacy_rows,
            query=query, bank=bank, review_status=status_filter,
        )
        review_context = annotate_review_actions(
            review_context, month, bool(closed.get(month_key(month), False)),
        )
        family_account_items = apply_payment_statuses(
            cashflow_items(txs, cards, accounts, month, FAMILY), legacy_rows, statuses,
        )
        opening = opening_account_balances(txs, accounts, settings.get("base_month"), month, cards=cards)
        summary = cashflow_summary(
            review_context["review_all_items"], accounts, opening, person,
            account_items=family_account_items,
        )
        account_names = {row["account"] for row in summary["account_balances"]}
        balance_items = [item for item in family_account_items if item.get("account") in account_names]
        daily_chart = build_daily_cash_chart(balance_items, summary["opening_total"], month)["chart"]
    review_banks = sorted({str(item.get("account") or "") for item in review_context["review_all_items"] if item.get("account")}, key=str.casefold)
    banks = sorted(set(event_banks) | set(review_banks), key=str.casefold)
    render_context = dict(review_context)
    render_context.update({
        "events": events, "start": start.isoformat(), "end": end.isoformat(),
        "month": month_key(month), "person": person, "people": PERSONS,
        "lookups": lookups, "q": query, "bank": bank, "status_filter": status_filter,
        "banks": banks, "review_banks": review_banks,
        "closed": bool(closed.get(month_key(month), False)),
        "bank_balances": summary["account_balances"], "summary": summary,
        "daily_chart": daily_chart, "month_name": month_label(month),
        "items": review_context["review_items"],
        "items_total": len(review_context["review_all_items"]),
        "pending_total_count": review_context["review_summary"]["unconfirmed"],
        "query": query, "bank_filter": bank,
        "filters": {"q": query, "bank": bank, "status": status_filter},
        "total": sum(float(x.get("signed_amount", 0)) for x in all_events),
    })
    return render(request, "today.html", "Dia a dia", **render_context)


@router.get("/transactions", response_class=HTMLResponse)
def transactions_page(request: Request):
    txs, accounts, cards, lookups, settings, _ = _snapshot()
    nav = _nav_context(request)
    query = request.query_params.get("q", "").strip().casefold()
    type_filter = request.query_params.get("type", "")
    movement_month_filter = request.query_params.get("movement_month", "").strip()
    try:
        selected_month = month_start(movement_month_filter) if movement_month_filter else None
    except ValueError:
        selected_month, movement_month_filter = None, ""
    selected_idx = month_id(selected_month) if selected_month else None
    bank_filter = request.query_params.get("bank", "").strip()
    card_filter = request.query_params.get("card", "").strip()
    member_options = sorted({str(row.get("member") or "Família") for row in txs if str(row.get("member") or "Família").strip()}, key=str.casefold)
    member_filter = request.query_params.get("movement_member", "").strip()
    if member_filter not in member_options:
        member_filter = ""
    visible = [row for row in txs if not row["archived"]]
    if nav["person"] != FAMILY:
        visible = [row for row in visible if str(row.get("member", "")) == nav["person"]]
    if query:
        visible = [row for row in visible if query in " ".join(str(row.get(k, "")) for k in ("description", "category", "member", "account_card")).casefold()]
    if type_filter:
        visible = [row for row in visible if row["type"] == type_filter]
    if selected_idx is not None:
        def active_in_selected_month(row):
            start_idx = month_id(row.get("start_month"))
            end_idx = month_id(row.get("end_month"))
            return start_idx is not None and start_idx <= selected_idx and (end_idx is None or end_idx >= selected_idx)
        visible = [row for row in visible if active_in_selected_month(row)]
    if bank_filter:
        visible = [row for row in visible if str(row.get("payment_bank") or row.get("account_card") or "").casefold() == bank_filter.casefold()]
    if card_filter:
        visible = [row for row in visible if row.get("card_flag") == 1 and row.get("account_card") == card_filter]
    if member_filter:
        visible = [row for row in visible if str(row.get("member", "")) == member_filter]
    visible.sort(key=lambda row: int(row.get("id") or 0), reverse=True)
    context_month = month_start(nav["month"])
    if context_month is not None:
        visible = [
            dict(row, installment_number=installment_number_for_month(row, context_month))
            if row.get("type") == "Parcelado" else row
            for row in visible
        ]
    visible = [dict(row, visual=_movement_visual(row)) for row in visible]
    results_count = len(visible)
    try:
        page = max(1, int(request.query_params.get("page", "1") or 1))
    except ValueError:
        page = 1
    per_page = 40
    pages = max(1, (len(visible) + per_page - 1) // per_page)
    page = min(page, pages)
    visible = visible[(page - 1) * per_page:page * per_page]
    banks = sorted({str(row.get("payment_bank") or row.get("account_card") or "").strip() for row in txs if str(row.get("payment_bank") or row.get("account_card") or "").strip()}, key=str.casefold)
    filters = {"q": query, "type": type_filter, "movement_month": movement_month_filter,
               "month": nav["month"], "bank": bank_filter, "card": card_filter,
               "movement_member": member_filter, "person": nav["person"], "horizon": nav["horizon"]}
    filter_query = urlencode({key: value for key, value in filters.items() if value})
    return render(request, "transactions.html", "Movimentações", rows=visible, query=query, type_filter=type_filter,
                  page=page, pages=pages, types=TRANSACTION_TYPES,
                  month=month_key(settings.get("reference_date", date.today())), month_filter=movement_month_filter,
                  bank_filter=bank_filter, card_filter=card_filter, member_filter=member_filter,
                  banks=banks, cards=cards, members=member_options, results_count=results_count,
                  movement_month=movement_month_filter, filter_query=filter_query)


def _reserve_names_for_month(txs, transaction_date):
    selected_month = month_start(transaction_date)
    if selected_month is None:
        return []
    status = pot_monthly_status(txs, selected_month, person=FAMILY)
    return sorted(
        {str(row.get("pot")) for row in status.get("rows", []) if str(row.get("pot") or "").strip()},
        key=str.casefold,
    )


def _validate_linked_pot(values, txs):
    linked_pot = str(values.get("linked_pot") or "").strip()
    if not linked_pot:
        return
    selected_month = month_start(values.get("transaction_date"))
    if selected_month is None:
        raise ValueError("Informe uma data válida antes de vincular uma reserva.")
    available = set(_reserve_names_for_month(txs, selected_month))
    if linked_pot in available:
        return
    raise ValueError(f"A reserva “{linked_pot}” não está planejada para {month_key(selected_month)}. Escolha uma opção disponível ou deixe sem vínculo.")


def _preserve_existing_link(values, form, original):
    preserved = str(form.get("preserve_linked_pot") or "").strip()
    clear_link = str(form.get("clear_stale_link") or "").strip().lower() in {"yes", "on", "true", "1"}
    if values.get("linked_pot"):
        return
    if clear_link:
        values["linked_pot"] = ""
        return
    if not preserved:
        return
    if not original or preserved != str(original.get("linked_pot") or ""):
        raise ValueError("O vínculo de reserva mudou. Reabra o lançamento e escolha uma opção válida.")
    raise ValueError(f"A reserva atual “{preserved}” não está planejada nesta competência. Escolha uma reserva disponível ou marque a remoção explícita do vínculo atual.")


@router.get("/api/reserve-options")
def reserve_options(request: Request):
    selected_month = month_start(request.query_params.get("month", ""))
    if selected_month is None:
        raise HTTPException(status_code=422, detail="Informe uma competência válida.")
    txs, _, _, _, _, _ = _snapshot()
    return JSONResponse(
        {"month": month_key(selected_month), "reserves": _reserve_names_for_month(txs, selected_month)},
        headers={"Cache-Control": "private, no-store"},
    )


def _form_context(request: Request, title: str, initial: dict[str, Any] | None = None, error: str = "", **extra):
    txs, accounts, cards, lookups, settings, _ = _snapshot()
    record = dict(initial or {})
    current_day = datetime.now(ZoneInfo("America/Sao_Paulo")).date()
    if record.get("date_precision") not in ("exact", "recurrence"):
        legacy_date = as_date(record.get("transaction_date")) or as_date(record.get("start_month"))
        if legacy_date and legacy_date.day == 1:
            current_month = current_day.replace(day=1)
            legacy_month = legacy_date.replace(day=1)
            legacy_day = 1 if legacy_month <= current_month else 10
            record["transaction_date"] = legacy_date.replace(day=legacy_day).isoformat()
    if not record.get("transaction_date"):
        selected_month = _selected_month(request, settings)
        current_month = current_day.replace(day=1)
        default_day = current_day.day if selected_month == current_month else (1 if selected_month < current_month else 10)
        record["transaction_date"] = selected_month.replace(day=default_day).isoformat()
    for key in ("return_page", "return_card", "return_month"):
        if key not in record and request.query_params.get(key):
            record[key] = request.query_params.get(key)
    return_path, return_filters = _transaction_return_target(record)
    return_form = record if any(key in record for key in ("_context_month", "_context_person", "_context_horizon")) else None
    cancel_url = _return_url(request, return_path, return_form, **return_filters)
    billing = card_billing_context(cards, settings)
    if record.get("id") and record.get("type") in CARD_TYPES and not record.get("first_bill_month"):
        record["first_bill_month"] = record.get("start_month")
    reserve_month = month_start(record.get("transaction_date"))
    reserve_names = _reserve_names_for_month(txs, reserve_month)
    linked_pot = str(record.get("linked_pot") or "")
    stale_linked_pot = linked_pot if record.get("id") and linked_pot and linked_pot not in reserve_names else ""
    repeated = bool(record.get("id")) and (record.get("type") == "Parcelado" or record.get("end_month") != record.get("start_month"))
    return render(request, "transaction_form.html", title, initial=record, error=error,
                  members=lookups.get("membro", ["Hener", "Ju"]), categories=lookups.get("categoria", []),
                  category_groups=_group_category_suggestions(lookups.get("categoria", [])),
                  accounts=accounts, cards=billing["cards"], card_closing_days=billing["card_closing_days"],
                  types=TRANSACTION_TYPES, reserve_names=reserve_names,
                  total_amount=record.get("total_amount", record.get("amount", "")),
                  first_bill=str(record.get("first_bill_month") or "")[:7],
                  editing_series=repeated, series_start=record.get("start_month"), series_end=record.get("end_month"),
                  stale_linked_pot=stale_linked_pot,
                  cancel_url=cancel_url, return_page=record.get("return_page", ""),
                  return_card=record.get("return_card", ""), return_month=record.get("return_month", ""), **extra)


@router.get("/transactions/new", response_class=HTMLResponse)
def new_transaction(request: Request):
    nav = _nav_context(request)
    purchase = date.today().isoformat() if nav["month"] == date.today().strftime("%Y-%m") else nav["month"] + "-01"
    initial = {"transaction_date": purchase, "type": "Débito/Pix", "installment_count": 1,
               "member": nav["person"] if nav["person"] != FAMILY else "Hener"}
    return_page = str(request.query_params.get("return_page", "")).strip()
    if return_page not in {"today", "cash", "transactions", "cards", "open-finance"}:
        return_page = "transactions"
    initial.update({"return_page": return_page, "_context_month": nav["month"],
                    "_context_person": nav["person"], "_context_horizon": nav["horizon"]})
    title = "Novo lançamento"
    duplicate_id = str(request.query_params.get("duplicate_id", "")).strip()
    if duplicate_id:
        try:
            source_id = int(duplicate_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="Lançamento não encontrado.") from None
        txs, _, _, _, _, _ = _snapshot()
        source = next((row for row in txs if row.get("id") == source_id and not row.get("archived")), None)
        if source is None:
            raise HTTPException(status_code=404, detail="Lançamento não encontrado.")
        today = datetime.now(ZoneInfo("America/Sao_Paulo")).date()
        initial = dict(source)
        initial.pop("id", None)
        initial.pop("installment_overrides", None)
        initial.update({"return_page": return_page, "_context_month": nav["month"],
                        "_context_person": nav["person"], "_context_horizon": nav["horizon"]})
        initial.update({
            "transaction_date": today.isoformat(),
            "start_month": today.replace(day=1).isoformat(),
            "end_month": today.replace(day=1).isoformat(),
            "first_bill_month": "",
            "repeat_monthly": False,
            "_duplicate_source_description": str(source.get("description") or "")[:255],
        })
        available = set(_reserve_names_for_month(txs, today))
        source_linked_pot = str(source.get("linked_pot") or "").strip()
        if source_linked_pot and source_linked_pot not in available:
            initial["linked_pot"] = ""
            initial["_duplicate_reserve_removed"] = source_linked_pot
        else:
            initial["linked_pot"] = source_linked_pot
        title = "Duplicar lançamento"
    else:
        source_date = as_date(request.query_params.get("source_date"))
        source_description = str(request.query_params.get("source_description", "")).strip()[:191]
        if source_date and source_description:
            initial.update({"transaction_date": source_date.isoformat(), "description": source_description,
                            "linked_pot": ""})
        source_member = str(request.query_params.get("source_member", ""))
        if source_member:
            _, _, _, lookups, _, _ = _snapshot()
            if source_member in {str(value) for value in lookups.get("membro", [])}:
                initial["member"] = source_member
        source_type = str(request.query_params.get("source_type", ""))
        if source_type in {"Débito/Pix", "Receita", "Crédito à vista", "Parcelado"}:
            _, accounts, cards, _, _, _ = _snapshot()
            account_names = {str(row.get("name") or "") for row in accounts}
            card_names = {str(row.get("name") or "") for row in cards}
            try:
                source_amount = float(request.query_params.get("source_amount", ""))
            except (TypeError, ValueError):
                source_amount = 0.0
            source_account = str(request.query_params.get("source_account", "")).strip()
            if source_type in {"Débito/Pix", "Receita"} and source_amount > 0 and math.isfinite(source_amount):
                initial.update({"type": source_type, "amount": source_amount, "total_amount": source_amount,
                                "installment_count": 1})
                if source_account in account_names:
                    initial["account_card"] = source_account
            elif source_type == "Crédito à vista":
                if source_amount > 0 and math.isfinite(source_amount):
                    initial.update({"type": source_type, "amount": source_amount, "total_amount": source_amount,
                                    "installment_count": 1})
                if source_account in card_names:
                    initial["account_card"] = source_account
            elif source_type == "Parcelado":
                try:
                    installments = int(request.query_params.get("source_installments", ""))
                except (TypeError, ValueError):
                    installments = 0
                if 2 <= installments <= 120:
                    initial.update({"type": source_type, "installment_count": installments})
                if source_account in card_names:
                    initial["account_card"] = source_account
    return _form_context(request, title, initial)


def _transaction_from_form(form) -> dict[str, Any]:
    tx_type = str(form.get("type", "")).strip()
    if tx_type not in TRANSACTION_TYPES:
        raise ValueError("Selecione um tipo de lançamento válido.")
    raw_purchase_date = str(form.get("transaction_date", "")).strip()
    try:
        purchase_date = as_date(raw_purchase_date) if len(raw_purchase_date) == 10 else None
    except ValueError:
        purchase_date = None
    if not purchase_date:
        raise ValueError("Informe uma data válida para a movimentação.")
    try:
        total = parse_amount(form.get("total_amount", "0"))
    except ValueError:
        raise ValueError("Informe um valor monetário válido.") from None
    if not math.isfinite(total) or abs(total) > 1_000_000_000:
        raise ValueError("Informe um valor válido dentro do limite permitido.")
    total = float(Decimal(str(total)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    if total == 0:
        raise ValueError("Informe um valor válido diferente de zero.")
    if total < 0 and tx_type != "Crédito à vista":
        raise ValueError("Valores negativos são permitidos somente para estornos em Crédito à vista no cartão.")
    count = max(1, int(form.get("installment_count", 1) or 1)) if tx_type == "Parcelado" else 1
    if tx_type == "Parcelado" and count < 2:
        raise ValueError("Uma compra parcelada precisa ter pelo menos duas parcelas.")
    if count > 120:
        raise ValueError("Use no máximo 120 parcelas.")
    account = str(form.get("account_card", "")).strip()
    if not account:
        raise ValueError("Selecione uma conta ou cartão.")
    if tx_type in CARD_TYPES:
        _, _, billing_cards, _, billing_settings, _ = _snapshot()
        card_names = {str(card.get("name") or "") for card in billing_cards}
        if account not in card_names:
            raise ValueError("Crédito à vista e compras parceladas precisam estar vinculados a um cartão.")
        if total < 0 and (tx_type != "Crédito à vista" or account not in card_names):
            raise ValueError("Estorno negativo exige Crédito à vista vinculado a cartão.")
        first_bill = month_start(first_bill_month_for_form(form, billing_cards, billing_settings))
    else:
        first_bill = month_start(purchase_date)
    start = first_bill if tx_type in CARD_TYPES else month_start(purchase_date)
    if start is None:
        raise ValueError("Informe um mês válido para a movimentação.")
    start_idx = month_id(start)
    if start_idx is None:
        raise ValueError("Informe um mês válido para a movimentação.")
    if tx_type == "Parcelado":
        end = month_from_id(start_idx + count - 1)
    elif tx_type in CARD_TYPES:
        end = start
    elif tx_type in RECURRING_TYPES and form.get("repeat_monthly") == "on":
        raw_end = str(form.get("end_month", "")).strip()
        end = month_start(raw_end) if raw_end else None
        if raw_end and end is None:
            raise ValueError("Informe um mês final válido para a recorrência.")
        if end and end < start:
            raise ValueError("O fim da recorrência não pode anteceder o início.")
    else:
        end = start
    target = str(form.get("destination_account", "")).strip()
    if tx_type == "Transferência" and not target:
        raise ValueError("Selecione a conta de destino da transferência.")
    description = str(form.get("description", "")).strip()
    if not description:
        raise ValueError("Informe uma descrição.")
    return {
        "member": str(form.get("member", "Família")),
        "category": str(form.get("category", "")).strip(),
        "description": description,
        "type": tx_type,
        "account_card": account,
        "amount": total / count if tx_type == "Parcelado" else total,
        "total_amount": total,
        "installment_count": count,
        "start_month": start.isoformat(),
        "end_month": end.isoformat() if end else None,
        "destination_account": target,
        "linked_pot": str(form.get("linked_pot", "")).strip(),
        "transaction_date": purchase_date.isoformat(),
    }


def _duplicate_review(request, form, values, txs, tx_id=None):
    duplicates = probable_duplicates(txs, values, tx_id)
    if not duplicates:
        return None
    session = getattr(request.state, "family_session", None) or {}
    expected = {"kind": "duplicate", "digest": values_digest(values), "tx_id": tx_id, "csrf": session.get("csrf")}
    if form.get("confirm_duplicate") == "yes" and read_review(str(form.get("duplicate_token", ""))) == expected:
        return None
    record = {**dict(form), "id": tx_id} if tx_id else dict(form)
    if tx_id:
        original = next((row for row in txs if row["id"] == tx_id), {})
        record = {**original, **record}
    title = "Editar lançamento" if tx_id else ("Duplicar lançamento" if record.get("_duplicate_source_description") else "Novo lançamento")
    return _form_context(request, title, record,
                         "Existe um lançamento parecido. Confira antes de gravar.",
                         duplicates=duplicates, duplicate_token=sign_review(expected),
                         entry_version=form.get("_entry_version", ""))


@router.post("/transactions/new")
async def create_transaction(request: Request):
    form = await checked_form(request)
    try:
        values = _transaction_from_form(form)
        with get_session_factory()() as db:
            current_snapshot = read_snapshot(db)
            if form.get("preserve_linked_pot"):
                raise ValueError("Não é possível preservar um vínculo antigo em um lançamento novo.")
            _validate_linked_pot(values, current_snapshot[0])
            from app.assistant_routes import _closed_edit_message, _closed_origin
            if _closed_origin(values, current_snapshot[5]):
                raise ValueError(_closed_edit_message())
            duplicate_page = _duplicate_review(request, form, values, current_snapshot[0])
            if duplicate_page is not None:
                return duplicate_page
            draft_token = str(form.get("_notification_token", ""))
            draft_id = None
            if draft_token:
                from app.notification_routes import claim_draft
                draft_id = claim_draft(db, draft_token, request.state.family_session["csrf"])
            tx_id = insert_transaction(db, values)
            if draft_id is not None:
                from app.notification_routes import complete_draft
                complete_draft(db, draft_id, tx_id)
            db.commit()
            event_id = latest_activity_id(db, "transaction", tx_id, "create")
        return_path, return_filters = _transaction_return_target(form)
        redirect_filters = {**return_filters, "saved": "1"}
        if event_id is not None:
            redirect_filters["undo_id"] = str(event_id)
        return RedirectResponse(_return_url(request, return_path, form, **redirect_filters), status_code=303)
    except (ValueError, TypeError) as exc:
        initial = dict(form)
        title = "Duplicar lançamento" if initial.get("_duplicate_source_description") else "Novo lançamento"
        return _form_context(request, title, initial, str(exc))


@router.get("/transactions/{tx_id}/edit", response_class=HTMLResponse)
def edit_transaction_page(request: Request, tx_id: int):
    txs, _, _, _, _, _ = _snapshot()
    row = next((item for item in txs if item["id"] == tx_id and not item["archived"]), None)
    if not row:
        raise HTTPException(status_code=404, detail="Lançamento não encontrado.")
    session = getattr(request.state, "family_session", None) or {}
    version = sign_review({"kind": "entry-version", "tx_id": tx_id,
                           "fingerprint": values_digest(row), "csrf": session.get("csrf")})
    return _form_context(request, "Editar lançamento", row, entry_version=version)


@router.post("/transactions/{tx_id}/edit")
async def update_transaction_page(request: Request, tx_id: int):
    form = await checked_form(request)
    try:
        values = _transaction_from_form(form)
        with get_session_factory()() as db:
            current_snapshot = read_snapshot(db)
            txs = current_snapshot[0]
            original = next((row for row in txs if row["id"] == tx_id and not row["archived"]), None)
            if original is None:
                raise HTTPException(status_code=404, detail="Lançamento não encontrado.")
            _preserve_existing_link(values, form, original)
            _validate_linked_pot(values, txs)
            from app.assistant_routes import _closed_edit_message, _closed_origin
            if _closed_origin(original, current_snapshot[5]) or _closed_origin(values, current_snapshot[5]):
                raise ValueError(_closed_edit_message())
            if not form.get("_ai_review_token"):
                version = read_review(str(form.get("_entry_version", "")))
                if (not version or version.get("kind") != "entry-version"
                    or version.get("tx_id") != tx_id
                    or version.get("csrf") != request.state.family_session["csrf"]
                    or version.get("fingerprint") != values_digest(original)):
                    raise HTTPException(status_code=409, detail="Este lançamento foi alterado em outro aparelho. Reabra a edição para conferir a versão mais recente.")
            if form.get("_ai_review_token"):
                review = read_review(str(form["_ai_review_token"]))
                if (not review or review.get("kind") != "general-ai-review"
                    or review.get("csrf") != request.state.family_session["csrf"]
                    or review.get("tx_id") != tx_id
                    or review.get("fingerprint") != values_digest(original)):
                    raise ValueError("O lançamento mudou ou a proposta expirou. Gere uma nova análise antes de salvar.")
            if original.get("end_month") is None and values.get("end_month") is not None and form.get("confirm_series_end") != "yes":
                return _form_context(request, "Editar lançamento", {**original, **dict(form), "id": tx_id}, "Confirme explicitamente o encerramento da recorrência aberta.", editing_original=original, entry_version=form.get("_entry_version", ""))
            if (original["type"] == "Parcelado" or original.get("end_month") != original.get("start_month")) and form.get("confirm_series") != "yes":
                return _form_context(request, "Editar lançamento", {**original, **dict(form), "id": tx_id}, "Revise e confirme a alteração de toda a série.", editing_original=original, entry_version=form.get("_entry_version", ""))
            duplicate_page = _duplicate_review(request, form, values, txs, tx_id)
            if duplicate_page is not None:
                return duplicate_page
            update_transaction(db, tx_id, values, expected_before=original)
            db.commit()
            event_id = latest_activity_id(db, "transaction", tx_id, "update")
        return_path, return_filters = _transaction_return_target(form)
        redirect_filters = {**return_filters, "saved": "1"}
        if event_id is not None:
            redirect_filters["undo_id"] = str(event_id)
        return RedirectResponse(_return_url(request, return_path, form, **redirect_filters), status_code=303)
    except ConcurrentEditError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except (ValueError, TypeError) as exc:
        initial = {**dict(form), "id": tx_id}
        preserved = str(form.get("preserve_linked_pot") or "").strip()
        if preserved and not str(form.get("clear_stale_link") or "").strip():
            initial["linked_pot"] = preserved
        return _form_context(request, "Editar lançamento", initial, str(exc),
                             entry_version=form.get("_entry_version", ""))


@router.get("/transactions/{tx_id}/delete", response_class=HTMLResponse)
def delete_transaction_page(request: Request, tx_id: int):
    txs, _, _, _, settings, closed = _snapshot()
    row = next((item for item in txs if item["id"] == tx_id and not item["archived"]), None)
    if row is None:
        raise HTTPException(status_code=404, detail="Lançamento não encontrado.")
    repeated = row["type"] == "Parcelado" or row.get("end_month") != row.get("start_month")
    from app.assistant_routes import _closed_origin
    session = getattr(request.state, "family_session", None) or {}
    version = sign_review({"kind": "entry-version", "tx_id": tx_id,
                           "fingerprint": values_digest(row), "csrf": session.get("csrf")})
    return_path, return_filters = _transaction_return_target(request.query_params)
    cancel_url = _return_url(request, return_path, **return_filters)
    start = month_start(row.get("start_month"))
    end = month_start(row.get("end_month")) if row.get("end_month") else None
    today_month = date.today().replace(day=1)
    try:
        requested_month = month_start(request.query_params.get("return_month") or request.query_params.get("month") or settings.get("reference_date"))
        requested_cutoff = month_start(request.query_params.get("cutoff_month")) if request.query_params.get("cutoff_month") else None
    except (TypeError, ValueError):
        requested_month, requested_cutoff = None, None
    cutoff = requested_month or today_month
    if cutoff < today_month:
        cutoff = today_month
    if start and cutoff < start:
        cutoff = start
    if requested_cutoff and start and requested_cutoff >= start and (end is None or requested_cutoff <= end):
        cutoff = requested_cutoff
    for _ in range(2400):
        if (end is not None and cutoff > end) or not bool(closed.get(month_key(cutoff), False)):
            break
        cutoff = month_from_id((month_id(cutoff) or 0) + 1)
    future_delete_allowed = bool(repeated and start and (end is None or cutoff <= end)
                                 and not bool(closed.get(month_key(cutoff), False)))
    blocked_by_closed = _closed_origin(row, closed)
    return render(request, "transaction_delete.html", "Excluir lançamento", row=row, repeated=repeated,
                  entry_version=version, blocked_by_closed=blocked_by_closed,
                  full_delete_allowed=not blocked_by_closed, future_delete_allowed=future_delete_allowed,
                  cutoff_month=month_key(cutoff), delete_error=request.query_params.get("error", ""), cancel_url=cancel_url,
                  return_page=request.query_params.get("return_page", ""),
                  return_card=request.query_params.get("return_card", ""),
                  return_month=request.query_params.get("return_month", ""))


@router.post("/transactions/{tx_id}/delete")
async def delete_transaction_confirm(request: Request, tx_id: int):
    form = await checked_form(request)
    if form.get("confirmation") != "delete":
        raise HTTPException(status_code=400, detail="Confirme a exclusão do lançamento.")
    scope = str(form.get("deletion_scope", "all"))
    if scope not in {"all", "future"}:
        raise HTTPException(status_code=400, detail="Escolha um escopo de exclusão válido.")
    event_id = None
    with get_session_factory()() as db:
        current_snapshot = read_snapshot(db)
        original = next((row for row in current_snapshot[0] if row["id"] == tx_id and not row["archived"]), None)
        if original is None:
            raise HTTPException(status_code=404, detail="Lançamento não encontrado.")
        version = read_review(str(form.get("_entry_version", "")))
        if (not version or version.get("kind") != "entry-version"
            or version.get("tx_id") != tx_id
            or version.get("csrf") != request.state.family_session["csrf"]
            or version.get("fingerprint") != values_digest(original)):
            raise HTTPException(status_code=409, detail="Este lançamento foi alterado em outro aparelho. Reabra a confirmação para conferir a versão mais recente.")
        repeated = original["type"] == "Parcelado" or original.get("end_month") != original.get("start_month")
        try:
            if scope == "future":
                if not repeated:
                    raise ValueError("Exclusão prospectiva só se aplica a parcelas ou recorrências.")
                cutoff = month_start(form.get("cutoff_month"))
                if cutoff is None:
                    raise ValueError("Escolha a competência aberta a partir da qual excluir.")
                _, event_id = stop_transaction_from_month(db, tx_id, cutoff, expected_before=original)
            else:
                from app.assistant_routes import _closed_edit_message, _closed_origin
                if _closed_origin(original, current_snapshot[5]):
                    raise HTTPException(status_code=409, detail=_closed_edit_message())
                delete_transaction(db, tx_id, expected_before=original)
                event_id = latest_activity_id(db, "transaction", tx_id, "delete")
        except ConcurrentEditError as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except (ValueError, TypeError) as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from None
        db.commit()
    return_path, return_filters = _transaction_return_target(form)
    redirect_filters = {**return_filters, "saved": "1"}
    if event_id is not None:
        redirect_filters["undo_id"] = str(event_id)
    return RedirectResponse(_return_url(request, return_path, form, **redirect_filters), status_code=303)


@router.get("/cash", response_class=HTMLResponse)
def cash_page(request: Request):
    query = request.url.query
    return RedirectResponse("/today" + (f"?{query}" if query else ""), status_code=307)


@router.post("/cash/status")
async def cash_status(request: Request):
    form = await checked_form(request)
    month = str(form.get("month", ""))
    person = str(form.get("person", FAMILY))
    if person not in PERSONS:
        person = FAMILY
    item_key = str(form.get("item_key", ""))
    paid = form.get("paid") == "on"
    try:
        month = normalize_review_month(month)
        return_to = normalize_return_to(form.get("return_to"), "/today")
        paid_on = validate_paid_on(form.get("paid_on")) if paid else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    with get_session_factory()() as db:
        txs, accounts, cards, _, _, closed = read_snapshot(db)
        blocked = status_change_block_reason(month, bool(closed.get(month, False)))
        if blocked:
            raise HTTPException(status_code=409, detail=blocked)
        items = cashflow_items(txs, cards, accounts, month, person)
        selected = next((item for item in items if item["item_key"] == item_key), None)
        if selected is None:
            raise HTTPException(status_code=404, detail="Movimentação não encontrada para este mês.")
        if paid:
            try:
                validate_item_confirmation(selected, paid_on)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
        related = [selected]
        if selected["type"] in ("Transf. Env.", "Transf. Receb.") and selected.get("transaction_id") is not None:
            family_items = cashflow_items(txs, cards, accounts, month, FAMILY)
            related = [item for item in family_items if item.get("transaction_id") == selected["transaction_id"]
                       and item.get("type") in ("Transf. Env.", "Transf. Receb.")]
        for item in related:
            save_cashflow_status(db, month, item["item_key"], paid, paid_on, item_label=item.get("description", ""))
        db.commit()
    location = build_return_url(
        return_to,
        month=month,
        person=person,
        horizon=form.get("horizon", form.get("_context_horizon", "12")),
        **{key: form.get(key) for key in ("from", "to", "q", "bank", "status")},
    )
    location += "&" + urlencode({"saved": "1", "saved_item": item_key})
    return RedirectResponse(f"{location}#daily-review", status_code=303)


@router.post("/cash/close")
async def cash_close(request: Request):
    form = await checked_form(request)
    with get_session_factory()() as db:
        set_month_closed(db, form.get("month"), form.get("closed") == "1")
        db.commit()
    location = _return_url(request, "/today", form, month=form.get("month"),
                            person=form.get("person", FAMILY), q=form.get("q"), bank=form.get("bank"), status=form.get("status"))
    return RedirectResponse(f"{location}#cash-overview", status_code=303)


def _personal_bank_context(txs, accounts, cards, settings, closed, person, month, end, full, chart_id,
                           ):
    if person == FAMILY:
        return {"member_bank": None, "member_bank_rows": [], "member_bank_chart": None}
    observed_month = month_start(settings.get("reference_date")) or date.today().replace(day=1)
    with get_session_factory()() as db:
        statuses = cashflow_statuses(db, observed_month)
        legacy_rows = legacy_statuses(db, observed_month)
    series = member_bank_series(
        txs, accounts, cards, settings, statuses=statuses, legacy_rows=legacy_rows,
        closed_months=closed, person=person, start=month, end=end, observed_month=observed_month,
    )
    bank_rows = member_bank_rows_from(series, month)
    chart = build_line_chart(
        bank_rows if full else bank_rows[:12],
        [("confirmed", "Saldo pelas baixas", "#123b63", False),
         ("projected", "Saldo previsto", "#79a7d0", True)],
        chart_id, "Contas do titular: saldo pelas baixas e previsto", max_x_labels=5,
    )
    return {"member_bank": member_bank_row(series, month), "member_bank_rows": bank_rows,
            "member_bank_chart": chart}


@router.get("/flow", response_class=HTMLResponse)
def flow_page(request: Request):
    txs, accounts, cards, _, settings, closed = _snapshot()
    month = _selected_month(request, settings)
    person = _person(request)
    rows = project_fgts(_forecast(txs, accounts, settings, closed, person, requested=month), settings)
    current = _row_for(rows, month) or (rows[0] if rows else None)
    visible_rows = _future_rows(rows, month)
    show_full_charts = _nav_context(request)["horizon"] == "all"
    chart_rows = visible_rows if show_full_charts else visible_rows[:12]
    personal_bank = _personal_bank_context(
        txs, accounts, cards, settings, closed, person, month,
        rows[-1]["month"] if rows else month, show_full_charts, "flow-member-bank",
    )
    budget_chart = build_line_chart(
        chart_rows,
        [
            ("monthly_balance_with_prior_impact", "Com compromissos anteriores", "#123b63", False),
            ("monthly_balance_without_prior_impact", "Sem compromissos anteriores", "#4e91c1", True),
        ],
        "flow-results", "Resultado mensal com e sem impacto de meses anteriores", max_x_labels=5,
    )
    bank_chart = build_line_chart(
        chart_rows,
        [
            ("projected_ending_balance", "Saldo bancário projetado", "#123b63", False),
            ("minimum_balance", "Piso de segurança", "#a9433d", True),
        ],
        "flow-bank", "Saldo bancário projetado e piso de segurança", max_x_labels=5,
    )
    fgts_chart = build_line_chart(
        chart_rows,
        [("fgts_accumulated", "FGTS acumulado estimado", "#4e91c1", False)],
        "flow-fgts", "FGTS acumulado estimado, separado do saldo bancário", max_x_labels=5,
    )
    cashboxes = _cashbox_accumulation(txs, accounts, cards, settings, closed, rows, person, month)
    cashbox_chart_rows = _future_rows(cashboxes["rows"], month)
    cashbox_chart_rows = cashbox_chart_rows if show_full_charts else cashbox_chart_rows[:12]
    cashbox_chart_end = cashbox_chart_rows[-1] if cashbox_chart_rows else None
    cashbox_categories = sorted({
        name for row in cashbox_chart_rows
        for name, amount in row.get("projected_by_category", {}).items()
        if abs(float(amount or 0)) >= 0.005
    })
    cashbox_specs = []
    for index, category in enumerate(cashbox_categories):
        field = f"cashbox_category_{index}"
        for row in cashbox_chart_rows:
            row["cashboxes_total"] = row.get("projected", 0)
            row[field] = row.get("projected_by_category", {}).get(category, 0)
        palette = ("#123b63", "#2b6797", "#4e91c1", "#719fc0", "#57738f", "#3d748c", "#8aaed0", "#607ca4", "#35678d", "#8695d1", "#5f7db8", "#89b0b3")
        cashbox_specs.append((field, category, palette[index % len(palette)]))
    for row in cashbox_chart_rows:
        row["cashboxes_total"] = row.get("projected", 0)
    cashbox_chart = build_stacked_bar_chart(
        cashbox_chart_rows, cashbox_specs, "flow-cashboxes",
        "Saldo acumulado estimado das caixinhas por categoria", max_x_labels=5,
    ) if cashboxes["integrity_ok"] else None
    return render(request, "flow.html", "Ganhos × gastos", month=month_key(month), person=person, people=PERSONS,
                  current=current, rows=visible_rows, budget_chart=budget_chart, bank_chart=bank_chart,
                  fgts_chart=fgts_chart, cashbox_chart=cashbox_chart,
                  cashbox_chart_end=cashbox_chart_end, cashbox_chart_months=len(cashbox_chart_rows),
                  cashbox_integrity_ok=cashboxes["integrity_ok"],
                  chart_horizon="all" if show_full_charts else "12",
                  chart_toggle_url=_chart_toggle_url(request, "/flow", show_full_charts), **personal_bank)


@router.get("/pots", response_class=HTMLResponse)
def pots_page(request: Request):
    txs, _, _, _, settings, _ = _snapshot()
    month = _selected_month(request, settings)
    person = _person(request)
    pot_data = pot_monthly_status(txs, month, person, settings.get("pot_order") or [])
    usage_details = reserve_usage_details(txs, month, person)
    for row in pot_data["rows"]:
        details = usage_details.get(str(row.get("pot") or ""), [])
        row["usage_details"] = details
        row["usage_detail_matches"] = sum(item["amount_cents"] for item in details) == round(float(row.get("used") or 0) * 100)
    daily_reserve = daily_reserve_summary(
        txs, month, person, pot_data["totals"]["reserved"],
        usage_details_by_pot=usage_details,
        active_pot_names=[str(row["pot"]) for row in pot_data["rows"]],
        expected_used_total=pot_data["totals"]["used"],
    )
    daily_reserve_chart = build_line_chart(
        daily_reserve["rows"],
        [("projected", "Consumo acumulado", "#246b9b", False),
         ("minimum_balance", "Total reservado", "#91abc2", True)],
        "reserve-daily-consumption",
        f"Consumo acumulado das reservas por competência do orçamento/ciclo em {month_label(month)}",
        max_x_labels=7,
    )
    return render(request, "pots.html", "Potes e reservas", month=month_key(month), month_name=month_label(month), person=person,
                  people=PERSONS, data=pot_data, pot_groups=group_pots(pot_data["rows"]),
                  daily_reserve=daily_reserve, daily_reserve_chart=daily_reserve_chart)


@router.get("/plan", response_class=HTMLResponse)
def plan_page(request: Request):
    txs, accounts, cards, _, settings, closed = _snapshot()
    month = _selected_month(request, settings)
    person = _person(request)
    start_value = request.query_params.get("scenario_month") or month.isoformat()
    try:
        scenario_month = month_start(start_value) or month
    except ValueError:
        scenario_month = month
    try:
        amount = max(0.0, parse_amount(request.query_params.get("amount", "0")))
        if not math.isfinite(amount):
            amount = 0.0
    except ValueError:
        amount = 0.0
    try:
        parts = min(120, max(1, int(request.query_params.get("parts", "3"))))
    except ValueError:
        parts = 3
    selected_card = request.query_params.get("card", "")
    scenarios_end = [{"start_month": scenario_month, "months": parts}]
    plan_settings = {**settings, "forecast_end": effective_end(txs, settings, month, scenarios_end).isoformat()}
    base = _forecast(txs, accounts, plan_settings, closed, person, requested=month)
    cash = installments = None
    if amount > 0:
        cash = _forecast(txs, accounts, plan_settings, closed, person,
                         [{"monthly_amount": amount, "months": 1, "start_month": scenario_month}])
        installments = _forecast(txs, accounts, plan_settings, closed, person,
                                 [{"monthly_amount": amount / parts, "months": parts, "start_month": scenario_month}])
    first_idx = month_id(month)
    if first_idx is None:
        first_idx = month_id(date.today()) or 0
    compare = []
    for index, row in enumerate(base):
        row_idx = month_id(row["month"])
        if row_idx is None or row_idx < first_idx:
            continue
        cash_row = cash[index] if cash else None
        part_row = installments[index] if installments else None
        out_cash = float(cash_row["scenario_outflow"]) if cash_row else 0.0
        out_parts = float(part_row["scenario_outflow"]) if part_row else 0.0
        compare.append({
            "month": row["month"], "base_ae": row["monthly_balance_with_prior_impact"],
            "base_af": row["monthly_balance_without_prior_impact"], "base_ag": row["projected_ending_balance"],
            "cash_ae": row["monthly_balance_with_prior_impact"] - out_cash,
            "cash_af": row["monthly_balance_without_prior_impact"] - out_cash,
            "cash_ag": cash_row["projected_ending_balance"] if cash_row else row["projected_ending_balance"],
            "parts_ae": row["monthly_balance_with_prior_impact"] - out_parts,
            "parts_af": row["monthly_balance_without_prior_impact"] - out_parts,
            "parts_ag": part_row["projected_ending_balance"] if part_row else row["projected_ending_balance"],
            "floor": row["minimum_balance"], "closed": bool(row.get("closed", False)),
        })
    show_full_charts = _nav_context(request)["horizon"] == "all"
    chart_rows = compare if show_full_charts else compare[:12]
    scenario_chart = build_line_chart(
        chart_rows,
        [
            ("base_ag", "Sem compra", "#123b63", False),
            ("cash_ag", "À vista", "#4e91c1", True),
            ("parts_ag", "Parcelado", "#79a7d0", False),
            ("floor", "Piso de segurança", "#a9433d", True),
        ],
        "purchase-scenarios", "Saldo bancário projetado nos cenários sem compra, à vista e parcelado", max_x_labels=5,
    )
    card_status = card_limits(txs, cards, scenario_month)
    selected_limit = next((item for item in card_status if item["card"] == selected_card), None)
    if selected_limit and amount:
        start_index = month_id(scenario_month)
        assert start_index is not None
        end_purchase = month_from_id(start_index + parts - 1)
        assert end_purchase is not None
        simulated_purchase = {"id": -1, "type": "Parcelado" if parts > 1 else "Crédito à vista", "member": person,
                              "account_card": selected_card, "amount": amount / parts, "total_amount": amount,
                              "installment_count": parts, "start_month": scenario_month.isoformat(),
                              "end_month": end_purchase.isoformat(), "card_flag": 1}
        for row in compare:
            original_limits = card_limits(txs, cards, row["month"])
            altered_limits = card_limits([*txs, simulated_purchase], cards, row["month"])
            row["card_available_base"] = next(item["available"] for item in original_limits if item["card"] == selected_card)
            row["card_available_parts"] = next(item["available"] for item in altered_limits if item["card"] == selected_card)
    cash_below = next((r for r in (cash or []) if _month_is_at_least(r, first_idx) and r["gap_to_minimum"] < 0), None)
    parts_below = next((r for r in (installments or []) if _month_is_at_least(r, first_idx) and r["gap_to_minimum"] < 0), None)
    return render(request, "plan.html", "Planejar e simular", month=month_key(month), scenario_month=month_key(scenario_month),
                  person=person, people=PERSONS, amount=amount, parts=parts, cards=cards, selected_card=selected_card,
                  card_status=selected_limit, card_available_after=(selected_limit["available"] - amount) if selected_limit and amount else None,
                  compare=compare, scenario_chart=scenario_chart, base=base, cash_below=cash_below, parts_below=parts_below,
                  end_base=_row_for(base, chart_rows[-1]["month"]) if chart_rows else None,
                  end_cash=_row_for(cash, chart_rows[-1]["month"]) if cash and chart_rows else None,
                  end_parts=_row_for(installments, chart_rows[-1]["month"]) if installments and chart_rows else None,
                  chart_horizon="all" if show_full_charts else "12",
                  chart_toggle_url=_chart_toggle_url(request, "/plan", show_full_charts))


def _assistant_context(target_pot: str = "", source_pot: str = "", start_month: str | None = None) -> dict[str, Any]:
    with get_session_factory()() as db:
        txs, accounts, cards, _, settings, closed = read_snapshot(db)
        first_open = month_from_id((month_id(date.today().replace(day=1)) or 0) + 1) or date.today().replace(day=1)
        try:
            requested_start = month_start(start_month) if start_month else None
        except ValueError:
            requested_start = None
        horizon = effective_end(txs, settings, requested=requested_start)
        start = requested_start or first_open
        if start < first_open or start > horizon:
            start = first_open

        pot_data = pot_monthly_status(txs, start, FAMILY, settings.get("pot_order") or [])
        current_pots = [row["pot"] for row in pot_data["rows"]]
        first_index, last_index = month_id(first_open), month_id(horizon)
        pot_reserve_schedule: dict[str, dict[str, float]] = {}
        if first_index is not None and last_index is not None and first_index <= last_index:
            for index in range(first_index, last_index + 1):
                schedule_month = month_from_id(index)
                if schedule_month is None:
                    continue
                schedule_rows = pot_monthly_status(txs, schedule_month, FAMILY, settings.get("pot_order") or [])['rows']
                pot_reserve_schedule[month_key(schedule_month)] = {
                    row["pot"]: float(row["reserved"]) for row in schedule_rows
                }
        essential_pots = [name for name in current_pots if classify_pot(name) == "Essenciais"]
        discretionary_pots = [name for name in current_pots if classify_pot(name) == "Lazer e escolhas"]
        default_target = next((name for name in essential_pots if "gasolina" in name.casefold()),
                              essential_pots[0] if essential_pots else "")
        default_source = next((name for name in discretionary_pots if "lazer" in name.casefold()),
                              discretionary_pots[0] if discretionary_pots else "")
        selected_target = target_pot if target_pot in essential_pots else default_target
        selected_source = source_pot if source_pot in discretionary_pots else default_source

        history: list[dict[str, Any]] = []
        current_month = date.today().replace(day=1)
        previous_month = month_from_id((month_id(current_month) or 0) - 1)
        closed_keys = sorted(key for key, is_closed in closed.items() if is_closed)
        for key in closed_keys:
            historic_month = month_start(key)
            if historic_month is None or historic_month >= current_month or (previous_month and historic_month > previous_month):
                continue
            items = apply_payment_statuses(
                cashflow_items(txs, cards, accounts, historic_month, FAMILY),
                legacy_statuses(db, historic_month), cashflow_statuses(db, historic_month),
            )
            external_items = [item for item in items if item["type"] in ("Fixo", "Débito/Pix", "Fatura cartão")]
            if not external_items or any(not item.get("paid") for item in external_items):
                continue
            historical_pots = pot_monthly_status(txs, historic_month, FAMILY, settings.get("pot_order") or [])["rows"]
            reserved = {row["pot"]: float(row["reserved"]) for row in historical_pots}
            used = confirmed_pot_usage(txs, historic_month, items, current_pots)
            rows = [
                {"pot": name, "reserved": reserved.get(name, 0.0),
                 "used": float(used.get(name, {}).get("amount", 0.0)),
                 "confirmed_count": int(used.get(name, {}).get("count", 0))}
                for name in current_pots
            ]
            history.append({"month": month_key(historic_month), "rows": rows})
        history = history[-6:]
        suggestion = suggest_reallocation(history, pot_data["rows"], selected_target, selected_source)
        adjustments = [
            {"start_month": row.start_month, "target_pot": row.target_pot,
             "target_before": row.target_before, "target_after": row.target_after,
             "source_pot": row.source_pot, "source_before": row.source_before,
             "source_after": row.source_after}
            for row in db.scalars(select(BudgetAdjustment).order_by(BudgetAdjustment.id.desc()).limit(20))
        ]
    return {
        "suggestion": suggestion, "pots": essential_pots, "discretionary_pots": discretionary_pots,
        "target_label": "Essenciais", "source_label": "Lazer e escolhas",
        "history_start": history[0]["month"] if history else None,
        "history_end": history[-1]["month"] if history else None,
        "pot_reserve_schedule": pot_reserve_schedule,
        "minimum_start": month_key(first_open), "forecast_end": horizon.isoformat(),
        "start_month": start.strftime("%Y-%m"), "adjustments": adjustments,
        "preferences": settings.get("assistant_preferences") or {},
    }


@router.get("/assistente", response_class=HTMLResponse)
def assistant_page(request: Request):
    context = _assistant_context(
        request.query_params.get("target_pot", ""), request.query_params.get("source_pot", ""),
        request.query_params.get("start_month") or _nav_context(request)["month"],
    )
    context["ai_enabled"] = assistant_agent_available()
    return render(request, "assistant.html", "Assistente financeiro", **context)


@router.post("/assistente/perguntar", response_class=HTMLResponse)
async def assistant_ask(request: Request):
    form = await checked_form(request)
    question = str(form.get("question", "")).strip()
    context = _assistant_context(start_month=_nav_context(request, form)["month"])
    context.update({"ai_enabled": assistant_agent_available(), "ai_question": question})
    if not context["ai_enabled"]:
        context["error"] = "A análise por IA não está habilitada neste ambiente. As projeções e sugestões existentes continuam disponíveis."
        response = render(request, "assistant.html", "Assistente financeiro", **context)
        response.status_code = 503
        return response
    if form.get("ai_consent") != "on":
        context["error"] = "Marque a autorização de processamento para enviar a pergunta e o resumo agregado ao assistente."
        response = render(request, "assistant.html", "Assistente financeiro", **context)
        response.status_code = 400
        return response
    if not question or len(question) > MAX_QUESTION_CHARS:
        context["error"] = f"Escreva uma pergunta com até {MAX_QUESTION_CHARS} caracteres."
        response = render(request, "assistant.html", "Assistente financeiro", **context)
        response.status_code = 400
        return response
    txs, accounts, cards, lookups, _, _ = _snapshot()
    protected = [
        *(str(row.get("name", "")) for row in accounts),
        *(str(row.get("name", "")) for row in cards),
        *(str(value) for value in lookups.get("membro", [])),
    ]

    def summary_provider(months: int):
        with get_session_factory()() as db:
            return build_financial_summary(db, months)

    try:
        result = await asyncio.to_thread(
            ask_finance_proposals, question, summary_provider, protected_identifiers=protected,
            preferences=context["preferences"],
        )
        context["ai_answer"] = result.get("answer", "")
        csrf = (getattr(request.state, "family_session", None) or {}).get("csrf")
        context["ai_proposals"] = []
        for proposal in result.get("proposals", []):
            action = proposal["action"]
            eligible = True
            if action["kind"] == "reallocate":
                suggestion = context["suggestion"]
                eligible = (bool(suggestion.get("has_enough_data")) and "gasolina" in str(suggestion.get("target_pot", "")).casefold()
                            and "lazer" in str(suggestion.get("source_pot", "")).casefold()
                            and action["amount"] <= float(suggestion.get("current_source", 0)))
                if "lazer" in str(context["preferences"].get("protected_goals", "")).casefold():
                    eligible = False
            context["ai_proposals"].append({**proposal, "signed_token": sign_review({"kind": "ai-proposal", "proposal": proposal, "csrf": csrf}) if eligible else ""})
    except AIAssistantError as exc:
        context["error"] = str(exc)
    except Exception:
        context["error"] = "A análise não pôde ser concluída agora. Nenhuma alteração foi feita."
    return render(request, "assistant.html", "Assistente financeiro", **context)


@router.post("/assistente/ajustar")
async def assistant_adjust(request: Request):
    form = await checked_form(request)
    target_pot = str(form.get("target_pot", ""))
    source_pot = str(form.get("source_pot", ""))
    start_raw = str(form.get("start_month", ""))
    form_values = {key: str(form.get(key, "")) for key in ("target_pot", "source_pot", "target_amount", "source_amount", "start_month")}
    context = _assistant_context(target_pot, source_pot, start_raw)
    try:
        target_after = round(parse_amount(form.get("target_amount", "")), 2)
        source_after = round(parse_amount(form.get("source_amount", "")), 2)
        start = month_start(start_raw)
        end = month_start(context["forecast_end"])
        first_open = month_start(context["minimum_start"])
        if target_pot not in context["pots"] or source_pot not in context["discretionary_pots"]:
            raise ValueError("Escolha uma reserva essencial e uma reserva de lazer disponíveis.")
        if start is None or end is None or first_open is None or start < first_open or start > end:
            raise ValueError("Escolha uma competência futura dentro do planejamento.")
        if target_after < 0 or source_after < 0:
            raise ValueError("Os valores mensais não podem ser negativos.")
        with get_session_factory()() as db:
            txs, _, _, _, settings, closed = read_snapshot(db)
            current_rows = pot_monthly_status(txs, start, FAMILY, settings.get("pot_order") or [])["rows"]
            current_by_name = {row["pot"]: row for row in current_rows}
            if target_pot not in current_by_name or source_pot not in current_by_name:
                raise ValueError("As reservas selecionadas não estão ativas nessa competência.")
            target_before = float(current_by_name[target_pot]["reserved"])
            source_before = float(current_by_name[source_pot]["reserved"])
            if abs((target_after + source_after) - (target_before + source_before)) > 0.01:
                raise ValueError("A soma dos dois valores precisa permanecer igual ao orçamento atual.")
            if abs(target_after - target_before) < 0.005:
                raise ValueError("Informe um ajuste diferente dos valores atuais.")
            for key, is_closed in closed.items():
                closed_month = month_start(key)
                if is_closed and closed_month and start <= closed_month <= end:
                    raise ValueError("O intervalo inclui um mês fechado e não pode ser alterado.")
            saved_target = reallocate_future_reserve(db, target_pot, target_after, start, end)
            saved_source = reallocate_future_reserve(db, source_pot, source_after, start, end)
            if abs(saved_target - target_before) > 0.01 or abs(saved_source - source_before) > 0.01:
                raise ValueError("Os valores mudaram durante a revisão. Atualize a página e tente novamente.")
            db.add(BudgetAdjustment(
                created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                start_month=start.isoformat(), end_month=end.isoformat(),
                target_pot=target_pot, source_pot=source_pot,
                target_before=target_before, target_after=target_after,
                source_before=source_before, source_after=source_after,
                sample_months=int(context["suggestion"]["months"]),
            ))
            record_activity_event(
                db, "update", "budget_adjustment", "family",
                {"target_pot": target_pot, "source_pot": source_pot, "start_month": start.isoformat(), "end_month": end.isoformat()},
                {"target_pot": target_pot, "source_pot": source_pot, "start_month": start.isoformat(), "end_month": end.isoformat()},
                undo_window=timedelta(0),
            )
            db.commit()
        return RedirectResponse(_return_url(request, "/assistente", form, applied="1"), status_code=303)
    except (TypeError, ValueError) as exc:
        context["error"] = str(exc)
        context["form_values"] = form_values
        response = render(request, "assistant.html", "Assistente financeiro", **context)
        response.status_code = 400
        return response


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    _, accounts, cards, lookups, settings, _ = _snapshot()
    return render(request, "settings.html", "Contas e regras", accounts=accounts,
                  cards=cards_with_closing_days(cards, settings), lookups=lookups, settings=settings)


@router.post("/settings")
async def save_settings_page(request: Request):
    form = await checked_form(request)
    _, accounts, cards, _, old_settings, _ = _snapshot()
    try:
        closing_days = validate_card_closing_days_form(form, cards, old_settings)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    def form_number(name: str, default: float) -> float:
        try:
            value = str(form.get(name, default)).strip()
            return parse_amount(value) if value else default
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"Valor inválido no campo {name}.") from None

    minimum_balance = form_number("minimum_balance", 5000)
    fgts_rate = form_number("fgts_rate", 0.08)
    fgts_salary_hener = form_number("fgts_salary_hener", 10351)
    fgts_salary_ju = form_number("fgts_salary_ju", 7500)
    if minimum_balance < 0 or fgts_salary_hener < 0 or fgts_salary_ju < 0 or not 0 <= fgts_rate <= 1:
        raise HTTPException(status_code=400, detail="O piso e os salários-base não podem ser negativos; a alíquota FGTS deve estar entre 0 e 1.")
    with get_session_factory()() as db:
        values = {
            "minimum_balance": minimum_balance,
            "base_month": (month_start(str(form.get("base_month", ""))) or date(2026, 8, 1)).isoformat(),
            "forecast_end": (month_start(str(form.get("forecast_end", ""))) or date(2030, 12, 1)).isoformat(),
            "fgts_salary_hener": fgts_salary_hener,
            "fgts_salary_ju": fgts_salary_ju,
            "fgts_rate": fgts_rate,
            "card_closing_days": closing_days,
        }
        save_settings(db, values)
        account_rows = []
        for i, account in enumerate(accounts):
            account_rows.append({"name": account["name"], "owner": form.get(f"account_owner_{i}", account["owner"]),
                                 "initial_balance": form.get(f"account_balance_{i}", account["initial_balance"])})
        save_accounts(db, account_rows)
        card_rows = []
        for i, card in enumerate(cards):
            card_rows.append({"name": card["name"], "bank": form.get(f"card_bank_{i}", card["bank"]),
                              "limit_amount": form.get(f"card_limit_{i}", card["limit_amount"])})
        save_cards(db, card_rows)
        for kind, field in (("categoria", "categories"), ("membro", "members")):
            raw = str(form.get(field, ""))
            save_lookup(db, kind, raw.splitlines())
        db.commit()
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.get("/backup.json")
def backup(request: Request):
    with get_session_factory()() as db:
        payload = backup_payload(db)
    content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    filename = f"controle_financeiro_backup_{date.today().isoformat()}.json"
    return StreamingResponse(iter([content]), media_type="application/json; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "private, no-store"})


@router.get("/transactions.csv")
def transactions_csv(request: Request):
    with get_session_factory()() as db:
        content = csv_export(db)
    return Response(content=content, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="movimentacoes_familia.csv"', "Cache-Control": "private, no-store"})

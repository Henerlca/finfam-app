"""Rotas da revisão cotidiana integrada.

O router é incluído pelo ``main.py`` pelo agente responsável pela composição
principal. Ele não substitui ``/cash/status``: a baixa direta de um item usa
esse endpoint existente, enquanto esta rota trata apenas a ação explícita em
lote para itens comprovadamente seguros.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.auth import verify_csrf
from app.database import get_session_factory
from app.repository import cashflow_statuses, legacy_statuses, read_snapshot, save_cashflow_status
from app.services.daily_review import (
    FAMILY,
    build_return_url,
    monthly_cash_items,
    normalize_month,
    normalize_person,
    normalize_return_to,
    related_transfer_items,
    safe_batch_selection,
    status_change_block_reason,
    validate_item_confirmation,
    validate_paid_on,
)

router = APIRouter()
MAX_BATCH_ITEMS = 50


async def _checked_form(request: Request):
    form = await request.form()
    session = getattr(request.state, "family_session", None) or {}
    if not verify_csrf(session.get("csrf"), str(form.get("_csrf", ""))):
        raise HTTPException(status_code=403, detail="Formulário expirado. Atualize a página e tente novamente.")
    return form


def _keys_from_form(form: Any) -> list[str]:
    # Starlette's FormData supports getlist; accepting both names keeps the
    # contract friendly to a future custom checkbox component.
    keys = list(form.getlist("item_key"))
    if not keys:
        keys = list(form.getlist("item_keys"))
    return [str(key).strip() for key in keys if str(key).strip()]


def _redirect_after_batch(form: Any, *, month: str, person: str, count: int, undone: bool = False, last_key: str = "") -> RedirectResponse:
    path = normalize_return_to(form.get("return_to"), "/today")
    filters = {
        "from": form.get("from"),
        "to": form.get("to"),
        "q": form.get("q"),
        "bank": form.get("bank"),
        "state": form.get("state"),
        "status": form.get("status"),
    }
    location = build_return_url(
        path,
        month=month,
        person=person,
        horizon=form.get("horizon", form.get("_context_horizon", "12")),
        **filters,
    )
    params = {"undone": str(count)} if undone else {"saved": str(count)}
    if last_key:
        params["saved_item"] = last_key
    joiner = "&" if "?" in location else "?"
    return RedirectResponse(f"{location}{joiner}{urlencode(params)}#daily-review", status_code=303)


@router.post("/daily/review/batch")
async def review_batch(request: Request):
    """Confirm checked exact-date rows (or undo checked rows) explicitly.

    The selected keys are resolved against a fresh current-month
    ``cashflow_items`` list. A stale, forged, projected, or month-estimate key
    therefore produces no database write. Card invoices are the one deliberate
    exception to exact dates: the invoice itself is a reliable cashflow item,
    even though the underlying purchases have separate dates.
    """
    form = await _checked_form(request)
    try:
        month = normalize_month(form.get("month"))
        person = normalize_person(form.get("person", FAMILY))
        normalize_return_to(form.get("return_to"), "/today")
        action = str(form.get("action", "confirm")).strip().casefold()
        if action not in ("confirm", "undo"):
            raise ValueError("Ação de revisão inválida.")
        keys = _keys_from_form(form)
        if not keys:
            raise ValueError("Selecione ao menos um item para continuar.")
        if len(keys) > MAX_BATCH_ITEMS:
            raise ValueError(f"Selecione no máximo {MAX_BATCH_ITEMS} itens por vez.")
        paid_on = validate_paid_on(form.get("paid_on")) if action == "confirm" else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    with get_session_factory()() as db:
        snapshot = read_snapshot(db)
        blocked = status_change_block_reason(month, bool(snapshot[5].get(month, False)))
        if blocked:
            raise HTTPException(status_code=409, detail=blocked)
        statuses = cashflow_statuses(db, month)
        legacy_rows = legacy_statuses(db, month)
        current = monthly_cash_items(
            snapshot,
            month,
            person,
            statuses=statuses,
            legacy_rows=legacy_rows,
        )
        family_current = monthly_cash_items(
            snapshot, month, FAMILY, statuses=statuses, legacy_rows=legacy_rows,
        )
        selection = safe_batch_selection(current, keys, action=action)
        if selection["missing"]:
            raise HTTPException(status_code=404, detail="Uma ou mais movimentações não existem mais neste mês.")
        if not selection["selected"]:
            raise HTTPException(status_code=400, detail="Selecione ao menos um item para continuar.")
        if action == "confirm" and selection["unsafe"]:
            raise HTTPException(
                status_code=400,
                detail="A confirmação em lote só aceita datas exatas e faturas. Revise previsões individualmente.",
            )
        if action == "confirm":
            try:
                for item in selection["selected"]:
                    if not item.get("paid"):
                        validate_item_confirmation(item, paid_on)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None

        writes: list[tuple[str, bool, str | None, str]] = []
        selected_items = selection["selected"]
        if action == "confirm":
            for item in selected_items:
                if item.get("paid"):
                    continue
                for related in related_transfer_items(family_current, item):
                    if related.get("paid"):
                        continue
                    writes.append((str(related["item_key"]), True, paid_on, str(related.get("description") or "")))
        else:
            for item in selected_items:
                if not item.get("paid"):
                    continue
                for related in related_transfer_items(family_current, item):
                    writes.append((str(related["item_key"]), False, None, str(related.get("description") or "")))

        # Dedupe transfer pairs when both sides were checked.
        deduped: list[tuple[str, bool, str | None, str]] = []
        seen: set[str] = set()
        for write in writes:
            if write[0] in seen:
                continue
            seen.add(write[0])
            deduped.append(write)
        for item_key, paid, item_paid_on, item_label in deduped:
            save_cashflow_status(db, month, item_key, paid, item_paid_on, item_label=item_label)
        if deduped:
            db.commit()

    return _redirect_after_batch(
        form,
        month=month,
        person=person,
        count=len(deduped),
        undone=action == "undo",
        last_key=deduped[-1][0] if deduped else "",
    )


# Name exposed for the main module's explicit router-wiring contract.
daily_router = router

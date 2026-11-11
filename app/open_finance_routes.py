from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select

from app.database import get_session_factory
from app.models import (
    ActivityEvent, CashflowStatus, HeartbeatTask, MonthClosure,
    OpenFinanceAccountSnapshot, OpenFinanceBill, OpenFinanceItem,
    OpenFinanceTransaction, Transaction,
)
from app.repository import (
    cashflow_statuses, legacy_statuses, read_snapshot, record_activity_event,
    save_cashflow_status, save_settings,
)
from app.routes import _nav_context, checked_form, render
from app.services.heartbeat_auth import HeartbeatAuthError, resolve_platform_task, verify_platform_jwt
from app.services.open_finance import (
    ITEM_DEFINITIONS, configured_items, has_run_today_in_brasilia, pluggy_ready,
    sync_configured_items,
)
from app.services.finance_core import (
    FAMILY, apply_payment_statuses, as_date, cashflow_items, cashflow_summary,
    month_from_id, month_id, month_key, month_start, opening_account_balances,
)
from app.services.daily_review import (
    current_brazil_day, status_change_block_reason, validate_item_confirmation,
    validate_paid_on,
)
from app.services.occurrence_edit import (
    OCCURRENCE_TYPES, OccurrenceEditError, edit_occurrence,
)
from app.services.open_finance_matching import (
    enforce_one_to_one, match_bank_transaction, match_card_purchase,
)
from app.services.open_finance_balance import source_creation_block_reason
from app.services.open_finance_reconciliation import (
    build_invoice_groups, clean_mappings, credit_used, external_card_closing_day,
    latest_provider_closing_date, money, project_card_cycle, suggested_internal_name,
)

router = APIRouter()


def _decimal_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _brazil_datetime_label(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(ZoneInfo("America/Sao_Paulo")).strftime("%d/%m/%Y às %H:%M")
    except (TypeError, ValueError):
        return text


def _money_difference(external, finfam):
    left = money(external)
    right = money(finfam)
    if left is None or right is None:
        return None
    return (left - right).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _valid_mapping(account, mapping, local_account_by_name, local_card_by_name):
    if account is None or not mapping:
        return None
    kind = str(getattr(account, "account_type", "") or "").upper()
    names = local_account_by_name if kind == "BANK" else local_card_by_name if kind == "CREDIT" else {}
    if mapping.get("kind") != kind or mapping.get("name") not in names:
        return None
    return mapping


def _review_rows_near(db, seed_rows, mappings, account_map, local_account_by_name):
    linked_ids = [
        account_id for account_id, mapping in mappings.items()
        if account_id in account_map
        and str(account_map[account_id].account_type or "").upper() == "BANK"
        and mapping.get("kind") == "BANK"
        and mapping.get("name") in local_account_by_name
    ]
    linked_seed_dates = [
        parsed for row in seed_rows
        if row.account_id in linked_ids
        and (parsed := as_date(row.occurred_on)) is not None
    ]
    if not linked_ids or not linked_seed_dates:
        return []
    first = (min(linked_seed_dates) - timedelta(days=4)).isoformat()
    last = (max(linked_seed_dates) + timedelta(days=4)).isoformat()
    return list(db.scalars(select(OpenFinanceTransaction).where(
        OpenFinanceTransaction.is_current.is_(True),
        OpenFinanceTransaction.account_id.in_(linked_ids),
        OpenFinanceTransaction.occurred_on >= first,
        OpenFinanceTransaction.occurred_on <= last,
    ).order_by(OpenFinanceTransaction.occurred_on.desc(), OpenFinanceTransaction.last_seen_at.desc())))


def _review_history(db, rows):
    ids = list({str(row.transaction_id) for row in rows if row.transaction_id})
    if not ids:
        return {}
    events = db.scalars(select(ActivityEvent).where(
        ActivityEvent.entity_type == "open_finance_review",
        ActivityEvent.action == "create",
        ActivityEvent.entity_id.in_(ids),
    ).order_by(ActivityEvent.id.desc()))
    history = {}
    for event in events:
        history.setdefault(str(event.entity_id), event.after or {})
    return history


def _bank_review_matches(
    db, seed_rows, account_map, mappings, txs, local_accounts, local_cards,
    closed_months, today,
):
    account_names = {str(row.get("name") or ""): row for row in local_accounts}
    card_names = {str(row.get("name") or ""): row for row in local_cards}
    rows = _review_rows_near(db, seed_rows, mappings, account_map, account_names)
    history = _review_history(db, rows)
    local_by_id = {str(row.get("id")): row for row in txs}
    month_cache = {}
    matches = {}
    for external in rows:
        external_id = str(external.transaction_id)
        if external_id in history:
            matches[external_id] = {
                "state": "reviewed", "decision": str(history[external_id].get("decision") or "reviewed"),
                "candidates": [],
            }
            continue
        account = account_map.get(external.account_id)
        mapping = _valid_mapping(account, mappings.get(external.account_id), account_names, card_names)
        if account is None or str(account.account_type or "").upper() != "BANK" or mapping is None:
            continue
        external_date = as_date(external.occurred_on)
        if external_date is None:
            continue
        candidate_months = {
            month_start(external_date + timedelta(days=offset)) for offset in range(-4, 5)
        }
        eligible_items = []
        for month in sorted((value for value in candidate_months if value is not None)):
            key = month_key(month)
            if key not in month_cache:
                raw = cashflow_items(txs, local_cards, local_accounts, month, FAMILY)
                reconciled = apply_payment_statuses(
                    raw, legacy_statuses(db, month), cashflow_statuses(db, month),
                )
                block = status_change_block_reason(
                    key, bool(closed_months.get(key, False)), today=today,
                )
                for item in reconciled:
                    local = local_by_id.get(str(item.get("transaction_id"))) or {}
                    item["category"] = str(local.get("category") or "")
                    item["confirmation_block_reason"] = block
                month_cache[key] = reconciled
            eligible_items.extend(month_cache[key])
        matches[external_id] = match_bank_transaction(
            external, account, mapping, eligible_items, today=today,
        )
    return enforce_one_to_one(matches), history, rows


def _card_review_matches(
    db, seed_rows, all_card_rows, account_map, mappings, transactions,
    local_cards, settings, today,
):
    local_card_names = {str(row.get("name") or ""): row for row in local_cards}
    seed_account_ids = {
        row.account_id for row in seed_rows
        if (account := account_map.get(row.account_id)) is not None
        and str(account.account_type or "").upper() == "CREDIT"
        and _valid_mapping(account, mappings.get(row.account_id), {}, local_card_names)
    }
    seed_dates = [
        parsed for row in seed_rows if row.account_id in seed_account_ids
        and (parsed := as_date(row.occurred_on)) is not None
    ]
    if not seed_account_ids or not seed_dates:
        return {}
    lower, upper = min(seed_dates) - timedelta(days=4), max(seed_dates) + timedelta(days=4)
    relevant = [
        row for row in all_card_rows if row.account_id in seed_account_ids
        and (parsed := as_date(row.occurred_on)) is not None and lower <= parsed <= upper
    ]
    history = _review_history(db, relevant)
    matches = {}
    for external in relevant:
        external_id = str(external.transaction_id)
        if external_id in history:
            matches[external_id] = {
                "state": "reviewed", "decision": str(history[external_id].get("decision") or "reviewed"),
                "candidates": [],
            }
            continue
        account = account_map.get(external.account_id)
        mapping = _valid_mapping(account, mappings.get(external.account_id), {}, local_card_names)
        matches[external_id] = match_card_purchase(
            external, account, mapping, transactions, local_cards, settings, today=today,
        )
    return enforce_one_to_one(matches)


def _status_label(status: str, configured: bool, last_error: str = "") -> tuple[str, str]:
    if not configured:
        return "Não configurado", ""
    if status == "UPDATED" and last_error:
        return "Dados disponíveis · coleta não renovada", last_error
    labels = {
        "UPDATED": ("Atualizado", ""),
        "UPDATING": ("Atualizando", "A Pluggy ainda está coletando os dados."),
        "WAITING_USER_INPUT": ("Ação no banco necessária", "A conexão precisa de uma ação no banco. Os dados anteriores seguem como referência."),
        "LOGIN_ERROR": ("Conexão precisa de atenção", "O banco recusou a atualização. Revise a conexão no banco/Pluggy."),
        "OUTDATED": ("Dados podem estar desatualizados", "A Pluggy ainda não concluiu uma coleta recente."),
        "ERROR": ("Atualização indisponível", last_error or "Falha ao consultar esta conexão."),
        "UNKNOWN": ("Aguardando primeira atualização", "Os dados aparecerão depois da primeira sincronização."),
    }
    return labels.get(status, (status.replace("_", " ").title(), last_error))


@router.get("/open-finance")
def open_finance_page(request: Request):
    configured_keys = {item["key"] for item in configured_items()}
    nav = _nav_context(request)
    current_day = current_brazil_day().isoformat()
    current_month_start = date.fromisoformat(current_day).replace(day=1)
    selected_month = month_start(nav["month"]) or current_month_start

    with get_session_factory()() as db:
        statuses = {row.item_key: row for row in db.scalars(select(OpenFinanceItem))}
        connections = []
        for key, owner, institution, _ in ITEM_DEFINITIONS:
            record = statuses.get(key)
            status = str(record.status if record else "UNKNOWN")
            label, message = _status_label(status, key in configured_keys, record.last_error_message if record else "")
            connections.append({
                "key": key, "owner": owner, "institution": institution,
                "configured": key in configured_keys, "status": status,
                "status_label": label, "message": message,
                "synced_at": record.synced_at if record else None,
                "provider_updated_at": record.provider_updated_at if record else None,
                "synced_at_label": _brazil_datetime_label(record.synced_at if record else None),
                "provider_updated_at_label": _brazil_datetime_label(record.provider_updated_at if record else None),
                "next_auto_sync_at_label": _brazil_datetime_label(record.next_auto_sync_at if record else None),
            })

        txs, local_accounts, local_cards, _, settings, closed = read_snapshot(db)
        mappings = clean_mappings(settings.get("open_finance_mappings"))
        snapshots = list(db.scalars(select(OpenFinanceAccountSnapshot).order_by(
            OpenFinanceAccountSnapshot.snapshot_date.desc(), OpenFinanceAccountSnapshot.synced_at.desc(),
        )))
        latest_accounts = {}
        for account in snapshots:
            latest_accounts.setdefault(account.account_id, account)
        all_external_accounts = sorted(
            latest_accounts.values(), key=lambda row: (row.owner, row.institution, row.name.casefold()),
        )
        credit_account_ids = [
            row.account_id for row in all_external_accounts if str(row.account_type or "").upper() == "CREDIT"
        ]
        credit_transactions = list(db.scalars(select(OpenFinanceTransaction).where(
            OpenFinanceTransaction.account_id.in_(credit_account_ids),
            OpenFinanceTransaction.is_current.is_(True),
        )))
        account_map = {row.account_id: row for row in all_external_accounts}
        bill_rows = list(db.scalars(select(OpenFinanceBill).order_by(OpenFinanceBill.due_date.desc())))
        external_rows = list(db.scalars(select(OpenFinanceTransaction).where(
            OpenFinanceTransaction.is_current.is_(True),
        ).order_by(OpenFinanceTransaction.occurred_on.desc(), OpenFinanceTransaction.last_seen_at.desc()).limit(12)))

        # Reuse the same confirmed-cash calculation as the Caixa page. The
        # opening balance includes the FinFam plan through prior months.
        statuses_for_month = cashflow_statuses(db, selected_month)
        legacy_for_month = legacy_statuses(db, selected_month)
        family_items = apply_payment_statuses(
            cashflow_items(txs, local_cards, local_accounts, selected_month, FAMILY),
            legacy_for_month, statuses_for_month,
        )
        opening = opening_account_balances(
            txs, local_accounts, settings.get("base_month"), selected_month, cards=local_cards,
        )
        cash_summary = cashflow_summary(
            family_items, local_accounts, opening, FAMILY, account_items=family_items,
        )
        local_balance_by_name = {
            str(row.get("account") or ""): row for row in cash_summary.get("account_balances", [])
        }
        local_account_by_name = {str(row.get("name") or ""): row for row in local_accounts}
        local_card_by_name = {str(row.get("name") or ""): row for row in local_cards}

        bank_accounts = []
        credit_accounts = []
        for account in all_external_accounts:
            record = statuses.get(account.item_key)
            provider_time = record.provider_updated_at if record else None
            last_error = record.last_error_message if record else ""
            mapping = mappings.get(account.account_id)
            kind = str(account.account_type or "").upper()
            options = local_accounts if kind == "BANK" else local_cards if kind == "CREDIT" else []
            expected_kind = kind
            if mapping and (mapping.get("kind") != expected_kind or mapping.get("name") not in {
                str(option.get("name") or "") for option in options
            }):
                mapping = None
            source_info = {
                "row": account,
                "mapping_name": mapping.get("name") if mapping else "",
                "mapping_kind": expected_kind,
                "mapping_options": options,
                "mapping_suggestion": suggested_internal_name(
                    str(account.institution), kind, local_accounts, local_cards,
                ),
                "source_updated_at": provider_time,
                "source_updated_at_label": _brazil_datetime_label(provider_time),
                "source_error": last_error,
                "local_snapshot_date": account.snapshot_date,
                "currency_code": str(account.currency_code or "BRL").upper(),
                "stale": bool(last_error) or str(account.snapshot_date) != current_day,
            }
            if kind == "BANK":
                linked = local_balance_by_name.get(mapping["name"]) if mapping else None
                local_balance = money(linked.get("live")) if linked else None
                external_balance = money(account.balance)
                bank_accounts.append({
                    **source_info,
                    "bank_balance": external_balance,
                    "finfam_balance": local_balance,
                    "difference": _money_difference(external_balance, local_balance),
                })
            elif kind == "CREDIT":
                credit_accounts.append({
                    **source_info,
                    "credit_used": credit_used(account),
                    "credit_limit": money(account.credit_limit),
                    "available_limit": money(account.available_credit_limit),
                })

        bank_total_values = [
            row["bank_balance"] for row in bank_accounts
            if row["bank_balance"] is not None and row["currency_code"] == "BRL"
        ]
        credit_used_values = [
            row["credit_used"] for row in credit_accounts
            if row["credit_used"] is not None and row["currency_code"] == "BRL"
        ]
        bank_total = sum(bank_total_values, Decimal("0.00")) if bank_total_values else None
        credit_total = sum(credit_used_values, Decimal("0.00")) if credit_used_values else None
        following_month = (
            date(selected_month.year + 1, 1, 1) if selected_month.month == 12
            else date(selected_month.year, selected_month.month + 1, 1)
        )
        projection_months = {following_month}
        if selected_month >= current_month_start:
            projection_months.add(selected_month)
        projection_months = sorted(projection_months)
        projected_cycles = {}
        source_updated_by_account = {}
        for card_account in credit_accounts:
            account = card_account["row"]
            account_id = str(account.account_id)
            provider_close = latest_provider_closing_date(bill_rows, account_id, following_month)
            closing_day, closing_day_source = external_card_closing_day(
                account_id, mappings, local_cards, settings, provider_closing_date=provider_close,
            )
            item_status = statuses.get(account.item_key)
            source_updated_by_account[account_id] = item_status.provider_updated_at if item_status else None
            for invoice_month in projection_months:
                cycle = project_card_cycle(
                    credit_transactions, account_id, invoice_month, closing_day,
                )
                cycle["closing_day_source"] = closing_day_source
                cycle["provider_closing_date"] = provider_close
                if cycle["projected_total"] is not None or cycle["credit_count"] or cycle["unclassified_count"]:
                    projected_cycles[(account_id, month_key(invoice_month))] = cycle

        invoice_groups = build_invoice_groups(
            [row["row"] for row in credit_accounts], bill_rows, mappings,
            local_cards, txs, closed, settings.get("base_month"), selected_month,
            projected_cycles=projected_cycles,
            source_updated_by_account=source_updated_by_account,
        )

        bank_review_matches, _history, _review_rows = _bank_review_matches(
            db, external_rows, account_map, mappings, txs, local_accounts, local_cards,
            closed, date.fromisoformat(current_day),
        )
        card_review_matches = _card_review_matches(
            db, external_rows, credit_transactions, account_map, mappings, txs,
            local_cards, settings, date.fromisoformat(current_day),
        )
        feed_history = _review_history(db, external_rows)
        external_transactions = []
        for row in external_rows:
            account = account_map.get(row.account_id)
            mapping = _valid_mapping(
                account, mappings.get(row.account_id), local_account_by_name, local_card_by_name,
            )
            external_id = str(row.transaction_id)
            review = bank_review_matches.get(external_id)
            if external_id in feed_history:
                review = {
                    "state": "reviewed",
                    "decision": str(feed_history[external_id].get("decision") or "reviewed"),
                }
            suggestion = None
            kind = str(account.account_type or "").upper() if account else ""
            if kind == "CREDIT" and mapping is None:
                suggestion = {"unlinked": True}
            elif kind == "CREDIT":
                card_match = card_review_matches.get(external_id)
                if card_match and card_match.get("state") in {"candidate", "ambiguous"}:
                    suggestion = {
                        "ambiguous": card_match.get("state") == "ambiguous",
                        "candidates": card_match.get("candidates", []),
                        "candidate": card_match.get("candidate"),
                        "message": card_match.get("message", ""),
                    }
            if kind == "BANK" and mapping is None and review is None:
                review = {"state": "unavailable", "reason": "unlinked", "candidates": []}
            direction = str(row.transaction_type or "").strip().upper()
            status = str(row.status or "").strip().upper()
            registration_block_reason = source_creation_block_reason(row)
            registration_type = ""
            registration_installments = 0
            if kind == "BANK" and direction == "DEBIT":
                registration_type = "Débito/Pix"
            elif kind == "BANK" and direction == "CREDIT":
                registration_type = "Receita"
            elif kind == "CREDIT" and direction == "DEBIT":
                try:
                    registration_installments = int(row.total_installments or 1)
                except (TypeError, ValueError):
                    registration_installments = 0
                if registration_installments == 1:
                    registration_type = "Crédito à vista"
                elif 2 <= registration_installments <= 120:
                    registration_type = "Parcelado"
            review_state = str((review or {}).get("state") or "")
            has_card_suggestion = bool(
                isinstance(suggestion, dict) and (suggestion.get("candidate") or suggestion.get("ambiguous"))
            )
            can_register = bool(
                status == "POSTED" and str(row.currency_code or "").strip().upper() == "BRL"
                and mapping and registration_type and not registration_block_reason
                and review_state in {"", "none", "unavailable"} and not has_card_suggestion
            )
            registration_url = ""
            if can_register:
                params = {
                    "month": month_key(selected_month), "person": nav["person"], "horizon": nav["horizon"],
                    "return_page": "open-finance", "source_type": registration_type,
                    "source_date": str(row.occurred_on or ""),
                    "source_description": str(row.description or "")[:191],
                    "source_account": str(mapping.get("name") or "") if mapping else "",
                    "source_member": str(account.owner or "") if account else "",
                }
                amount = money(row.amount)
                if registration_type != "Parcelado" and amount is not None:
                    params["source_amount"] = format(abs(amount), "f")
                if registration_type == "Parcelado":
                    params["source_installments"] = str(registration_installments)
                registration_url = "/transactions/new?" + urlencode(params)
            candidate_details = review.get("candidate") if isinstance(review, dict) else None
            can_adjust = bool(
                isinstance(candidate_details, dict)
                and candidate_details.get("type") in OCCURRENCE_TYPES
            )
            external_transactions.append({
                "row": row,
                "account_name": account.name if account else "Conta bancária",
                "institution": account.institution if account else "",
                "owner": account.owner if account else "",
                "account_type": kind,
                "mapped_name": mapping.get("name") if mapping else "",
                "currency_code": str(row.currency_code or "").strip().upper(),
                "suggestion": suggestion,
                "review": review,
                "registration_block_reason": registration_block_reason,
                "can_register": can_register,
                "registration_url": registration_url,
                "can_adjust": can_adjust,
                "status_label": "Pendente no banco" if row.status.upper() == "PENDING" else "Lançada pelo banco" if row.status.upper() == "POSTED" else row.status,
            })

    return render(
        request, "open_finance.html", "Open Finance",
        connections=connections, bank_accounts=bank_accounts, credit_accounts=credit_accounts,
        bank_total=bank_total, bank_total_count=len(bank_total_values),
        credit_total=credit_total, credit_total_count=len(credit_used_values),
        invoice_groups=invoice_groups, external_transactions=external_transactions,
        ready=pluggy_ready(), has_configured=bool(configured_keys), today=current_day,
        selected_month=selected_month, selected_month_label=month_key(selected_month),
        cash_basis_label="Abertura por conta + baixas confirmadas no mês; a abertura encadeia o planejamento anterior.",
        nav=nav,
        mapping_result=request.query_params.get("mapped", ""),
        sync_result=request.query_params.get("sync", ""),
        review_result=request.query_params.get("review", ""),
    )


@router.post("/open-finance/review")
async def review_open_finance_transaction(request: Request):
    form = await checked_form(request)
    transaction_id = str(form.get("transaction_id", "")).strip()
    action = str(form.get("action", "")).strip()
    if not transaction_id or action not in {"confirm", "adjust_confirm", "ignore"}:
        raise HTTPException(status_code=400, detail="Revisão inválida.")
    nav = _nav_context(request, form)
    today = current_brazil_day()
    result_label = ""

    with get_session_factory()() as db:
        external = db.scalar(select(OpenFinanceTransaction).where(
            OpenFinanceTransaction.transaction_id == transaction_id,
        ).with_for_update().execution_options(populate_existing=True))
        if external is None or not external.is_current:
            raise HTTPException(status_code=404, detail="Transação Open Finance não encontrada ou desatualizada.")
        existing = db.scalar(select(ActivityEvent).where(
            ActivityEvent.entity_type == "open_finance_review",
            ActivityEvent.entity_id == transaction_id,
            ActivityEvent.action == "create",
        ).order_by(ActivityEvent.id.desc()).limit(1))
        if existing is not None:
            result_label = "already"
        elif action == "ignore":
            record_activity_event(
                db, "create", "open_finance_review", transaction_id, None,
                {"decision": "ignored", "occurred_on": str(external.occurred_on),
                 "currency": str(external.currency_code or "").upper(),
                 "external_amount": str(external.amount), "reviewer": "sessão familiar"},
                undo_window=timedelta(0),
            )
            db.commit()
            result_label = "ignored"
        else:
            if str(external.status or "").upper() != "POSTED":
                raise HTTPException(status_code=409, detail="Só uma transação já lançada pelo banco pode confirmar uma baixa.")
            if str(external.currency_code or "").strip().upper() != "BRL":
                raise HTTPException(status_code=409, detail="A aprovação exige moeda BRL; nenhuma conversão cambial é aplicada.")
            paid_on = validate_paid_on(external.occurred_on, today=today)
            if not paid_on:
                raise HTTPException(status_code=400, detail="Data bancária inválida para baixa.")

            txs, local_accounts, local_cards, _, settings, closed = read_snapshot(db)
            mappings = clean_mappings(settings.get("open_finance_mappings"))
            snapshots = list(db.scalars(select(OpenFinanceAccountSnapshot).order_by(
                OpenFinanceAccountSnapshot.snapshot_date.desc(), OpenFinanceAccountSnapshot.synced_at.desc(),
            )))
            account_map = {}
            for account in snapshots:
                account_map.setdefault(account.account_id, account)
            matches, history, _rows = _bank_review_matches(
                db, [external], account_map, mappings, txs, local_accounts, local_cards,
                closed, today,
            )
            match = matches.get(transaction_id)
            if transaction_id in history or not match or match.get("state") != "candidate" or not match.get("can_confirm"):
                raise HTTPException(status_code=409, detail="A correspondência mudou, está ambígua ou não pode ser confirmada. Atualize a página e revise novamente.")
            candidate = match["candidate"]
            target_month = str(candidate.get("month_key") or "")
            target_id = candidate.get("transaction_id")
            try:
                target_id = int(target_id)
            except (TypeError, ValueError):
                raise HTTPException(status_code=409, detail="O lançamento FinFam não pode ser confirmado com segurança.") from None

            transaction = db.scalar(select(Transaction).where(Transaction.id == target_id)
                .with_for_update().execution_options(populate_existing=True))
            if transaction is None or transaction.archived:
                raise HTTPException(status_code=409, detail="O lançamento FinFam mudou; atualize e revise novamente.")
            month_row = db.scalar(select(MonthClosure.closed).where(
                MonthClosure.month == target_month,
            ).with_for_update())
            if status_change_block_reason(target_month, bool(month_row), today=today):
                raise HTTPException(status_code=409, detail="Competência futura ou fechada; reabra o mês ou aguarde para confirmar.")
            status_row = db.scalar(select(CashflowStatus).where(
                CashflowStatus.month == target_month,
                CashflowStatus.item_key == str(candidate.get("item_key") or ""),
            ).with_for_update().execution_options(populate_existing=True))
            if status_row is not None and status_row.paid:
                raise HTTPException(status_code=409, detail="Este lançamento já foi confirmado no FinFam.")

            fresh_snapshot = read_snapshot(db)
            fresh_transactions = fresh_snapshot[0]
            target_local = next((row for row in fresh_transactions if int(row.get("id") or 0) == target_id), None)
            if target_local is None or target_local.get("archived"):
                raise HTTPException(status_code=409, detail="O lançamento FinFam mudou; atualize e revise novamente.")
            current_items = cashflow_items(
                fresh_transactions, fresh_snapshot[2], fresh_snapshot[1], target_month, FAMILY,
            )
            current_item = next((item for item in current_items if item.get("item_key") == candidate.get("item_key")), None)
            if current_item is None or abs(_decimal_float(current_item.get("amount")) - float(candidate["amount"])) > 0.001:
                raise HTTPException(status_code=409, detail="O valor ou a ocorrência mudou; atualize e revise novamente.")

            final_item_key = str(candidate["item_key"])
            final_item_label = str(candidate.get("description") or "")
            final_transaction_id = target_id
            planned_amount = candidate["amount"]
            actual_amount = candidate["external_amount"]
            decision = "confirmed"
            if action == "adjust_confirm":
                if str(target_local.get("type") or "") not in OCCURRENCE_TYPES:
                    raise HTTPException(status_code=409, detail="Este tipo de lançamento não aceita ajuste por competência. Edite-o no FinFam antes da baixa.")
                if candidate["difference"] == 0:
                    raise HTTPException(status_code=400, detail="Não há diferença para ajustar; use Confirmar baixa.")
                try:
                    edit_result = edit_occurrence(
                        db, target_id, target_month, str(actual_amount), expected_before=target_local,
                    )
                except OccurrenceEditError as exc:
                    db.rollback()
                    raise HTTPException(status_code=409, detail=str(exc)) from None
                target_segment = next((segment for segment in edit_result.segments
                                       if month_key(segment.get("start_month")) == target_month
                                       and month_key(segment.get("end_month")) == target_month), None)
                if target_segment is None:
                    db.rollback()
                    raise HTTPException(status_code=409, detail="O segmento ajustado não foi encontrado; nenhuma baixa foi gravada.")
                adjusted_transaction_id = int(target_segment["id"])
                after_edit = read_snapshot(db)
                adjusted_items = cashflow_items(
                    after_edit[0], after_edit[2], after_edit[1], target_month, FAMILY,
                )
                adjusted_item = next((item for item in adjusted_items
                                      if item.get("transaction_id") == adjusted_transaction_id), None)
                if adjusted_item is None:
                    db.rollback()
                    raise HTTPException(status_code=409, detail="A ocorrência ajustada não foi encontrada; nenhuma baixa foi gravada.")
                final_item_key = str(adjusted_item["item_key"])
                final_item_label = str(adjusted_item.get("description") or final_item_label)
                final_transaction_id = adjusted_transaction_id
                decision = "adjusted_and_confirmed"

            save_cashflow_status(
                db, target_month, final_item_key, True, paid_on,
                item_label=final_item_label,
            )
            record_activity_event(
                db, "create", "open_finance_review", transaction_id, None,
                {"decision": decision, "local_item_key": final_item_key,
                 "local_transaction_id": final_transaction_id,
                 "competence": target_month, "occurred_on": paid_on,
                 "currency": "BRL", "external_amount": str(actual_amount),
                 "planned_amount_before_review": str(planned_amount),
                 "difference": str(candidate["difference"]),
                 "reviewer": "sessão familiar"},
                undo_window=timedelta(0),
            )
            db.commit()
            result_label = "adjusted" if action == "adjust_confirm" else "confirmed"

    destination = "/open-finance?" + urlencode({**nav, "review": result_label})
    return RedirectResponse(destination, status_code=303)


@router.post("/open-finance/map")
async def save_open_finance_mapping(request: Request):
    form = await checked_form(request)
    account_id = str(form.get("account_id", "")).strip()
    internal_name = str(form.get("internal_name", "")).strip()
    if not account_id:
        raise HTTPException(status_code=400, detail="Conta Open Finance ausente.")
    nav = _nav_context(request, form)

    with get_session_factory()() as db:
        external = db.scalar(select(OpenFinanceAccountSnapshot).where(
            OpenFinanceAccountSnapshot.account_id == account_id,
        ).order_by(OpenFinanceAccountSnapshot.snapshot_date.desc()).limit(1))
        if external is None:
            raise HTTPException(status_code=404, detail="Conta externa não encontrada.")
        kind = str(external.account_type or "").upper()
        if kind not in {"BANK", "CREDIT"}:
            raise HTTPException(status_code=400, detail="Tipo de conta não pode ser vinculado com segurança.")
        txs, local_accounts, local_cards, _, settings, _ = read_snapshot(db)
        mappings = clean_mappings(settings.get("open_finance_mappings"))
        if internal_name:
            options = local_accounts if kind == "BANK" else local_cards
            valid_names = {str(row.get("name") or "") for row in options}
            if internal_name not in valid_names:
                raise HTTPException(status_code=400, detail="Selecione uma conta ou cartão compatível do FinFam.")
            duplicate = next((
                other_id for other_id, other in mappings.items()
                if other_id != account_id and other.get("kind") == kind and other.get("name") == internal_name
            ), None)
            if duplicate:
                raise HTTPException(status_code=409, detail="Essa conta/cartão FinFam já está vinculado a outra conta Pluggy.")
            mappings[account_id] = {"kind": kind, "name": internal_name}
        else:
            mappings.pop(account_id, None)
        save_settings(db, {"open_finance_mappings": mappings})
        db.commit()

    query = {**nav, "mapped": "saved"}
    return RedirectResponse(url=f"/open-finance?{urlencode(query)}", status_code=303)


@router.post("/open-finance/sync")
async def manual_open_finance_sync(request: Request):
    await checked_form(request)
    with get_session_factory()() as db:
        summary = sync_configured_items(db, refresh=False)
    if summary["configured"] == 0:
        suffix = "unavailable"
    elif summary["errors"] or summary["pending"]:
        suffix = "partial"
    else:
        suffix = "complete"
    nav = _nav_context(request)
    return RedirectResponse(url=f"/open-finance?{urlencode({**nav, 'sync': suffix})}", status_code=303)


@router.post("/api/scheduled/pluggy-sync")
async def scheduled_open_finance_sync(request: Request):
    token = request.cookies.get("app_session_id", "")
    try:
        claims = verify_platform_jwt(token)
        identity = resolve_platform_task(token)
    except HeartbeatAuthError:
        raise HTTPException(status_code=401, detail="Identidade Heartbeat inválida.") from None
    if identity.get("openId") != claims.get("openId"):
        raise HTTPException(status_code=401, detail="Identidade Heartbeat inválida.")
    now = datetime.now(timezone.utc)
    with get_session_factory()() as db:
        task = db.scalar(
            select(HeartbeatTask).where(HeartbeatTask.task_uid == str(identity["taskUid"])).with_for_update()
        )
        if task is None or not task.enabled or task.task_name != "pluggy-daily-read-sync":
            raise HTTPException(status_code=403, detail="Tarefa Heartbeat não registrada.")
        if has_run_today_in_brasilia(task.last_run_at, now):
            return JSONResponse({"ok": True, "skipped": "already_synced_today"})
        task.last_run_at = now.isoformat(timespec="seconds")
        db.commit()
        summary = sync_configured_items(db, refresh=False)
    # Retorno 2xx para que falhas de um banco não repitam em loop os Items já lidos.
    return JSONResponse({
        "ok": summary["configured"] > 0 and summary["errors"] == 0 and summary["pending"] == 0,
        "configured_items": summary["configured"],
        "successful_items": len([row for row in summary["items"] if row["ok"]]),
        "errors": summary["errors"],
        "pending": summary["pending"],
        "synced_accounts": summary["accounts"],
        "synced_transactions": summary["transactions"],
        "synced_bills": summary["bills"],
    })

from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import math
from typing import Any, Iterable

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.models import (
    Account, ActivityEvent, AppSetting, BudgetAdjustment, Card, CashflowStatus,
    LegacyStatus, LookupValue, MonthClosure, RestoreSnapshot, Transaction,
)
from app.services.finance_core import as_date, month_from_id, month_id, month_key, month_start

SETTING_DEFAULTS: dict[str, Any] = {
    "base_month": "2026-11-01",
    "reference_date": "2026-11-06",
    "forecast_end": "2030-12-01",
    "minimum_balance": 0.0,
    "fgts_salary_hener": 0.0,
    "fgts_salary_ju": 0.0,
    "fgts_rate": 0.08,
    "pot_order": [],
}


def _tx_dict(row: Transaction) -> dict[str, Any]:
    return {
        "id": row.id, "member": row.member, "category": row.category,
        "description": row.description, "type": row.type, "account_card": row.account_card,
        "amount": float(row.amount or 0), "total_amount": float(row.total_amount or 0),
        "installment_count": int(row.installment_count or 1),
        "installment_overrides": dict(row.installment_overrides or {}), "start_month": row.start_month,
        "end_month": row.end_month, "destination_account": row.destination_account,
        "linked_pot": row.linked_pot, "card_flag": int(row.card_flag or 0),
        "payment_bank": row.payment_bank, "transaction_date": row.transaction_date,
        "date_precision": row.date_precision, "archived": bool(row.archived),
    }


def read_snapshot(db: Session) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, list[str]], dict[str, Any], dict[str, bool]]:
    txs = [_tx_dict(r) for r in db.scalars(select(Transaction).order_by(Transaction.transaction_date.desc(), Transaction.id.desc()))]
    accounts = [{"name": r.name, "owner": r.owner, "initial_balance": float(r.initial_balance or 0)} for r in db.scalars(select(Account).order_by(Account.name))]
    cards = [{"name": r.name, "bank": r.bank, "limit_amount": float(r.limit_amount or 0)} for r in db.scalars(select(Card).order_by(Card.name))]
    lookups: dict[str, list[str]] = {}
    for row in db.scalars(select(LookupValue).order_by(LookupValue.kind, LookupValue.position, LookupValue.value)):
        lookups.setdefault(row.kind, []).append(row.value)
    settings = dict(SETTING_DEFAULTS)
    for row in db.scalars(select(AppSetting)):
        try:
            settings[row.key] = json.loads(row.value)
        except (ValueError, TypeError):
            settings[row.key] = row.value
    closed = {r.month: bool(r.closed) for r in db.scalars(select(MonthClosure))}
    return txs, accounts, cards, lookups, settings, closed


def _clean(value: Any) -> str:
    if value in (None, "-"):
        return ""
    return str(value).strip()


def _amount(value: Any) -> float:
    if value in (None, "", "-"):
        return 0.0
    if isinstance(value, str):
        text = value.strip().replace("R$", "").replace(" ", "")
        if "," in text:
            text = text.replace(".", "").replace(",", ".")
        value = text
    return float(value)


def _month_iso(value: Any) -> str:
    month = month_start(value)
    if not month:
        raise ValueError("Informe um mês válido.")
    return month.isoformat()


def _normalized_installment_overrides(raw: Any) -> dict[str, str]:
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict) or len(raw) > 2400:
        raise ValueError("Os ajustes de parcelas estão em formato inválido.")
    result: dict[str, str] = {}
    for raw_month, raw_amount in raw.items():
        try:
            normalized_month = month_start(raw_month)
            text = str(raw_amount or "0").strip().replace("R$", "").replace(" ", "")
            if "," in text:
                text = text.replace(".", "").replace(",", ".")
            amount = Decimal(text)
        except (TypeError, ValueError, InvalidOperation):
            raise ValueError("Os ajustes de parcelas estão em formato inválido.") from None
        if normalized_month is None or not amount.is_finite() or abs(amount) > Decimal("1000000000"):
            raise ValueError("O valor ajustado da parcela é inválido.")
        key = month_key(normalized_month)
        result[key] = format(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ".2f")
    return result


def _normalized_transaction(record: dict[str, Any], cards: list[Card]) -> dict[str, Any]:
    account = _clean(record.get("account_card"))
    card_map = {card.name: card.bank for card in cards}
    tx_type = _clean(record.get("type"))
    start = _month_iso(record.get("start_month"))
    end = _month_iso(record["end_month"]) if record.get("end_month") else (
        None if "end_month" in record and record["end_month"] is None else start
    )
    raw_date = record.get("transaction_date")
    if isinstance(raw_date, datetime):
        exact = raw_date.date()
    elif isinstance(raw_date, date):
        exact = raw_date
    else:
        text_date = _clean(raw_date)
        try:
            exact = date.fromisoformat(text_date) if len(text_date) == 10 else None
        except ValueError:
            exact = None
    if exact is None:
        raise ValueError("Informe uma data completa válida para a movimentação.")
    count = max(1, int(_amount(record.get("installment_count") or 1)))
    amount = _amount(record.get("amount"))
    total = _amount(record.get("total_amount") or 0) or (amount * count if tx_type == "Parcelado" else amount)
    installment_overrides = _normalized_installment_overrides(record.get("installment_overrides"))
    if (amount < 0 or total < 0) and (tx_type != "Crédito à vista" or account not in card_map or ((amount < 0) != (total < 0))):
        raise ValueError("Valores-base negativos só são permitidos para estorno de Crédito à vista vinculado a cartão.")
    if installment_overrides and (tx_type not in {"Crédito à vista", "Parcelado"} or account not in card_map):
        raise ValueError("Ajustes individuais só podem ser usados em parcelas vinculadas a um cartão.")
    if any(Decimal(value) < 0 for value in installment_overrides.values()) and tx_type != "Crédito à vista":
        raise ValueError("Ajustes negativos só são permitidos para Crédito à vista vinculado a cartão.")
    start_date = month_start(start)
    end_date = month_start(end) if end else None
    for override_month in installment_overrides:
        parsed_month = month_start(override_month)
        if parsed_month is None or start_date is None or parsed_month < start_date or (end_date is not None and parsed_month > end_date):
            raise ValueError("O ajuste individual precisa estar dentro do período ativo da compra.")
    return {
        "member": _clean(record.get("member")) or "Família",
        "category": _clean(record.get("category")),
        "description": _clean(record.get("description")),
        "type": tx_type,
        "account_card": account,
        "amount": amount,
        "total_amount": total,
        "installment_count": count,
        "installment_overrides": installment_overrides,
        "start_month": start,
        "end_month": end,
        "destination_account": _clean(record.get("destination_account")),
        "linked_pot": _clean(record.get("linked_pot")),
        "card_flag": int(account in card_map),
        "payment_bank": card_map.get(account, account),
        "transaction_date": exact.isoformat(),
        "date_precision": "recurrence" if _clean(record.get("date_precision")).casefold() == "recurrence" else "exact",
        "archived": bool(record.get("archived", False)),
    }


def insert_transaction(db: Session, record: dict[str, Any]) -> int:
    cards = list(db.scalars(select(Card)))
    values = _normalized_transaction(record, cards)
    row = Transaction(**values)
    db.add(row)
    db.flush()
    record_activity_event(db, "create", "transaction", str(row.id), None, _tx_dict(row),
                          undo={"kind": "delete", "transaction": _tx_dict(row), "cashflow_status": []})
    return int(row.id)


def update_transaction(
    db: Session,
    tx_id: int,
    record: dict[str, Any],
    expected_before: dict[str, Any] | None = None,
) -> None:
    row = db.scalar(
        select(Transaction).where(Transaction.id == int(tx_id)).with_for_update()
        .execution_options(populate_existing=True)
    )
    if not row or row.archived:
        raise ValueError("Lançamento não encontrado.")
    before = _tx_dict(row)
    if expected_before is not None and not _payload_equal(before, expected_before):
        raise ConcurrentEditError("O lançamento mudou durante a edição; atualize a página antes de salvar.")
    if "installment_overrides" not in record:
        record = {**record, "installment_overrides": before.get("installment_overrides", {})}
    values = _normalized_transaction(record, list(db.scalars(select(Card))))
    if (row.date_precision == "recurrence" and values["type"] in {"Receita", "Fixo", "Reserva", "Caixinha", "Saque Caixinha", "Transferência"}
        and values["end_month"] != values["start_month"]):
        values["date_precision"] = "recurrence"
    for key, value in values.items():
        setattr(row, key, value)
    db.flush()
    after = _tx_dict(row)
    record_activity_event(db, "update", "transaction", str(row.id), before, after,
                          undo={"kind": "restore", "before": before, "after": after})


def delete_transaction(
    db: Session,
    tx_id: int,
    expected_before: dict[str, Any] | None = None,
) -> None:
    row = db.scalar(
        select(Transaction).where(Transaction.id == int(tx_id)).with_for_update()
        .execution_options(populate_existing=True)
    )
    if not row or row.archived:
        raise ValueError("Lançamento não encontrado.")
    before = _tx_dict(row)
    if expected_before is not None and not _payload_equal(before, expected_before):
        raise ConcurrentEditError("O lançamento mudou durante a confirmação; atualize a página antes de excluir.")
    suffix = f":tx:{int(tx_id)}"
    statuses = [{"month": status.month, "item_key": status.item_key, "paid": bool(status.paid), "paid_on": status.paid_on}
                for status in db.scalars(select(CashflowStatus).where(or_(
        CashflowStatus.item_key.like(f"%{suffix}"),
        CashflowStatus.item_key.like(f"%{suffix}:out"),
        CashflowStatus.item_key.like(f"%{suffix}:in"),
    )))]
    db.execute(delete(CashflowStatus).where(or_(
        CashflowStatus.item_key.like(f"%{suffix}"),
        CashflowStatus.item_key.like(f"%{suffix}:out"),
        CashflowStatus.item_key.like(f"%{suffix}:in"),
    )))
    db.delete(row)
    db.flush()
    record_activity_event(db, "delete", "transaction", str(tx_id), before, None,
                          undo={"kind": "recreate", "transaction": before, "cashflow_status": statuses})


def stop_transaction_from_month(
    db: Session,
    tx_id: int,
    cutoff_month: Any,
    expected_before: dict[str, Any] | None = None,
) -> tuple[str, int]:
    """Stop an ongoing series prospectively without rewriting earlier months."""
    row = db.scalar(
        select(Transaction).where(Transaction.id == int(tx_id)).with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None or row.archived:
        raise ValueError("Lançamento não encontrado.")
    before = _tx_dict(row)
    if expected_before is not None and not _payload_equal(before, expected_before):
        raise ConcurrentEditError("O lançamento mudou durante a confirmação; atualize a página antes de excluir.")
    start = month_start(before.get("start_month"))
    end = month_start(before.get("end_month")) if before.get("end_month") else None
    cutoff = month_start(cutoff_month)
    repeated = before.get("type") == "Parcelado" or before.get("end_month") != before.get("start_month")
    if not repeated or start is None or cutoff is None:
        raise ValueError("Este lançamento não tem competências futuras para encerrar.")
    cutoff_index = month_id(cutoff)
    end_index = month_id(end) if end else None
    if cutoff_index is None or cutoff < start or (end is not None and cutoff > end):
        raise ValueError("Escolha uma competência dentro do período desta recorrência.")
    if bool(db.scalar(select(MonthClosure.closed).where(MonthClosure.month == month_key(cutoff)).with_for_update())):
        raise ValueError("A competência escolhida está fechada. Selecione uma competência aberta.")
    for closure in db.scalars(select(MonthClosure).where(MonthClosure.closed.is_(True)).with_for_update()):
        closed_index = month_id(month_start(closure.month))
        if closed_index is not None and closed_index >= cutoff_index and (end_index is None or closed_index <= end_index):
            raise ValueError("Há uma competência fechada a partir do mês escolhido. Selecione um mês aberto antes dela.")

    previous = month_from_id(cutoff_index - 1)
    if previous < start:
        delete_transaction(db, tx_id, expected_before=before)
        event_id = latest_activity_id(db, "transaction", tx_id, "delete")
        if event_id is None:
            raise RecoveryError("Não foi possível registrar o encerramento do lançamento.")
        return "deleted", event_id

    suffix = f":tx:{int(tx_id)}"
    related_statuses = list(db.scalars(select(CashflowStatus).where(or_(
        CashflowStatus.item_key.like(f"%{suffix}"),
        CashflowStatus.item_key.like(f"%{suffix}:out"),
        CashflowStatus.item_key.like(f"%{suffix}:in"),
    )).with_for_update()))
    future_statuses = []
    for status in related_statuses:
        status_index = month_id(month_start(status.month))
        if status_index is not None and status_index >= cutoff_index and (end_index is None or status_index <= end_index):
            future_statuses.append({"month": status.month, "item_key": status.item_key,
                                    "paid": bool(status.paid), "paid_on": status.paid_on})

    values = dict(before)
    values["end_month"] = previous.isoformat()
    values["installment_overrides"] = {
        key: amount for key, amount in (before.get("installment_overrides") or {}).items()
        if (month_id(month_start(key)) or 0) < cutoff_index
    }
    normalized = _normalized_transaction(values, list(db.scalars(select(Card))))
    for key, value in normalized.items():
        setattr(row, key, value)
    for item in future_statuses:
        status_key = {"month": str(item["month"]), "item_key": str(item["item_key"])}
        status_row = db.get(CashflowStatus, status_key)
        if status_row is not None:
            db.delete(status_row)
    db.flush()
    after = _tx_dict(row)
    event = record_activity_event(
        db, "update", "transaction", str(tx_id), before, after,
        undo={"kind": "prospective_end", "cutoff_month": month_key(cutoff), "before": before,
              "after": after, "future_statuses": future_statuses},
    )
    return "stopped", int(event.id)


def _cashflow_transaction_id(item_key: str) -> int | None:
    marker = ":tx:"
    if marker not in str(item_key):
        return None
    try:
        return int(str(item_key).split(marker, 1)[1].split(":", 1)[0])
    except (TypeError, ValueError):
        return None


def save_cashflow_status(db: Session, month: Any, item_key: str, paid: bool, paid_on: Any = None, *, item_label: str = "") -> None:
    key = month_key(month)
    if isinstance(paid_on, (date, datetime)):
        paid_on = paid_on.isoformat()
    tx_id = _cashflow_transaction_id(item_key)
    if tx_id is not None:
        db.scalar(select(Transaction).where(Transaction.id == tx_id).with_for_update()
                  .execution_options(populate_existing=True))
    row = db.scalar(select(CashflowStatus).where(
        CashflowStatus.month == key, CashflowStatus.item_key == item_key,
    ).with_for_update().execution_options(populate_existing=True))
    before = {"paid": bool(row.paid), "paid_on": row.paid_on} if row is not None else {"paid": None, "paid_on": None}
    normalized_paid_on = str(paid_on) if paid and paid_on else None
    if row is None:
        row = CashflowStatus(month=key, item_key=item_key, paid=bool(paid), paid_on=normalized_paid_on)
        db.add(row)
    else:
        row.paid, row.paid_on = bool(paid), normalized_paid_on
    after = {"paid": bool(paid), "paid_on": normalized_paid_on, "description": str(item_label or "")[:255]}
    if before != {"paid": after["paid"], "paid_on": after["paid_on"]}:
        record_activity_event(db, "update", "cashflow_status", f"{key}:{item_key}", before, after, undo_window=timedelta(0))


def cashflow_statuses(db: Session, month: Any) -> dict[str, dict[str, Any]]:
    key = month_key(month)
    return {r.item_key: {"paid": bool(r.paid), "paid_on": r.paid_on} for r in db.scalars(select(CashflowStatus).where(CashflowStatus.month == key))}


def legacy_statuses(db: Session, month: Any) -> list[dict[str, Any]]:
    key = month_key(month)
    return [{"account": r.account, "type": r.type, "member": r.member, "description": r.description, "amount": float(r.amount or 0), "paid": bool(r.paid)}
            for r in db.scalars(select(LegacyStatus).where(LegacyStatus.month == key).order_by(LegacyStatus.id))]


def set_month_closed(db: Session, month: Any, closed: bool) -> None:
    key = month_key(month)
    row = db.get(MonthClosure, key)
    before = bool(row.closed) if row is not None else False
    if row is None:
        db.add(MonthClosure(month=key, closed=bool(closed)))
    else:
        row.closed = bool(closed)
    if before != bool(closed):
        record_activity_event(db, "update", "month_closure", key, {"closed": before}, {"closed": bool(closed)}, undo_window=timedelta(0))


def save_settings(db: Session, values: dict[str, Any]) -> None:
    changed_keys: list[str] = []
    for key, value in values.items():
        if isinstance(value, (date, datetime)):
            value = value.isoformat()
        row = db.get(AppSetting, key)
        encoded = json.dumps(value, ensure_ascii=False)
        if row is None:
            db.add(AppSetting(key=key, value=encoded))
            changed_keys.append(str(key))
        else:
            if row.value != encoded:
                row.value = encoded
                changed_keys.append(str(key))
    if changed_keys:
        record_activity_event(db, "update", "settings", "family", {"keys": changed_keys}, {"keys": changed_keys}, undo_window=timedelta(0))


def _copy_reserve_segment(row: Transaction, start: date, end: date | None) -> Transaction:
    return Transaction(
        member=row.member, category=row.category, description=row.description, type=row.type,
        account_card=row.account_card, amount=float(row.amount or 0), total_amount=float(row.total_amount or 0),
        installment_count=int(row.installment_count or 1), start_month=start.isoformat(),
        end_month=end.isoformat() if end else None, destination_account=row.destination_account,
        linked_pot=row.linked_pot, card_flag=int(row.card_flag or 0), payment_bank=row.payment_bank,
        transaction_date=row.transaction_date, date_precision=row.date_precision, archived=False,
    )


def reallocate_future_reserve(db: Session, pot_name: str, monthly_amount: float, start_month: Any, end_month: Any) -> float:
    """Replace one reserve's future schedule while retaining past and post-horizon portions."""
    start, end = month_start(start_month), month_start(end_month)
    if start is None or end is None or start > end:
        raise ValueError("Informe um intervalo futuro válido.")
    amount = round(max(0.0, float(monthly_amount)), 2)
    rows = list(db.scalars(select(Transaction).where(
        Transaction.type == "Reserva", Transaction.description == pot_name, Transaction.archived.is_(False),
    ).order_by(Transaction.id)))
    active_at_start = []
    for row in rows:
        row_start, row_end = month_start(row.start_month), month_start(row.end_month) or date.max
        if row_start is not None and row_start <= start <= row_end:
            active_at_start.append(row)
    old_amount = sum(float(row.amount or 0) for row in active_at_start)

    next_after_end = month_from_id((month_id(end) or 0) + 1)
    previous_before_start = month_from_id((month_id(start) or 0) - 1)
    for row in rows:
        row_start, row_end = month_start(row.start_month), month_start(row.end_month) or date.max
        if row_start is None or row_end < start or row_start > end:
            continue
        extends_after_end = row_end > end
        if row_start < start:
            row.end_month = previous_before_start.isoformat()
            if extends_after_end:
                db.add(_copy_reserve_segment(row, next_after_end, None if row_end == date.max else row_end))
        else:
            row.archived = True
            if extends_after_end:
                db.add(_copy_reserve_segment(row, next_after_end, None if row_end == date.max else row_end))

    if amount > 0:
        by_allocation: dict[tuple[str, str, str, str, str], float] = {}
        template_by_allocation: dict[tuple[str, str, str, str, str], Transaction] = {}
        for row in active_at_start:
            allocation = (row.member or "Família", row.category or "", row.account_card or "", row.payment_bank or "", row.date_precision or "exact")
            by_allocation[allocation] = by_allocation.get(allocation, 0.0) + float(row.amount or 0)
            template_by_allocation.setdefault(allocation, row)
        weight_total = sum(max(0.0, value) for value in by_allocation.values())
        if weight_total > 0:
            shares = {allocation: round(amount * max(0.0, value) / weight_total, 2) for allocation, value in by_allocation.items()}
            last_allocation = next(reversed(shares))
            shares[last_allocation] = round(shares[last_allocation] + amount - sum(shares.values()), 2)
        else:
            shares = {("Família", "", "", "", "exact"): amount}
        months = max(1, (month_id(end) or 0) - (month_id(start) or 0) + 1)
        for allocation, share in shares.items():
            if share <= 0:
                continue
            member, category, account_card, payment_bank, date_precision = allocation
            template = template_by_allocation.get(allocation)
            reserve_date = start
            if date_precision in ("exact", "recurrence") and template:
                old_date = as_date(template.transaction_date)
                if old_date:
                    reserve_date = date(start.year, start.month, min(old_date.day, 28))
            db.add(Transaction(
                member=member, category=category, description=pot_name,
                type="Reserva", account_card=account_card,
                amount=share, total_amount=round(share * months, 2), installment_count=1,
                start_month=start.isoformat(), end_month=end.isoformat(), destination_account="",
                linked_pot="", card_flag=0, payment_bank=payment_bank,
                transaction_date=reserve_date.isoformat(),
                date_precision=date_precision if date_precision in ("exact", "recurrence") else "exact",
                archived=False,
            ))
    db.flush()
    return round(old_amount, 2)


def save_accounts(db: Session, rows: Iterable[dict[str, Any]]) -> None:
    changed: list[str] = []
    for item in rows:
        name = _clean(item.get("name"))
        if not name:
            continue
        row = db.get(Account, name)
        values = (_clean(item.get("owner")), _amount(item.get("initial_balance")))
        before = (_clean(row.owner), float(row.initial_balance or 0)) if row is not None else None
        if row is None:
            row = Account(name=name, owner="", initial_balance=0)
            db.add(row)
        row.owner, row.initial_balance = values
        if before != values:
            changed.append(name)
    if changed:
        record_activity_event(db, "update", "accounts", "family", {"names": changed}, {"names": changed}, undo_window=timedelta(0))


def save_cards(db: Session, rows: Iterable[dict[str, Any]]) -> None:
    changed: list[str] = []
    for item in rows:
        name = _clean(item.get("name"))
        if not name:
            continue
        row = db.get(Card, name)
        values = (_clean(item.get("bank")), _amount(item.get("limit_amount")))
        before = (_clean(row.bank), float(row.limit_amount or 0)) if row is not None else None
        if row is None:
            row = Card(name=name, bank="", limit_amount=0)
            db.add(row)
        row.bank, row.limit_amount = values
        if before != values:
            changed.append(name)
    if changed:
        record_activity_event(db, "update", "cards", "family", {"names": changed}, {"names": changed}, undo_window=timedelta(0))


def save_lookup(db: Session, kind: str, values: Iterable[str]) -> None:
    cleaned = list(dict.fromkeys(_clean(value) for value in values if _clean(value)))
    previous = [row.value for row in db.scalars(select(LookupValue).where(LookupValue.kind == kind).order_by(LookupValue.position, LookupValue.value))]
    if previous == cleaned:
        return
    db.execute(delete(LookupValue).where(LookupValue.kind == kind))
    for position, value in enumerate(cleaned):
        db.add(LookupValue(kind=kind, value=value, position=position))
    record_activity_event(db, "update", "lookup", kind, {"count": len(previous)}, {"count": len(cleaned)}, undo_window=timedelta(0))


def backup_payload(db: Session) -> dict[str, Any]:
    txs, accounts, cards, lookups, settings, closed = read_snapshot(db)
    statuses = [{"month": r.month, "item_key": r.item_key, "paid": bool(r.paid), "paid_on": r.paid_on}
                for r in db.scalars(select(CashflowStatus))]
    legacy = [{"month": r.month, "account": r.account, "type": r.type, "member": r.member,
               "description": r.description, "amount": float(r.amount or 0), "paid": bool(r.paid)}
              for r in db.scalars(select(LegacyStatus).order_by(LegacyStatus.id))]
    adjustments = [{"id": r.id, "created_at": r.created_at, "start_month": r.start_month, "end_month": r.end_month,
                    "target_pot": r.target_pot, "source_pot": r.source_pot, "target_before": float(r.target_before),
                    "target_after": float(r.target_after), "source_before": float(r.source_before),
                    "source_after": float(r.source_after), "sample_months": r.sample_months}
                   for r in db.scalars(select(BudgetAdjustment).order_by(BudgetAdjustment.id))]
    return {"format": "finfam-backup-v1", "generated_at": datetime.now().astimezone().isoformat(),
            "transactions": txs, "accounts": accounts, "cards": cards, "lookups": lookups,
            "settings": settings, "month_closure": closed, "cashflow_status": statuses, "legacy_status": legacy,
            "budget_adjustments": adjustments}


def csv_export(db: Session) -> bytes:
    txs = [_tx_dict(r) for r in db.scalars(select(Transaction).order_by(Transaction.transaction_date.desc(), Transaction.id.desc()))]
    output = io.StringIO(newline="")
    fields = ["id", "transaction_date", "date_precision", "member", "category", "description", "type", "account_card", "amount", "total_amount", "installment_count", "installment_overrides", "start_month", "end_month", "destination_account", "linked_pot", "payment_bank", "archived"]
    for row in txs:
        row["installment_overrides"] = json.dumps(row.get("installment_overrides") or {}, ensure_ascii=False, sort_keys=True)
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(txs)
    return ("\ufeff" + output.getvalue()).encode("utf-8")


# Recovery is intentionally implemented below the existing finance helpers so
# the established backup format and AppSetting behavior remain unchanged.
BACKUP_FORMAT = "finfam-backup-v1"
MAX_BACKUP_BYTES = 16 * 1024 * 1024
MAX_TRANSACTIONS = 100_000
MAX_ACCOUNTS = 10_000
MAX_CARDS = 10_000
MAX_LOOKUP_VALUES = 50_000
MAX_STATUS_ROWS = 250_000
MAX_BUDGET_ADJUSTMENTS = 100_000
UNDO_WINDOW = timedelta(minutes=15)


class RecoveryError(ValueError):
    """Base class for safe, user-facing recovery validation failures."""


class BackupValidationError(RecoveryError):
    pass


class ConcurrentEditError(RecoveryError):
    """The row no longer equals the after-image captured by an event."""


class UndoExpiredError(RecoveryError):
    pass


class UndoConflictError(ConcurrentEditError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: datetime | None = None) -> str:
    return (value or _utc_now()).astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_stamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _payload_equal(left: Any, right: Any) -> bool:
    try:
        return json.dumps(left, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str) == json.dumps(
            right, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
        )
    except (TypeError, ValueError):
        return left == right


def record_activity_event(
    db: Session,
    action: str,
    entity_type: str,
    entity_id: str | int,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    *,
    undo: dict[str, Any] | None = None,
    created_at: datetime | None = None,
    undo_window: timedelta = UNDO_WINDOW,
) -> ActivityEvent:
    """Record a reversible before/after event without credentials or session data."""
    if action not in {"create", "update", "delete", "restore", "undo"}:
        raise RecoveryError("Tipo de atividade inválido.")
    created = created_at or _utc_now()
    event = ActivityEvent(
        created_at=_stamp(created),
        action=action,
        entity_type=str(entity_type)[:64],
        entity_id=str(entity_id)[:128],
        before=before,
        after=after,
        undo=undo,
        undo_expires_at=_stamp(created + undo_window) if undo_window.total_seconds() > 0 else None,
    )
    db.add(event)
    db.flush()
    return event


# A descriptive alias makes the intended call site clear to route integrators.
log_activity_event = record_activity_event


def record_transaction_event(
    db: Session,
    action: str,
    transaction_id: int | str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    *,
    undo: dict[str, Any] | None = None,
    created_at: datetime | None = None,
    undo_window: timedelta = UNDO_WINDOW,
) -> ActivityEvent:
    """Convenience wrapper for transaction routes; identity remains family-wide."""
    return record_activity_event(
        db, action, "transaction", transaction_id, before, after,
        undo=undo, created_at=created_at, undo_window=undo_window,
    )


def recent_activity(db: Session, limit: int = 50) -> list[ActivityEvent]:
    limit = max(1, min(int(limit), 100))
    return list(db.scalars(select(ActivityEvent).order_by(ActivityEvent.created_at.desc(), ActivityEvent.id.desc()).limit(limit)))


def latest_family_activity(db: Session, limit: int = 50) -> list[ActivityEvent]:
    """Return latest shared-family changes; no individual identity is inferred."""
    return recent_activity(db, limit=limit)


def latest_activity_id(db: Session, entity_type: str, entity_id: str | int, action: str | None = None) -> int | None:
    statement = select(ActivityEvent.id).where(
        ActivityEvent.entity_type == str(entity_type),
        ActivityEvent.entity_id == str(entity_id),
    )
    if action:
        statement = statement.where(ActivityEvent.action == action)
    return db.scalar(statement.order_by(ActivityEvent.id.desc()).limit(1))


def _status_rows_for_transaction(db: Session, tx_id: int) -> list[dict[str, Any]]:
    suffix = f":tx:{int(tx_id)}"
    return [
        {"month": row.month, "item_key": row.item_key, "paid": bool(row.paid), "paid_on": row.paid_on}
        for row in db.scalars(select(CashflowStatus).where(or_(
            CashflowStatus.item_key.like(f"%{suffix}"),
            CashflowStatus.item_key.like(f"%{suffix}:out"),
            CashflowStatus.item_key.like(f"%{suffix}:in"),
        )))
    ]


def _restore_transaction_values(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "id", "member", "category", "description", "type", "account_card", "amount", "total_amount",
        "installment_count", "installment_overrides", "start_month", "end_month", "destination_account", "linked_pot", "card_flag",
        "payment_bank", "transaction_date", "date_precision", "archived",
    }
    values = {key: payload.get(key) for key in allowed if key in payload}
    values.setdefault("member", "Família")
    values.setdefault("category", "")
    values.setdefault("description", "")
    values.setdefault("type", "")
    values.setdefault("account_card", "")
    values.setdefault("amount", 0.0)
    values.setdefault("total_amount", values["amount"])
    values.setdefault("installment_count", 1)
    values.setdefault("installment_overrides", {})
    values.setdefault("start_month", None)
    values.setdefault("end_month", values.get("start_month"))
    values.setdefault("destination_account", "")
    values.setdefault("linked_pot", "")
    values.setdefault("card_flag", 0)
    values.setdefault("payment_bank", values.get("account_card", ""))
    values.setdefault("transaction_date", values.get("start_month"))
    values.setdefault("date_precision", "exact")
    values.setdefault("archived", False)
    return values


def undo_activity_event(db: Session, event_id: int, *, now: datetime | None = None) -> ActivityEvent:
    """Undo one transaction event only while its exact after-image is current.

    The comparison happens inside the caller's transaction and therefore never
    silently overwrites an edit made after the original event.
    """
    event = db.scalar(select(ActivityEvent).where(ActivityEvent.id == int(event_id)).with_for_update())
    if event is None:
        raise RecoveryError("Alteração não encontrada.")
    current_time = now or _utc_now()
    expires = _parse_stamp(event.undo_expires_at)
    if event.undone_at:
        raise RecoveryError("Esta alteração já foi desfeita.")
    if expires is None or current_time.astimezone(timezone.utc) > expires:
        raise UndoExpiredError("O prazo de 15 minutos para desfazer terminou.")
    if event.entity_type != "transaction" or event.action not in {"create", "update", "delete"}:
        raise RecoveryError("Esta atividade não pode ser desfeita por esta interface.")

    tx_id = int(event.entity_id)
    # SQLite ignores FOR UPDATE; managed MySQL uses it to serialize the
    # compare-and-undo decision with a concurrent writer.
    row = db.scalar(select(Transaction).where(Transaction.id == tx_id).with_for_update())
    expected_after = event.after
    closures = list(db.scalars(select(MonthClosure).where(MonthClosure.closed.is_(True)).with_for_update()))
    payloads = [event.before, event.after, (event.undo or {}).get("transaction")]
    undo_info = event.undo or {}
    if undo_info.get("kind") == "prospective_end":
        cutoff = month_id(month_start(undo_info.get("cutoff_month")))
        old_end = month_id(month_start((event.before or {}).get("end_month"))) if (event.before or {}).get("end_month") else None
        for closure in closures:
            index = month_id(month_start(closure.month))
            if index is not None and cutoff is not None and index >= cutoff and (old_end is None or index <= old_end):
                raise UndoConflictError("O encerramento alcança uma competência fechada. Reabra o mês antes de desfazer; nada foi alterado.")
    else:
        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            start = month_id(month_start(payload.get("start_month") or payload.get("transaction_date")))
            end = month_id(month_start(payload.get("end_month"))) if payload.get("end_month") else None
            if start is None:
                continue
            for closure in closures:
                index = month_id(month_start(closure.month))
                if index is not None and index >= start and (end is None or index <= end):
                    raise UndoConflictError("Esta alteração alcança um mês fechado. Reabra a competência antes de desfazer; nada foi alterado.")
    future_statuses_to_restore: list[dict[str, Any]] = []
    if event.action == "update" and undo_info.get("kind") == "prospective_end":
        for item in undo_info.get("future_statuses") or []:
            key = {"month": str(item.get("month", "")), "item_key": str(item.get("item_key", ""))}
            if not key["month"] or not key["item_key"]:
                raise RecoveryError("O histórico das baixas futuras está incompleto.")
            existing = db.scalar(select(CashflowStatus).where(
                CashflowStatus.month == key["month"], CashflowStatus.item_key == key["item_key"],
            ).with_for_update())
            if existing is not None:
                raise UndoConflictError("Uma baixa futura mudou depois do encerramento; nada foi sobrescrito.")
            future_statuses_to_restore.append({**key, "paid": bool(item.get("paid", False)), "paid_on": item.get("paid_on")})
    if event.action == "create":
        if row is None or not _payload_equal(_tx_dict(row), expected_after):
            raise UndoConflictError("O lançamento mudou depois desta inclusão; nada foi sobrescrito.")
        db.execute(delete(CashflowStatus).where(or_(
            CashflowStatus.item_key.like(f"%:tx:{tx_id}"),
            CashflowStatus.item_key.like(f"%:tx:{tx_id}:out"),
            CashflowStatus.item_key.like(f"%:tx:{tx_id}:in"),
        )))
        db.delete(row)
    elif event.action == "update":
        if row is None or not _payload_equal(_tx_dict(row), expected_after):
            raise UndoConflictError("O lançamento mudou depois desta edição; nada foi sobrescrito.")
        before = event.before
        if not isinstance(before, dict):
            raise RecoveryError("O histórico desta alteração está incompleto.")
        for key, value in _restore_transaction_values(before).items():
            if key != "id":
                setattr(row, key, value)
        for item in future_statuses_to_restore:
            db.add(CashflowStatus(month=item["month"], item_key=item["item_key"],
                                  paid=bool(item["paid"]), paid_on=item.get("paid_on")))
    else:  # delete
        if row is not None:
            raise UndoConflictError("O lançamento foi recriado depois da exclusão; nada foi sobrescrito.")
        before = event.before or (event.undo or {}).get("transaction")
        if not isinstance(before, dict):
            raise RecoveryError("O histórico da exclusão está incompleto.")
        values = _restore_transaction_values(before)
        row = Transaction(**values)
        db.add(row)
        db.flush()
        for item in ((event.undo or {}).get("cashflow_status") or []):
            key = {"month": str(item.get("month", "")), "item_key": str(item.get("item_key", ""))}
            if not key["month"] or not key["item_key"] or db.get(CashflowStatus, key) is not None:
                raise UndoConflictError("Uma baixa relacionada mudou depois da exclusão; nada foi sobrescrito.")
            db.add(CashflowStatus(month=key["month"], item_key=key["item_key"], paid=bool(item.get("paid", False)), paid_on=item.get("paid_on")))
    db.flush()
    event.undone_at = _stamp(current_time)
    db.flush()
    return event


# Public spelling for callers that prefer a transaction-specific helper.
undo_transaction_event = undo_activity_event


def _require_dict(value: Any, message: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BackupValidationError(message)
    return value


def _require_list(value: Any, message: str, maximum: int) -> list[Any]:
    if not isinstance(value, list) or len(value) > maximum:
        raise BackupValidationError(message)
    return value


def _text(value: Any, field: str, maximum: int, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or len(value) > maximum:
        raise BackupValidationError(f"Campo de backup inválido: {field}.")
    return value


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise BackupValidationError(f"Número inválido no backup: {field}.")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise BackupValidationError(f"Número inválido no backup: {field}.") from None
    if not math.isfinite(result):
        raise BackupValidationError(f"Número inválido no backup: {field}.")
    return result


def _validate_transaction_rows(rows: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    ids: set[int] = set()
    required = ("id", "member", "category", "description", "type", "account_card", "amount", "total_amount", "installment_count", "start_month", "end_month", "destination_account", "linked_pot", "card_flag", "payment_bank", "transaction_date", "date_precision", "archived")
    for item in rows:
        row = _require_dict(item, "Cada lançamento do backup precisa ser um objeto.")
        missing = [key for key in required if key not in row]
        if missing:
            raise BackupValidationError("Lançamento sem campos obrigatórios.")
        tx_id = row["id"]
        if isinstance(tx_id, bool) or not isinstance(tx_id, int) or tx_id <= 0 or tx_id in ids:
            raise BackupValidationError("IDs de lançamentos inválidos ou repetidos.")
        ids.add(tx_id)
        for field in ("member", "category", "description", "type", "account_card", "destination_account", "linked_pot", "payment_bank", "date_precision"):
            _text(row[field], field, 255)
        for field in ("amount", "total_amount"):
            _finite_number(row[field], field)
        if isinstance(row["installment_count"], bool) or not isinstance(row["installment_count"], int) or not 1 <= row["installment_count"] <= 1200:
            raise BackupValidationError("Quantidade de parcelas inválida no backup.")
        if isinstance(row["card_flag"], bool) or row["card_flag"] not in (0, 1):
            raise BackupValidationError("Indicador de cartão inválido no backup.")
        if not isinstance(row["archived"], bool):
            raise BackupValidationError("Status de lançamento inválido no backup.")
        _text(row["start_month"], "start_month", 10, allow_none=True)
        _text(row["end_month"], "end_month", 10, allow_none=True)
        _text(row["transaction_date"], "transaction_date", 10, allow_none=True)
        validated = dict(row)
        try:
            validated["installment_overrides"] = _normalized_installment_overrides(row.get("installment_overrides"))
        except (TypeError, ValueError, OverflowError):
            raise BackupValidationError("Ajustes individuais de parcelas inválidos no backup.") from None
        result.append(validated)
    return result


def validate_backup_payload(payload: Any) -> dict[str, Any]:
    """Validate the existing finfam-backup-v1 shape and its row relationships."""
    root = _require_dict(payload, "O backup precisa ser um objeto JSON.")
    if root.get("format") != BACKUP_FORMAT:
        raise BackupValidationError("Formato de backup não reconhecido.")
    required_top = {"format", "transactions", "accounts", "cards", "lookups", "settings", "month_closure", "cashflow_status", "legacy_status"}
    if not required_top.issubset(root):
        raise BackupValidationError("O backup não contém todas as seções esperadas.")
    transactions = _validate_transaction_rows(_require_list(root["transactions"], "Limite de lançamentos excedido.", MAX_TRANSACTIONS))
    accounts_raw = _require_list(root["accounts"], "Limite de contas excedido.", MAX_ACCOUNTS)
    cards_raw = _require_list(root["cards"], "Limite de cartões excedido.", MAX_CARDS)
    accounts: list[dict[str, Any]] = []
    account_names: set[str] = set()
    for item in accounts_raw:
        row = _require_dict(item, "Cada conta do backup precisa ser um objeto.")
        name = _text(row.get("name"), "account.name", 128)
        assert name is not None
        if not name or name in account_names:
            raise BackupValidationError("Contas inválidas ou repetidas no backup.")
        account_names.add(name)
        accounts.append({"name": name, "owner": _text(row.get("owner", ""), "account.owner", 64), "initial_balance": _finite_number(row.get("initial_balance", 0), "account.initial_balance")})
    cards: list[dict[str, Any]] = []
    card_names: set[str] = set()
    for item in cards_raw:
        row = _require_dict(item, "Cada cartão do backup precisa ser um objeto.")
        name = _text(row.get("name"), "card.name", 128)
        assert name is not None
        if not name or name in card_names:
            raise BackupValidationError("Cartões inválidos ou repetidos no backup.")
        card_names.add(name)
        cards.append({"name": name, "bank": _text(row.get("bank", ""), "card.bank", 128), "limit_amount": _finite_number(row.get("limit_amount", 0), "card.limit_amount")})
    known_accounts = account_names | card_names
    for row in transactions:
        account = str(row["account_card"])
        destination = str(row["destination_account"])
        if account and account not in known_accounts:
            raise BackupValidationError("Lançamento referencia conta ou cartão ausente.")
        if destination and destination not in account_names:
            raise BackupValidationError("Transferência referencia conta de destino ausente.")
        if row["installment_overrides"] and (row["type"] not in {"Crédito à vista", "Parcelado"} or account not in card_names):
            raise BackupValidationError("Ajuste de parcela referencia uma compra de cartão inválida.")
        if any(Decimal(value) < 0 for value in row["installment_overrides"].values()) and row["type"] != "Crédito à vista":
            raise BackupValidationError("Ajustes negativos só são permitidos para Crédito à vista vinculado a cartão.")
        amount, total = float(row["amount"]), float(row["total_amount"])
        if (amount < 0 or total < 0) and (row["type"] != "Crédito à vista" or account not in card_names or ((amount < 0) != (total < 0))):
            raise BackupValidationError("Valores-base negativos só são permitidos para estornos à vista em cartão.")
        start_month = month_start(row.get("start_month"))
        end_month = month_start(row.get("end_month")) if row.get("end_month") else None
        for override_month in row["installment_overrides"]:
            parsed_month = month_start(override_month)
            if parsed_month is None or start_month is None or parsed_month < start_month or (end_month and parsed_month > end_month):
                raise BackupValidationError("O ajuste individual está fora do período ativo da compra.")

    lookups_root = _require_dict(root["lookups"], "A seção de listas do backup é inválida.")
    if len(lookups_root) > 100:
        raise BackupValidationError("Limite de listas excedido.")
    lookups: dict[str, list[str]] = {}
    total_lookup_values = 0
    for kind, values in lookups_root.items():
        _text(kind, "lookup.kind", 64)
        clean_values = _require_list(values, "Limite de valores de lista excedido.", MAX_LOOKUP_VALUES)
        output: list[str] = []
        for value in clean_values:
            text = _text(value, "lookup.value", 191)
            assert text is not None
            if text and text not in output:
                output.append(text)
        total_lookup_values += len(output)
        lookups[str(kind)] = output
    if total_lookup_values > MAX_LOOKUP_VALUES:
        raise BackupValidationError("Limite de valores de lista excedido.")

    settings_root = _require_dict(root["settings"], "A seção de configurações do backup é inválida.")
    settings: dict[str, Any] = {}
    for key, value in settings_root.items():
        _text(key, "settings.key", 128)
        try:
            json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            raise BackupValidationError("Uma configuração do backup não é JSON válida.") from None
        settings[str(key)] = value

    closure_root = _require_dict(root["month_closure"], "A seção de fechamentos do backup é inválida.")
    month_closure: dict[str, bool] = {}
    for month, closed in closure_root.items():
        _text(month, "month_closure.month", 10)
        if not isinstance(closed, bool):
            raise BackupValidationError("Fechamento mensal inválido no backup.")
        month_closure[str(month)] = closed

    statuses_raw = _require_list(root["cashflow_status"], "Limite de baixas excedido.", MAX_STATUS_ROWS)
    statuses: list[dict[str, Any]] = []
    status_keys: set[tuple[str, str]] = set()
    tx_ids = {int(row["id"]) for row in transactions}
    for item in statuses_raw:
        row = _require_dict(item, "Cada baixa do backup precisa ser um objeto.")
        month = _text(row.get("month"), "cashflow_status.month", 10)
        item_key = _text(row.get("item_key"), "cashflow_status.item_key", 255)
        assert month is not None and item_key is not None
        if (month, item_key) in status_keys:
            raise BackupValidationError("Baixas repetidas no backup.")
        status_keys.add((month, item_key))
        if not isinstance(row.get("paid"), bool):
            raise BackupValidationError("Status de baixa inválido no backup.")
        paid_on = _text(row.get("paid_on"), "cashflow_status.paid_on", 32, allow_none=True)
        marker = ":tx:"
        if marker in item_key:
            try:
                marker_value = item_key.split(marker, 1)[1].split(":", 1)[0]
                if int(marker_value) not in tx_ids:
                    raise BackupValidationError("Baixa referencia lançamento ausente.")
            except ValueError:
                raise BackupValidationError("Chave de baixa inválida no backup.") from None
        statuses.append({"month": month, "item_key": item_key, "paid": bool(row["paid"]), "paid_on": paid_on})

    legacy_raw = _require_list(root["legacy_status"], "Limite de status legado excedido.", MAX_STATUS_ROWS)
    legacy: list[dict[str, Any]] = []
    for item in legacy_raw:
        row = _require_dict(item, "Cada status legado precisa ser um objeto.")
        month = _text(row.get("month"), "legacy_status.month", 10)
        account = _text(row.get("account", ""), "legacy_status.account", 128)
        tx_type = _text(row.get("type", ""), "legacy_status.type", 64)
        member = _text(row.get("member", ""), "legacy_status.member", 64)
        description = _text(row.get("description", ""), "legacy_status.description", 255)
        if not isinstance(row.get("paid", False), bool):
            raise BackupValidationError("Status legado inválido no backup.")
        legacy.append({
            "month": month,
            "account": account,
            "type": tx_type,
            "member": member,
            "description": description,
            "amount": _finite_number(row.get("amount", 0), "legacy_status.amount"),
            "paid": bool(row.get("paid", False)),
        })

    adjustments_raw = _require_list(root.get("budget_adjustments", []), "Limite de ajustes de orçamento excedido.", MAX_BUDGET_ADJUSTMENTS)
    adjustments: list[dict[str, Any]] = []
    adjustment_ids: set[int] = set()
    for item in adjustments_raw:
        row = _require_dict(item, "Cada ajuste de orçamento precisa ser um objeto.")
        adjustment_id = row.get("id")
        if isinstance(adjustment_id, bool) or not isinstance(adjustment_id, int) or adjustment_id <= 0 or adjustment_id in adjustment_ids:
            raise BackupValidationError("IDs de ajustes de orçamento inválidos ou repetidos.")
        adjustment_ids.add(adjustment_id)
        created_at = _text(row.get("created_at"), "budget_adjustment.created_at", 40)
        start_month = _text(row.get("start_month"), "budget_adjustment.start_month", 10)
        end_month = _text(row.get("end_month"), "budget_adjustment.end_month", 10)
        target_pot = _text(row.get("target_pot"), "budget_adjustment.target_pot", 191)
        source_pot = _text(row.get("source_pot"), "budget_adjustment.source_pot", 191)
        assert created_at is not None and start_month is not None and end_month is not None
        assert target_pot is not None and source_pot is not None
        start_date, end_date = month_start(start_month), month_start(end_month)
        if not start_date or not end_date or start_date > end_date or not target_pot or not source_pot or target_pot == source_pot:
            raise BackupValidationError("Intervalo ou potes inválidos em ajuste de orçamento.")
        sample_months = row.get("sample_months")
        if isinstance(sample_months, bool) or not isinstance(sample_months, int) or not 0 <= sample_months <= 1200:
            raise BackupValidationError("Quantidade de meses de amostra inválida no ajuste de orçamento.")
        adjustments.append({
            "id": adjustment_id, "created_at": created_at, "start_month": start_month, "end_month": end_month,
            "target_pot": target_pot, "source_pot": source_pot,
            "target_before": _finite_number(row.get("target_before"), "budget_adjustment.target_before"),
            "target_after": _finite_number(row.get("target_after"), "budget_adjustment.target_after"),
            "source_before": _finite_number(row.get("source_before"), "budget_adjustment.source_before"),
            "source_after": _finite_number(row.get("source_after"), "budget_adjustment.source_after"),
            "sample_months": sample_months,
        })

    # Keep the established format, while returning copies so callers cannot mutate
    # the object that was validated before the transaction starts.
    return {
        "format": BACKUP_FORMAT,
        "generated_at": root.get("generated_at"),
        "transactions": transactions,
        "accounts": accounts,
        "cards": cards,
        "lookups": lookups,
        "settings": settings,
        "month_closure": month_closure,
        "cashflow_status": statuses,
        "legacy_status": legacy,
        "budget_adjustments": adjustments,
    }


def parse_backup_json(raw: bytes, *, max_bytes: int = MAX_BACKUP_BYTES) -> dict[str, Any]:
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > max_bytes:
        raise BackupValidationError("O arquivo de backup excede o limite permitido.")
    try:
        payload = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise BackupValidationError("O arquivo precisa conter JSON UTF-8 válido.") from None
    return validate_backup_payload(payload)


def backup_counts(payload: dict[str, Any]) -> dict[str, int]:
    checked = validate_backup_payload(payload)
    return {
        "transactions": len(checked["transactions"]),
        "accounts": len(checked["accounts"]),
        "cards": len(checked["cards"]),
        "lookups": sum(len(values) for values in checked["lookups"].values()),
        "month_closure": len(checked["month_closure"]),
        "cashflow_status": len(checked["cashflow_status"]),
        "legacy_status": len(checked["legacy_status"]),
        "budget_adjustments": len(checked["budget_adjustments"]),
    }


def backup_digest(payload: dict[str, Any]) -> str:
    import hashlib
    checked = validate_backup_payload(payload)
    encoded = json.dumps(checked, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _replace_financial_data(db: Session, payload: dict[str, Any]) -> None:
    # Notification/Android tables, activity history, and recovery snapshots are
    # intentionally outside finfam-backup-v1 and are not deleted here.
    for model in (CashflowStatus, LegacyStatus, MonthClosure, BudgetAdjustment, LookupValue, Transaction, Card, Account, AppSetting):
        db.execute(delete(model))
    db.flush()
    db.add_all(Account(**row) for row in payload["accounts"])
    db.add_all(Card(**row) for row in payload["cards"])
    for kind, values in payload["lookups"].items():
        db.add_all(LookupValue(kind=kind, value=value, position=position) for position, value in enumerate(values))
    for row in payload["transactions"]:
        db.add(Transaction(**_restore_transaction_values(row)))
    for key, value in payload["settings"].items():
        db.add(AppSetting(key=key, value=json.dumps(value, ensure_ascii=False, allow_nan=False)))
    db.add_all(MonthClosure(month=month, closed=closed) for month, closed in payload["month_closure"].items())
    db.add_all(CashflowStatus(**row) for row in payload["cashflow_status"])
    db.add_all(LegacyStatus(**row) for row in payload["legacy_status"])
    db.add_all(BudgetAdjustment(**row) for row in payload["budget_adjustments"])
    db.flush()


def create_restore_snapshot(db: Session, *, kind: str = "pre-restore") -> RestoreSnapshot:
    snapshot = RestoreSnapshot(created_at=_stamp(), kind=kind, status="available", payload=backup_payload(db))
    db.add(snapshot)
    db.flush()
    return snapshot


def restore_backup(db: Session, payload: dict[str, Any]) -> RestoreSnapshot:
    """Replace the backup-covered financial base after capturing a safety snapshot.

    The function deliberately does not commit. The caller must commit the session,
    allowing the caller to include adjacent authorization checks in one transaction.
    """
    checked = validate_backup_payload(payload)
    snapshot = create_restore_snapshot(db)
    _replace_financial_data(db, checked)
    return snapshot


def rollback_restore(db: Session, snapshot_id: int) -> RestoreSnapshot:
    """Restore a safety snapshot, creating a new snapshot first for reversibility."""
    snapshot = db.get(RestoreSnapshot, int(snapshot_id))
    if snapshot is None:
        raise RecoveryError("Snapshot de recuperação não encontrado.")
    payload = validate_backup_payload(snapshot.payload)
    safety_snapshot = create_restore_snapshot(db, kind="pre-rollback")
    _replace_financial_data(db, payload)
    snapshot.status = "restored"
    snapshot.restored_at = _stamp()
    db.flush()
    return safety_snapshot


# Alternative names kept public for the parent router integrator.
restore_snapshot = rollback_restore
validate_backup = validate_backup_payload

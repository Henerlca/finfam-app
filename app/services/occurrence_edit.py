"""Edição isolada de uma competência de uma recorrência.

A edição mantém a série como pequenos segmentos contíguos. O lançamento original
é reaproveitado para o segmento que começa primeiro; os demais segmentos recebem
novos IDs. Isso deixa as chaves ``mês:tx:id`` estáveis onde possível e permite
migrá-las de forma explícita quando um clone passa a representar uma competência.

Este módulo não faz commit. O chamador deve manter a sessão dentro da mesma
transação e confirmar somente depois que o evento de auditoria foi criado.
"""
from __future__ import annotations

import json
import math
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import CashflowStatus, LegacyStatus, MonthClosure, Transaction
from app.repository import record_activity_event, read_snapshot
from app.services.finance_core import (
    FAMILY,
    CARD_TYPES,
    as_date,
    apply_payment_statuses,
    cashflow_items,
    month_from_id,
    month_id,
    month_key,
    month_start,
)

OCCURRENCE_TYPES = frozenset({
    "Receita",
    "Fixo",
    "Débito/Pix",
    "Reserva",
    "Caixinha",
    "Saque Caixinha",
    "Transferência",
    "Crédito à vista",
    "Parcelado",
})


class OccurrenceEditError(ValueError):
    """Erro seguro e exibível para uma edição de competência."""


class UnsupportedOccurrenceError(OccurrenceEditError):
    pass


class ClosedOccurrenceError(OccurrenceEditError):
    pass


class ConcurrentOccurrenceError(OccurrenceEditError):
    pass


class PaidOccurrenceConfirmationRequired(OccurrenceEditError):
    pass


@dataclass(frozen=True)
class OccurrenceSegment:
    kind: str
    start_month: str
    end_month: str | None
    amount: float
    total_amount: float
    transaction_id: int | None = None


@dataclass(frozen=True)
class OccurrenceEditResult:
    transaction_id: int
    target_month: str
    old_amount: float
    new_amount: float
    changed: bool
    segments: tuple[dict[str, Any], ...]
    status_changes: tuple[dict[str, Any], ...]
    audit_event_id: int | None = None


def _payload(row: Transaction | dict[str, Any]) -> dict[str, Any]:
    """Return the same complete transaction image used by repository audits."""
    if isinstance(row, dict):
        return {
            "id": row.get("id"), "member": row.get("member", ""), "category": row.get("category", ""),
            "description": row.get("description", ""), "type": row.get("type", ""),
            "account_card": row.get("account_card", ""), "amount": float(row.get("amount") or 0),
            "total_amount": float(row.get("total_amount") or 0), "installment_count": int(row.get("installment_count") or 1),
            "installment_overrides": dict(row.get("installment_overrides") or {}),
            "start_month": row.get("start_month"), "end_month": row.get("end_month"),
            "destination_account": row.get("destination_account", ""), "linked_pot": row.get("linked_pot", ""),
            "card_flag": int(row.get("card_flag") or 0), "payment_bank": row.get("payment_bank", ""),
            "transaction_date": row.get("transaction_date"), "date_precision": row.get("date_precision", "exact"),
            "archived": bool(row.get("archived", False)),
        }
    return {
        "id": row.id, "member": row.member, "category": row.category, "description": row.description,
        "type": row.type, "account_card": row.account_card, "amount": float(row.amount or 0),
        "total_amount": float(row.total_amount or 0), "installment_count": int(row.installment_count or 1),
        "installment_overrides": dict(row.installment_overrides or {}),
        "start_month": row.start_month, "end_month": row.end_month,
        "destination_account": row.destination_account, "linked_pot": row.linked_pot,
        "card_flag": int(row.card_flag or 0), "payment_bank": row.payment_bank,
        "transaction_date": row.transaction_date, "date_precision": row.date_precision,
        "archived": bool(row.archived),
    }


def _same_payload(left: Any, right: Any) -> bool:
    try:
        return json.dumps(left, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str) == json.dumps(
            right, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
        )
    except (TypeError, ValueError):
        return left == right


def occurrence_fingerprint(row: Transaction | dict[str, Any]) -> str:
    """Stable complete-row fingerprint for the signed occurrence form."""
    import hashlib

    return hashlib.sha256(json.dumps(_payload(row), sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def parse_occurrence_amount(value: Any, *, allow_signed: bool = False) -> float:
    text = str(value or "").strip().replace("R$", "").replace(" ", "")
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        amount = float(text)
    except (TypeError, ValueError):
        raise OccurrenceEditError("Informe um valor monetário válido.") from None
    if not math.isfinite(amount):
        raise OccurrenceEditError("Informe um valor monetário válido.")
    if abs(amount) > 1_000_000_000:
        raise OccurrenceEditError("O valor informado é muito alto.")
    if not allow_signed and amount <= 0:
        raise OccurrenceEditError("O valor precisa ser maior que zero.")
    amount = float(Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    if not allow_signed and amount < 0.01:
        raise OccurrenceEditError("O valor precisa ser de pelo menos R$ 0,01.")
    return amount


def _previous_month(value: date) -> date:
    index = month_id(value)
    if index is None:
        raise OccurrenceEditError("Informe uma competência válida.")
    return month_from_id(index - 1)


def _next_month(value: date) -> date:
    index = month_id(value)
    if index is None:
        raise OccurrenceEditError("Informe uma competência válida.")
    return month_from_id(index + 1)


def _month_iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _date_in_month(original: Any, target: date) -> str:
    source = as_date(original) or target
    day = min(source.day, monthrange(target.year, target.month)[1])
    return date(target.year, target.month, day).isoformat()


def plan_occurrence_split(row: Transaction | dict[str, Any], target_month: Any, new_amount: Any) -> tuple[OccurrenceSegment, ...]:
    """Plan contiguous before/target/after segments without touching a session."""
    raw = _payload(row)
    tx_type = str(raw.get("type") or "")
    if tx_type not in OCCURRENCE_TYPES:
        raise UnsupportedOccurrenceError("Somente receitas, fixos, reservas, caixinhas, saques e transferências recorrentes podem ser ajustados por competência.")
    start = month_start(raw.get("start_month"))
    end = month_start(raw.get("end_month")) if raw.get("end_month") else None
    target = month_start(target_month)
    if start is None or target is None or target < start or (end is not None and target > end):
        raise OccurrenceEditError("A competência escolhida não pertence a esta recorrência.")
    amount = parse_occurrence_amount(new_amount)
    old_amount = round(float(raw.get("amount") or 0), 2)
    old_total = round(float(raw.get("total_amount") or old_amount), 2)
    # Recurrence rows are monthly amounts; total_amount follows amount. Keep the
    # old total for unchanged segments, including old imported rows.
    new_total = amount
    result: list[OccurrenceSegment] = []
    if start < target:
        result.append(OccurrenceSegment("before", start.isoformat(), _month_iso(_previous_month(target)), old_amount, old_total))
    result.append(OccurrenceSegment("target", target.isoformat(), target.isoformat(), amount, new_total))
    if end is None or target < end:
        result.append(OccurrenceSegment("after", _next_month(target).isoformat(), _month_iso(end), old_amount, old_total))
    return tuple(result)


def _item_tx_id(item_key: str) -> int | None:
    marker = ":tx:"
    if marker not in item_key:
        return None
    tail = item_key.split(marker, 1)[1].split(":", 1)[0]
    try:
        return int(tail)
    except (TypeError, ValueError):
        return None


def _item_month(item_key: str) -> str | None:
    try:
        return month_key(item_key.split(":", 1)[0])
    except (TypeError, ValueError):
        return None


def _replace_item_tx(item_key: str, old_id: int, new_id: int) -> str:
    marker = f":tx:{int(old_id)}"
    prefix = item_key.split(marker, 1)
    if len(prefix) != 2:
        return item_key
    return f"{prefix[0]}:tx:{int(new_id)}{prefix[1]}"


def _active_month(target: date, start: date, end: date | None) -> bool:
    return start <= target and (end is None or target <= end)


def _card_invoice_line_amount(
    snapshot: tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, list[str]], dict[str, Any], dict[str, bool]],
    tx_id: int,
    target: date,
    *,
    ignore_target_override: bool = False,
) -> float:
    txs, _, cards, _, _, _ = snapshot
    if ignore_target_override:
        key = month_key(target)
        txs = [
            {**tx, "installment_overrides": {m: v for m, v in (tx.get("installment_overrides") or {}).items() if m != key}}
            if int(tx.get("id") or 0) == int(tx_id) else tx
            for tx in txs
        ]
    from app.services.family_planning import card_calendar

    for card in card_calendar(txs, cards, [target], FAMILY):
        for month in card.get("months", []):
            if month.get("month_key") != month_key(target):
                continue
            for purchase in month.get("purchases", []):
                purchase_id = purchase.get("id", purchase.get("transaction_id"))
                if int(purchase_id or 0) == int(tx_id):
                    return round(float(purchase.get("amount") or 0), 2)
    raise OccurrenceEditError("A parcela não aparece na fatura escolhida. Atualize a tela antes de editar.")


def _segment_id_for_month(month: str, *, start: date, end: date | None, target: date, original_id: int, target_id: int, after_id: int | None) -> int:
    current = month_start(month)
    if current is None or not _active_month(current, start, end):
        return original_id
    if current == target:
        return target_id
    if current > target:
        return after_id or original_id
    return original_id


def _copy_transaction(
    row: Transaction, *, start: date, end: date | None, amount: float, total_amount: float,
    transaction_date: str | None = None,
) -> Transaction:
    return Transaction(
        member=row.member, category=row.category, description=row.description, type=row.type,
        account_card=row.account_card, amount=amount, total_amount=total_amount,
        installment_count=int(row.installment_count or 1), start_month=start.isoformat(),
        installment_overrides=dict(row.installment_overrides or {}),
        end_month=end.isoformat() if end else None, destination_account=row.destination_account,
        linked_pot=row.linked_pot, card_flag=int(row.card_flag or 0), payment_bank=row.payment_bank,
        transaction_date=transaction_date or row.transaction_date, date_precision=row.date_precision, archived=False,
    )


def _set_segment(
    row: Transaction, *, start: date, end: date | None, amount: float, total_amount: float,
    transaction_date: str | None = None,
) -> None:
    row.start_month = start.isoformat()
    row.end_month = end.isoformat() if end else None
    row.amount = amount
    row.total_amount = total_amount
    if transaction_date is not None:
        row.transaction_date = transaction_date


def _status_rows_for_id(db: Session, tx_id: int) -> list[CashflowStatus]:
    suffix = f":tx:{int(tx_id)}"
    stmt = select(CashflowStatus).where(or_(
        CashflowStatus.item_key.like(f"%{suffix}"),
        CashflowStatus.item_key.like(f"%{suffix}:out"),
        CashflowStatus.item_key.like(f"%{suffix}:in"),
    )).with_for_update().execution_options(populate_existing=True)
    rows = list(db.scalars(stmt))
    transaction = db.scalar(select(Transaction).where(Transaction.id == int(tx_id)).with_for_update()
                            .execution_options(populate_existing=True))
    if transaction is None or not transaction.card_flag or transaction.type == "Receita" or not transaction.account_card:
        return rows
    card_suffix = f":invoice:{transaction.account_card}"
    escaped_suffix = card_suffix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    invoice_stmt = select(CashflowStatus).where(
        CashflowStatus.item_key.like(f"%{escaped_suffix}", escape="\\")
    ).with_for_update().execution_options(populate_existing=True)
    start = month_start(transaction.start_month)
    end = month_start(transaction.end_month) if transaction.end_month else None
    for status in db.scalars(invoice_stmt):
        status_month = month_start(status.month)
        if status_month and start and _active_month(status_month, start, end):
            rows.append(status)
    return rows


def _old_item_states(
    db: Session,
    tx_id: int,
    months: Iterable[str],
    snapshot: tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, list[str]], dict[str, Any], dict[str, bool]],
) -> dict[str, dict[str, Any]]:
    """Resolve both app and legacy state before changing the natural amount key."""
    txs, accounts, cards, _, _, _ = snapshot
    result: dict[str, dict[str, Any]] = {}
    for month in sorted(set(months)):
        explicit = {row.item_key: {"paid": bool(row.paid), "paid_on": row.paid_on} for row in db.scalars(select(CashflowStatus).where(CashflowStatus.month == month))}
        legacy = [{
            "account": row.account, "type": row.type, "member": row.member, "description": row.description,
            "amount": float(row.amount or 0), "paid": bool(row.paid),
        } for row in db.scalars(select(LegacyStatus).where(LegacyStatus.month == month).order_by(LegacyStatus.id))]
        items = cashflow_items(txs, cards, accounts, month, FAMILY)
        transaction = next((row for row in txs if int(row.get("id") or 0) == int(tx_id)), None)
        invoice_key = None
        if transaction and int(transaction.get("card_flag") or 0) == 1 and transaction.get("type") != "Receita":
            invoice_key = f"{month}:invoice:{transaction.get('account_card') or ''}"
        for item in apply_payment_statuses(items, legacy, explicit):
            if item.get("transaction_id") != int(tx_id) and item.get("item_key") != invoice_key:
                continue
            key = str(item["item_key"])
            result[key] = {
                "paid": bool(item.get("paid")), "paid_on": item.get("paid_on"),
                "status_origin": item.get("status_origin", "novo"), "type": item.get("type", ""),
                "month": month,
            }
    return result


def _target_is_paid(states: dict[str, dict[str, Any]], target: str) -> bool:
    return any(value.get("month") == target and bool(value.get("paid")) for value in states.values())


def occurrence_target_is_paid(
    db: Session,
    tx_id: int,
    target_month: Any,
    *,
    snapshot: tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, list[str]], dict[str, Any], dict[str, bool]] | None = None,
) -> bool:
    """Resolve the current paid state, including legacy planilha rows."""
    target = month_start(target_month)
    if target is None:
        return False
    current = snapshot or read_snapshot(db)
    states = _old_item_states(db, int(tx_id), [month_key(target)], current)
    return _target_is_paid(states, month_key(target))


def _status_after_split(
    db: Session,
    old_rows: list[CashflowStatus],
    old_states: dict[str, dict[str, Any]],
    *,
    old_id: int,
    start: date,
    end: date | None,
    target: date,
    target_id: int,
    after_id: int | None,
) -> tuple[dict[tuple[str, str], dict[str, Any]], list[dict[str, Any]]]:
    """Build destination status rows before deleting/rekeying any old row."""
    explicit: dict[tuple[str, str], dict[str, Any]] = {}
    for row in old_rows:
        month = month_key(row.month)
        key = str(row.item_key)
        explicit[(month, key)] = {"paid": bool(row.paid), "paid_on": row.paid_on}

    desired: dict[tuple[str, str], dict[str, Any]] = {}
    # Explicit rows always win over natural-key/legacy resolution.
    for (month, old_key), state in explicit.items():
        new_id = _segment_id_for_month(month, start=start, end=end, target=target, original_id=old_id, target_id=target_id, after_id=after_id)
        desired[(month, _replace_item_tx(old_key, old_id, new_id))] = dict(state)

    # Legacy paid state is materialized only where a row's ID or amount changed,
    # so an old planilha checkbox cannot disappear when the amount changes.
    for old_key, state in old_states.items():
        month = str(state.get("month") or _item_month(old_key) or "")
        if not month:
            continue
        new_id = _segment_id_for_month(month, start=start, end=end, target=target, original_id=old_id, target_id=target_id, after_id=after_id)
        new_key = _replace_item_tx(old_key, old_id, new_id)
        source_exists = (month, old_key) in explicit
        if source_exists:
            continue
        if state.get("status_origin") in {"app", "planilha"} and bool(state.get("paid")):
            desired[(month, new_key)] = {"paid": True, "paid_on": state.get("paid_on")}

    # A transfer is one financial action. If either side was explicitly known,
    # materialize the same canonical state on both sides after an ID migration.
    by_month: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for key, state in old_states.items():
        by_month.setdefault(str(state.get("month") or _item_month(key) or ""), []).append((key, state))
    for month, entries in by_month.items():
        transfers = [(key, state) for key, state in entries if state.get("type") in {"Transf. Env.", "Transf. Receb."}]
        if len(transfers) < 2 or not any(state.get("status_origin") in {"app", "planilha"} for _, state in transfers):
            continue
        canonical = next((state for _, state in transfers if state.get("paid")), transfers[0][1])
        for old_key, _ in transfers:
            new_id = _segment_id_for_month(month, start=start, end=end, target=target, original_id=old_id, target_id=target_id, after_id=after_id)
            desired[(month, _replace_item_tx(old_key, old_id, new_id))] = {
                "paid": bool(canonical.get("paid")), "paid_on": canonical.get("paid_on"),
            }

    changes: list[dict[str, Any]] = []
    before_by_key = {(month, str(row.item_key)): {"paid": bool(row.paid), "paid_on": row.paid_on} for row in old_rows for month in [month_key(row.month)]}
    for key, after in sorted(desired.items()):
        before = before_by_key.get(key)
        if before != after:
            changes.append({"month": key[0], "item_key": key[1], "before": before, "after": after})
    for key, before in sorted(before_by_key.items()):
        if key not in desired:
            changes.append({"month": key[0], "item_key": key[1], "before": before, "after": None})
    return desired, changes


def edit_occurrence(
    db: Session,
    tx_id: int,
    target_month: Any,
    new_amount: Any,
    *,
    expected_before: dict[str, Any] | None = None,
    confirm_paid_change: bool = False,
    reset_override: bool = False,
) -> OccurrenceEditResult:
    """Atomically adjust one monthly occurrence without committing the session."""
    target = month_start(target_month)
    if target is None:
        raise OccurrenceEditError("Informe uma competência válida.")
    row = db.scalar(
        select(Transaction).where(Transaction.id == int(tx_id)).with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None or row.archived:
        raise OccurrenceEditError("Lançamento não encontrado.")
    before = _payload(row)
    if expected_before is not None and not _same_payload(before, _payload(expected_before)):
        raise ConcurrentOccurrenceError("O lançamento mudou durante a edição; atualize a página antes de salvar.")
    if before["type"] not in OCCURRENCE_TYPES:
        raise UnsupportedOccurrenceError("Este tipo não aceita ajuste por competência.")
    card_adjustment = before["type"] in CARD_TYPES and int(before.get("card_flag") or 0) == 1
    if before["type"] in CARD_TYPES and not card_adjustment:
        raise UnsupportedOccurrenceError("Ajuste parcela a parcela exige uma compra vinculada a cartão.")
    start = month_start(before.get("start_month"))
    end = month_start(before.get("end_month")) if before.get("end_month") else None
    if start is None or not _active_month(target, start, end):
        raise OccurrenceEditError("A competência escolhida não pertence a esta recorrência.")
    closed = db.scalar(select(MonthClosure.closed).where(MonthClosure.month == month_key(target)).with_for_update())
    if bool(closed):
        raise ClosedOccurrenceError("A competência selecionada está fechada. Reabra o mês para ajustar somente esta competência; nada foi alterado.")

    if card_adjustment:
        key = month_key(target)
        allow_negative_amount = before["type"] == "Crédito à vista" and int(before.get("card_flag") or 0) == 1
        snapshot = read_snapshot(db)
        old_amount = _card_invoice_line_amount(snapshot, int(tx_id), target)
        base_amount = _card_invoice_line_amount(snapshot, int(tx_id), target, ignore_target_override=True)
        overrides_before = dict(before.get("installment_overrides") or {})
        overrides_after = dict(overrides_before)
        if reset_override:
            if key not in overrides_after:
                return OccurrenceEditResult(int(tx_id), key, old_amount, base_amount, False, (before,), (), None)
            amount = base_amount
            overrides_after.pop(key, None)
        else:
            amount = parse_occurrence_amount(new_amount, allow_signed=allow_negative_amount)
            if abs(amount) > 1_000_000_000:
                raise OccurrenceEditError("O valor ajustado da parcela é muito alto.")
            if math.isclose(amount, base_amount, abs_tol=0.000001):
                overrides_after.pop(key, None)
            else:
                overrides_after[key] = f"{amount:.2f}"
        if overrides_after == overrides_before:
            return OccurrenceEditResult(int(tx_id), key, old_amount, amount, False, (before,), (), None)
        states = _old_item_states(db, int(tx_id), [key], snapshot)
        if _target_is_paid(states, key) and not confirm_paid_change:
            raise PaidOccurrenceConfirmationRequired(
                "Esta fatura já está confirmada. Marque explicitamente “alterar valor já confirmado”; a baixa continuará confirmada."
            )
        row.installment_overrides = overrides_after
        db.flush()
        after = _payload(row)
        event = record_activity_event(
            db, "update", "occurrence_edit", str(tx_id), before, after,
            undo={"kind": "installment_override", "month": key, "before": before, "after": after},
            undo_window=timedelta(0),
        )
        segment = {"kind": "override", "start_month": target.isoformat(), "end_month": target.isoformat(),
                   "amount": amount, "total_amount": float(before.get("total_amount") or 0), "transaction_id": int(tx_id)}
        return OccurrenceEditResult(int(tx_id), key, old_amount, amount, True, (segment,), (), int(event.id))

    amount = parse_occurrence_amount(new_amount)
    old_amount = round(float(before.get("amount") or 0), 2)
    if math.isclose(old_amount, amount, abs_tol=0.000001):
        return OccurrenceEditResult(int(tx_id), month_key(target), old_amount, amount, False, (before,), (), None)

    # Gather all rows needed to carry existing app/legacy checkboxes before any
    # segment is changed. Legacy rows are bounded to the old active interval.
    old_status_rows = _status_rows_for_id(db, int(tx_id))
    old_status_payload = [
        {"month": month_key(status.month), "item_key": str(status.item_key), "paid": bool(status.paid), "paid_on": status.paid_on}
        for status in old_status_rows
    ]
    status_months = {month_key(row.month) for row in old_status_rows}
    legacy_months = {month_key(row.month) for row in db.scalars(select(LegacyStatus))}
    candidate_months = {
        month for month in legacy_months
        if (m := month_start(month)) is not None and _active_month(m, start, end)
    }
    candidate_months.update(status_months)
    candidate_months.add(month_key(target))
    snapshot = read_snapshot(db)
    old_states = _old_item_states(db, int(tx_id), candidate_months, snapshot)
    if _target_is_paid(old_states, month_key(target)) and not confirm_paid_change:
        raise PaidOccurrenceConfirmationRequired(
            "Esta competência já está confirmada. Marque explicitamente “alterar valor já confirmado” para continuar; a baixa não será removida automaticamente."
        )

    segments = plan_occurrence_split(before, target, amount)
    before_end = _previous_month(target) if start < target else None
    after_start = _next_month(target) if end is None or target < end else None

    # The original row remains the first segment, or the target when target is
    # the first month. This preserves old IDs for all prior/closed months.
    if start < target:
        _set_segment(row, start=start, end=before_end, amount=old_amount, total_amount=float(before.get("total_amount") or old_amount))
        target_row = _copy_transaction(
            row, start=target, end=target, amount=amount, total_amount=amount,
            transaction_date=_date_in_month(before.get("transaction_date"), target),
        )
        db.add(target_row)
        db.flush()
    else:
        _set_segment(
            row, start=target, end=target, amount=amount, total_amount=amount,
            transaction_date=_date_in_month(before.get("transaction_date"), target),
        )
        target_row = row
    after_row: Transaction | None = None
    if after_start is not None:
        after_row = _copy_transaction(
            row,
            start=after_start,
            end=end,
            amount=old_amount,
            total_amount=float(before.get("total_amount") or old_amount),
            transaction_date=_date_in_month(before.get("transaction_date"), after_start),
        )
        # The segment's exact transaction date advances with its start month;
        # every other descriptive field remains unchanged.
        db.add(after_row)
        db.flush()
    db.flush()

    target_id = int(target_row.id)
    after_id = int(after_row.id) if after_row is not None else None
    desired_status, status_changes = _status_after_split(
        db, old_status_rows, old_states, old_id=int(tx_id), start=start, end=end,
        target=target, target_id=target_id, after_id=after_id,
    )
    old_keys = {(month_key(status.month), str(status.item_key)) for status in old_status_rows}
    destination_keys = set(desired_status)
    # A pre-existing destination means another writer already materialized a
    # split; fail rather than merging statuses and creating duplicate months.
    for month, item_key in destination_keys - old_keys:
        existing = db.get(CashflowStatus, {"month": month, "item_key": item_key})
        if existing is not None:
            raise ConcurrentOccurrenceError("O status desta competência mudou durante a edição; atualize a página antes de salvar.")
    for status in old_status_rows:
        db.delete(status)
    db.flush()
    for (month, item_key), state in desired_status.items():
        db.add(CashflowStatus(month=month, item_key=item_key, paid=bool(state.get("paid")), paid_on=state.get("paid_on")))
    db.flush()

    segment_after = [_payload(row)]
    if target_row is not row:
        segment_after.append(_payload(target_row))
    if after_row is not None:
        segment_after.append(_payload(after_row))
    segment_after.sort(key=lambda item: month_id(item.get("start_month")) or 0)
    audit_before = {
        "kind": "occurrence_edit", "transaction_id": int(tx_id), "target_month": month_key(target),
        "before_transaction": before, "before_segments": [before],
        "status_changes": status_changes, "old_status": old_status_payload,
    }
    audit_after = {
        "kind": "occurrence_edit", "transaction_id": int(tx_id), "target_month": month_key(target),
        "after_segments": segment_after,
        "status_changes": status_changes,
        "status_after": [{"month": month, "item_key": key, "paid": bool(state.get("paid")), "paid_on": state.get("paid_on")} for (month, key), state in sorted(desired_status.items())],
    }
    event = record_activity_event(
        db, "update", "occurrence_edit", f"{int(tx_id)}:{month_key(target)}", audit_before, audit_after,
        undo={"kind": "audit-only", "message": "Edição de competência sem desfazer automático; use uma nova edição explícita se necessário."},
        undo_window=timedelta(0),
    )
    return OccurrenceEditResult(
        int(tx_id), month_key(target), old_amount, amount, True,
        tuple(segment_after), tuple(status_changes), int(event.id),
    )


# Friendly aliases for route/test callers.
edit_one_occurrence = edit_occurrence
split_occurrence = plan_occurrence_split
SUPPORTED_OCCURRENCE_TYPES = OCCURRENCE_TYPES

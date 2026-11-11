"""Reserve usage details and a cumulative series by budget-cycle competence."""
from __future__ import annotations

from calendar import monthrange
from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable, Mapping

from app.services.finance_core import (
    INFINITE_MONTH,
    _member_ok,
    as_date,
    month_id,
    month_start,
    normalize_transaction,
    pot_monthly_status,
)


def reserve_usage_details(
    transactions: Iterable[Mapping[str, Any]], month: Any, person: str,
) -> dict[str, list[dict[str, Any]]]:
    """Return the exact ledger rows counted by ``pot_monthly_status`` per pot."""
    selected_month = month_start(month)
    idx = month_id(selected_month) if selected_month else None
    if selected_month is None or idx is None:
        return {}

    details: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in transactions:
        if raw.get("archived", False):
            continue
        tx = normalize_transaction(dict(raw))
        if not tx["linked_pot"] or tx["type"] == "Reserva" or tx["start_id"] is None:
            continue
        if not _member_ok(tx, person):
            continue
        # Mirror the workbook/motor trigger: card-linked usage is allocated to
        # the budget month before the first statement month.
        if tx["start_id"] - tx["card_flag"] != idx:
            continue
        quantity = 1
        if tx["type"] == "Parcelado" and int(tx["end_id"]) != INFINITE_MONTH:
            quantity = max(1, int(tx["end_id"]) - int(tx["start_id"]) + 1)
        unit_cents = _cents(tx["amount"])
        # Keep sub-cent installment precision until multiplying the purchase;
        # rounding each displayed installment first can invent a few cents.
        amount_cents = _cents(Decimal(str(tx["amount"])) * quantity)
        account = tx.get("account_card") or tx.get("payment_bank") or ""
        occurred_on = tx.get("transaction_date")
        details[tx["linked_pot"]].append({
            "transaction_id": tx.get("id"),
            "date": occurred_on.isoformat() if isinstance(occurred_on, date) else str(occurred_on or ""),
            "description": str(tx.get("description") or ""),
            "category": str(tx.get("category") or ""),
            "member": str(tx.get("member") or "Família"),
            "account": str(account),
            "type": str(tx.get("type") or ""),
            "installment_count": quantity,
            "unit_amount_cents": unit_cents,
            "amount_cents": amount_cents,
            "unit_amount": unit_cents / 100,
            "amount": amount_cents / 100,
        })

    for rows in details.values():
        rows.sort(key=lambda row: (str(row.get("date") or ""), int(row.get("transaction_id") or 0)))
    return dict(details)


def _cents(value: Any) -> int:
    try:
        amount = Decimal(str(value or 0))
    except Exception:
        return 0
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _cycle_day(raw_date: Any, selected_month: date, days_in_month: int) -> int:
    """Map a ledger date's day number onto the selected budget-cycle month."""
    occurred_on = as_date(raw_date)
    if occurred_on is not None:
        return min(occurred_on.day, days_in_month)
    current_month = date.today().replace(day=1)
    fallback_day = 10 if selected_month > current_month else 1
    return min(fallback_day, days_in_month)


def daily_reserve_summary(
    transactions: Iterable[Mapping[str, Any]],
    month: Any,
    person: str,
    reserved_total: Any,
    *,
    usage_details_by_pot: Mapping[str, list[dict[str, Any]]] | None = None,
    active_pot_names: Iterable[str] | None = None,
    expected_used_total: Any | None = None,
) -> dict[str, Any]:
    """Return cumulative reserve use assigned to the selected budget/cycle month.

    Each contributing ledger row is selected by the same ``start_id - card_flag``
    rule as ``pot_monthly_status``. Its day number is placed on the selected
    competence axis; when its recorded date falls in a different calendar month,
    the tooltip explicitly calls it a cycle day rather than a purchase date.
    """
    selected_month = month_start(month)
    reserved_cents = _cents(reserved_total)
    if selected_month is None:
        return {
            "month": None,
            "reserved_total": reserved_cents / 100,
            "used": 0.0,
            "detail_used": 0.0,
            "percent": None,
            "integrity_ok": False,
            "rows": [],
        }

    transaction_rows = [dict(row) for row in transactions]
    canonical = pot_monthly_status(transaction_rows, selected_month, person)
    if expected_used_total is None:
        expected_used_total = canonical["totals"]["used"]
    expected_cents = _cents(expected_used_total)

    if usage_details_by_pot is None:
        usage_details_by_pot = reserve_usage_details(transaction_rows, selected_month, person)
    if active_pot_names is None:
        active_pot_names = (str(row["pot"]) for row in canonical["rows"])
    active_names = tuple(str(name) for name in active_pot_names)
    detail_rows = [
        item
        for name in active_names
        for item in usage_details_by_pot.get(name, [])
    ]
    detail_cents = sum(int(item.get("amount_cents") or 0) for item in detail_rows)
    integrity_ok = detail_cents == expected_cents

    days_in_month = monthrange(selected_month.year, selected_month.month)[1]
    daily_cents: defaultdict[int, int] = defaultdict(int)
    if integrity_ok:
        for item in detail_rows:
            day = _cycle_day(item.get("date"), selected_month, days_in_month)
            daily_cents[day] += int(item.get("amount_cents") or 0)

    rows = []
    cumulative_cents = 0
    if integrity_ok:
        for day in range(1, days_in_month + 1):
            cumulative_cents += daily_cents[day]
            cycle_date = date(selected_month.year, selected_month.month, day)
            rows.append({
                "month": cycle_date,
                "label": f"{day:02d}",
                "tooltip_label": f"Dia {day:02d} · competência/ciclo {selected_month:%m/%Y}",
                "projected": cumulative_cents / 100,
                "minimum_balance": reserved_cents / 100,
            })

    percent = round(expected_cents / reserved_cents * 100, 1) if reserved_cents > 0 else None
    return {
        "month": selected_month.isoformat(),
        "reserved_total": reserved_cents / 100,
        "used": expected_cents / 100,
        "detail_used": detail_cents / 100,
        "percent": percent,
        "integrity_ok": integrity_ok,
        "rows": rows,
    }

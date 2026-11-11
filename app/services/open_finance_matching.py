"""Conservative, read-only bank-transaction matching for explicit family review."""
from __future__ import annotations

from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping, Sequence

from app.services.card_billing import CARD_BILLING_TYPES, closing_day_for_card, suggest_first_bill_month
from app.services.finance_core import as_date, month_from_id, month_id, month_key, month_start

CENT = Decimal("0.01")
MAX_AMOUNT_TOLERANCE = Decimal("50.00")
MIN_AMOUNT_TOLERANCE = Decimal("1.00")
MAX_CARD_INSTALLMENT_DIFFERENCE = Decimal("0.05")
MAX_DATE_DISTANCE_DAYS = 4


def _amount(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite():
        return None
    return abs(parsed).quantize(CENT, rounding=ROUND_HALF_UP)


def amount_tolerance(planned_amount: Any) -> Decimal | None:
    """min(R$50, max(R$1, 2% of planned)), rounded to cents."""
    planned = _amount(planned_amount)
    if planned is None or planned <= 0:
        return None
    percent = (planned * Decimal("0.02")).quantize(CENT, rounding=ROUND_HALF_UP)
    return min(MAX_AMOUNT_TOLERANCE, max(MIN_AMOUNT_TOLERANCE, percent))


def match_bank_transaction(
    external: Any,
    account: Any,
    mapping: Mapping[str, Any] | None,
    items: list[Mapping[str, Any]],
    *,
    today: date,
) -> dict[str, Any]:
    """Return possible matches only; no persistence or financial mutation occurs here."""
    currency = str(getattr(external, "currency_code", "") or "").strip().upper()
    if currency != "BRL":
        return {"state": "unavailable", "reason": "currency", "candidates": []}
    if str(getattr(account, "account_type", "") or "").upper() != "BANK":
        return {"state": "unavailable", "reason": "account_type", "candidates": []}
    if not mapping or mapping.get("kind") != "BANK" or not str(mapping.get("name") or "").strip():
        return {"state": "unavailable", "reason": "unlinked", "candidates": []}

    status = str(getattr(external, "status", "") or "").strip().upper()
    if status not in {"POSTED", "PENDING"}:
        return {"state": "unavailable", "reason": "status", "candidates": []}
    direction = str(getattr(external, "transaction_type", "") or "").strip().upper()
    eligible_types = {"DEBIT": {"Fixo", "Débito/Pix"}, "CREDIT": {"Receita"}}.get(direction)
    if not eligible_types:
        return {"state": "unavailable", "reason": "direction", "candidates": []}

    external_date = as_date(getattr(external, "occurred_on", None))
    external_amount = _amount(getattr(external, "amount", None))
    if external_date is None or external_date > today or external_amount is None or external_amount == 0:
        return {"state": "unavailable", "reason": "date_or_amount", "candidates": []}

    candidates: list[dict[str, Any]] = []
    for item in items:
        if item.get("type") not in eligible_types or item.get("archived"):
            continue
        if str(item.get("account") or "").strip() != str(mapping["name"]).strip():
            continue
        planned_date = as_date(item.get("transaction_date"))
        planned_amount = _amount(item.get("amount"))
        if planned_date is None or planned_amount is None or planned_amount == 0:
            continue
        day_distance = abs((external_date - planned_date).days)
        if day_distance > MAX_DATE_DISTANCE_DAYS:
            continue
        tolerance = amount_tolerance(planned_amount)
        if tolerance is None:
            continue
        difference = (external_amount - planned_amount).quantize(CENT, rounding=ROUND_HALF_UP)
        if abs(difference) > tolerance:
            continue
        percent_difference = (difference / planned_amount * Decimal("100")).quantize(
            CENT, rounding=ROUND_HALF_UP,
        )
        item_month = month_key(item.get("month") or item.get("due_month"))
        item_block_reason = str(item.get("confirmation_block_reason") or "")
        candidates.append({
            "item_key": str(item.get("item_key") or ""),
            "transaction_id": item.get("transaction_id"),
            "description": str(item.get("description") or "Lançamento FinFam"),
            "category": str(item.get("category") or ""),
            "type": str(item.get("type") or ""),
            "account": str(item.get("account") or ""),
            "date": planned_date.isoformat(),
            "paid": bool(item.get("paid")),
            "amount": planned_amount,
            "external_amount": external_amount,
            "difference": difference,
            "percent_difference": percent_difference,
            "tolerance": tolerance,
            "day_distance": day_distance,
            "month_key": item_month,
            "block_reason": item_block_reason,
        })

    candidates.sort(key=lambda row: (row["day_distance"], abs(row["difference"]), row["description"].casefold()))
    if not candidates:
        return {"state": "none", "reason": "no_match", "candidates": []}
    if len(candidates) > 1:
        return {"state": "ambiguous", "reason": "multiple_candidates", "candidates": candidates}
    candidate = candidates[0]
    if candidate["paid"]:
        return {
            "state": "already_confirmed", "reason": "local_already_paid",
            "candidates": candidates, "candidate": candidate, "can_confirm": False,
        }
    can_confirm = status == "POSTED" and not candidate["block_reason"]
    return {
        "state": "candidate", "reason": "", "candidates": candidates,
        "candidate": candidate, "can_confirm": can_confirm,
        "block_reason": candidate["block_reason"],
        "pending_bank": status == "PENDING",
    }


def match_card_purchase(
    external: Any,
    account: Any,
    mapping: Mapping[str, Any] | None,
    local_transactions: Sequence[Mapping[str, Any]],
    cards: list[Any],
    settings: Mapping[str, Any] | None,
    *,
    today: date,
) -> dict[str, Any]:
    """Suggest a card line in its configured billing cycle; never confirm cash."""
    if str(getattr(external, "currency_code", "") or "").strip().upper() != "BRL":
        return {"state": "unavailable", "reason": "currency", "candidates": []}
    if str(getattr(account, "account_type", "") or "").upper() != "CREDIT":
        return {"state": "unavailable", "reason": "account_type", "candidates": []}
    if not mapping or mapping.get("kind") != "CREDIT" or not str(mapping.get("name") or "").strip():
        return {"state": "unavailable", "reason": "unlinked", "candidates": []}
    if str(getattr(external, "status", "") or "").strip().upper() not in {"POSTED", "PENDING"}:
        return {"state": "unavailable", "reason": "status", "candidates": []}
    if str(getattr(external, "transaction_type", "") or "").strip().upper() != "DEBIT":
        return {"state": "unavailable", "reason": "not_a_purchase", "candidates": []}

    purchase_date = as_date(getattr(external, "occurred_on", None))
    external_amount = _amount(getattr(external, "amount", None))
    if purchase_date is None or purchase_date > today or external_amount is None or external_amount == 0:
        return {"state": "unavailable", "reason": "date_or_amount", "candidates": []}
    card_name = str(mapping["name"]).strip()
    closing_day = closing_day_for_card(card_name, cards, settings)
    first_bill = date.fromisoformat(suggest_first_bill_month(purchase_date, closing_day))
    first_bill_id = month_id(first_bill)
    installment_number = getattr(external, "installment_number", None)
    total_installments = getattr(external, "total_installments", None)
    try:
        installment_number = int(installment_number) if installment_number is not None else None
        total_installments = int(total_installments) if total_installments is not None else None
    except (TypeError, ValueError, OverflowError):
        return {"state": "unavailable", "reason": "invalid_installment_metadata", "candidates": []}
    if installment_number is not None and installment_number < 1:
        return {"state": "unavailable", "reason": "invalid_installment_metadata", "candidates": []}
    if total_installments is not None and total_installments < 1:
        return {"state": "unavailable", "reason": "invalid_installment_metadata", "candidates": []}
    if installment_number is not None and total_installments is not None and installment_number > total_installments:
        return {"state": "unavailable", "reason": "invalid_installment_metadata", "candidates": []}
    if first_bill_id is None:
        return {"state": "unavailable", "reason": "cycle", "candidates": []}
    invoice_month = month_from_id(first_bill_id + (installment_number or 1) - 1)
    invoice_month_id = month_id(invoice_month)
    if invoice_month is None or invoice_month_id is None:
        return {"state": "unavailable", "reason": "cycle", "candidates": []}

    candidates: list[dict[str, Any]] = []
    for row in local_transactions:
        if row.get("archived") or row.get("type") not in CARD_BILLING_TYPES:
            continue
        if not bool(row.get("card_flag")) or str(row.get("account_card") or "").strip() != card_name:
            continue
        local_date = as_date(row.get("transaction_date"))
        if local_date is None or abs((purchase_date - local_date).days) > MAX_DATE_DISTANCE_DAYS:
            continue
        local_start = month_start(row.get("start_month"))
        local_end = month_start(row.get("end_month")) if row.get("end_month") else None
        local_start_id = month_id(local_start) if local_start else None
        local_end_id = month_id(local_end) if local_end else None
        if local_start_id is None or local_start_id != first_bill_id:
            continue
        if invoice_month_id < local_start_id:
            continue
        if local_end_id is not None and invoice_month_id > local_end_id:
            continue
        try:
            local_installments = max(1, int(row.get("installment_count") or 1))
        except (TypeError, ValueError, OverflowError):
            continue
        if row.get("type") == "Crédito à vista" and local_installments != 1:
            continue
        if installment_number is not None and installment_number > local_installments:
            continue
        if total_installments is not None and total_installments != local_installments:
            continue
        if row.get("type") == "Crédito à vista" and installment_number not in (None, 1):
            continue
        planned_amount = _amount(row.get("amount"))
        if planned_amount is None or planned_amount == 0:
            continue
        difference = (external_amount - planned_amount).quantize(CENT, rounding=ROUND_HALF_UP)
        if abs(difference) > MAX_CARD_INSTALLMENT_DIFFERENCE:
            continue
        candidates.append({
            "item_key": f"{month_key(invoice_month)}:tx:{row.get('id')}",
            "transaction_id": row.get("id"),
            "description": str(row.get("description") or "Compra no cartão"),
            "category": str(row.get("category") or ""),
            "date": local_date.isoformat(),
            "invoice_month": month_key(invoice_month),
            "first_bill_month": month_key(first_bill),
            "closing_day": closing_day,
            "amount": planned_amount,
            "external_amount": external_amount,
            "difference": difference,
            "installment_number": installment_number or 1,
            "total_installments": total_installments or local_installments,
            "day_distance": abs((purchase_date - local_date).days),
        })
    candidates.sort(key=lambda row: (row["day_distance"], abs(row["difference"]), row["description"].casefold()))
    if not candidates:
        return {"state": "none", "reason": "no_match", "candidates": []}
    if len(candidates) > 1:
        return {"state": "ambiguous", "reason": "multiple_candidates", "candidates": candidates}
    candidate = candidates[0]
    return {
        "state": "candidate", "reason": "card_review_only", "candidates": candidates,
        "candidate": candidate, "can_confirm": False,
    }


def enforce_one_to_one(matches: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Mark all external rows ambiguous when they compete for one local item."""
    output = {key: dict(value) for key, value in matches.items()}
    owners: Counter[str] = Counter()
    for result in output.values():
        for candidate in result.get("candidates", []):
            item_key = str(candidate.get("item_key") or "")
            if item_key:
                owners[item_key] += 1
    for result in output.values():
        if result.get("state") not in {"candidate", "already_confirmed"}:
            continue
        candidate = result.get("candidate") or {}
        if owners[str(candidate.get("item_key") or "")] > 1:
            result.update(
                state="ambiguous", reason="competing_bank_transactions",
                can_confirm=False,
                message="Mais de uma transação bancária pode corresponder a este lançamento.",
            )
    return output

"""Helpers puros para calcular linhas de fatura por competência.

Os overrides são somente uma apresentação/cálculo da parcela naquele mês: não
alteram o valor-base da compra nem o total_amount armazenado.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any, Iterable

_CENT = Decimal("0.01")


def decimal_amount(value: Any) -> Decimal:
    if isinstance(value, str):
        text = value.strip().replace("R$", "").replace(" ", "")
        if "," in text:
            text = text.replace(".", "").replace(",", ".")
        value = text
    try:
        amount = Decimal(str(value or 0))
        return amount if amount.is_finite() else Decimal("0.00")
    except (TypeError, ValueError, ArithmeticError):
        return Decimal("0.00")


def month_key(month: Any) -> str:
    if hasattr(month, "strftime"):
        return month.strftime("%Y-%m")
    text = str(month or "")
    return text[:7]


def line_amount_for_month(tx: dict[str, Any], month: Any) -> Decimal:
    """Base line amount, replaced only by an override for this competence."""
    overrides = tx.get("installment_overrides")
    key = month_key(month)
    if isinstance(overrides, dict) and key in overrides:
        return decimal_amount(overrides[key])
    return decimal_amount(tx.get("amount"))


def allocate_invoice_lines(purchases: list[dict[str, Any]]) -> float:
    """Allocate fractional cents deterministically while preserving invoice total."""
    raw = [decimal_amount(row.pop("_raw_amount", row.get("amount"))) for row in purchases]
    total = sum(raw, Decimal("0.00")).quantize(_CENT, rounding=ROUND_HALF_UP)
    rounded = [amount.quantize(_CENT, rounding=ROUND_FLOOR) for amount in raw]
    remaining = int((total - sum(rounded, Decimal("0.00"))) / _CENT)
    if not 0 <= remaining <= len(rounded):
        rounded = [amount.quantize(_CENT, rounding=ROUND_HALF_UP) for amount in raw]
        total = sum(rounded, Decimal("0.00"))
    else:
        order = sorted(
            range(len(raw)),
            key=lambda index: (
                -(raw[index] - rounded[index]),
                int(purchases[index].get("id") or 0), index,
            ),
        )
        for index in order[:remaining]:
            rounded[index] += _CENT
    for row, amount in zip(purchases, rounded):
        row["amount"] = float(amount)
    return float(total)


def allocated_month_lines(
    transactions: Iterable[dict[str, Any]], month: Any,
) -> tuple[float, dict[int, float]]:
    """Return rounded total and per-transaction allocated values for card lines."""
    month_text = month_key(month)
    rows: list[dict[str, Any]] = []
    for index, tx in enumerate(transactions):
        value = line_amount_for_month(tx, month_text)
        rows.append({"id": tx.get("id"), "_index": index, "_raw_amount": value})
    total = allocate_invoice_lines(rows)
    return total, {int(row["_index"]): float(row["amount"]) for row in rows}


def effective_card_amount(tx: dict[str, Any], month: Any) -> float:
    """Resolve one line to cents (ordinary no-override value remains unrounded)."""
    overrides = tx.get("installment_overrides")
    key = month_key(month)
    if isinstance(overrides, dict) and key in overrides:
        return float(decimal_amount(overrides[key]).quantize(_CENT, rounding=ROUND_HALF_UP))
    return float(tx.get("amount") or 0)

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from app.services.finance_core import month_from_id, month_id


FGTS_BASE_MONTH = date(2026, 11, 1)
_SOURCE_BASE = Decimal("0")
_SOURCE_RATE = Decimal("0.08")
# The demo starts with no imported historical FGTS balance.
_SOURCE_OPENING_BALANCE = Decimal("0")


def _setting_decimal(settings: dict[str, Any], key: str, default: str) -> Decimal:
    try:
        value = Decimal(str(settings.get(key, default)))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"Configuração FGTS inválida: {key}.") from None
    if not value.is_finite() or value < 0:
        raise ValueError(f"Configuração FGTS inválida: {key}.")
    return value


def project_fgts(rows: Iterable[dict[str, Any]], settings: dict[str, Any]) -> list[dict[str, Any]]:
    """Anexa a estimativa patrimonial FGTS sem alterar as métricas de caixa.

    O mês-base demonstrativo não tem saldo histórico importado. Os meses
    seguintes usam salários sintéticos configuráveis; dezembro dobra o aporte.
    A precisão Decimal é preservada até a montagem dos valores de saída.
    """
    source_rows = list(rows)
    if not source_rows:
        return []

    indexed = [(month_id(row.get("month")), row) for row in source_rows]
    valid_indexes = [idx for idx, _ in indexed if idx is not None]
    if not valid_indexes:
        return [{**row, "fgts_monthly": None, "fgts_accumulated": None} for row in source_rows]

    first_idx = month_id(FGTS_BASE_MONTH)
    assert first_idx is not None
    last_idx = max(valid_indexes)
    if last_idx < first_idx:
        return [{**row, "fgts_monthly": None, "fgts_accumulated": None} for row in source_rows]

    salary_hener = _setting_decimal(settings, "fgts_salary_hener", "0.0")
    salary_ju = _setting_decimal(settings, "fgts_salary_ju", "0.0")
    rate = _setting_decimal(settings, "fgts_rate", "0.08")
    if rate > 1:
        raise ValueError("A alíquota FGTS deve estar entre 0 e 1.")

    monthly_by_index: dict[int, Decimal] = {}
    accumulated_by_index: dict[int, Decimal] = {}
    accumulated = _SOURCE_OPENING_BALANCE
    for index in range(first_idx, last_idx + 1):
        month = month_from_id(index)
        if month is None:
            continue
        if index == first_idx:
            monthly_base = _SOURCE_BASE
        else:
            monthly_base = salary_hener + salary_ju
        monthly_amount = monthly_base * rate
        if month.month == 12:
            monthly_amount *= Decimal(2)
        accumulated += monthly_amount
        monthly_by_index[index] = monthly_amount
        accumulated_by_index[index] = accumulated

    result = []
    for index, row in indexed:
        if index is None or index < first_idx or index not in monthly_by_index:
            result.append({**row, "fgts_monthly": None, "fgts_accumulated": None})
            continue
        result.append({
            **row,
            "fgts_monthly": float(monthly_by_index[index]),
            "fgts_accumulated": float(accumulated_by_index[index]),
        })
    return result

"""Horizonte de apresentação/cálculo; não altera nem prorroga lançamentos."""
from __future__ import annotations

from datetime import date
from typing import Any, Iterable

from app.services.finance_core import month_from_id, month_id, month_start


def effective_end(txs: Iterable[dict[str, Any]], settings: dict[str, Any], requested: Any = None,
                  scenarios: Iterable[dict[str, Any]] | None = None) -> date:
    candidates = [month_start(settings.get("forecast_end") or "2030-12-01") or date(2030, 12, 1)]
    anchor = month_start(requested or date.today()) or date.today().replace(day=1)
    anchor_idx = month_id(anchor)
    assert anchor_idx is not None
    candidates.append(month_from_id(min(anchor_idx + 11, 9999 * 12 + 12)))
    for row in txs:
        if row.get("archived"):
            continue
        for field in ("start_month", "end_month", "transaction_date"):
            value = row.get(field)
            if value:
                d = month_start(value)
                if d:
                    candidates.append(d)
        if row.get("end_month") is None and row.get("start_month"):
            start_idx = month_id(row["start_month"])
            if start_idx is not None:
                candidates.append(month_from_id(min(start_idx + 11, 9999 * 12 + 12)))
    for scenario in scenarios or []:
        start_idx = month_id(scenario.get("start_month"))
        if start_idx is not None:
            duration = max(1, int(scenario.get("months") or 1))
            candidates.append(month_from_id(min(start_idx + duration - 1, 9999 * 12 + 12)))
    for goal in settings.get("family_goals", []) or []:
        value = goal.get("target_month")
        if value:
            d = month_start(value)
            if d:
                candidates.append(d)
    return max(candidates)

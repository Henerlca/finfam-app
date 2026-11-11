from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.services.charts import classify_pot


def _pot_map(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row.get("pot", "")): row for row in rows}


def suggest_reallocation(
    history: list[dict[str, Any]],
    current_rows: Iterable[dict[str, Any]],
    target_pot: str,
    source_pot: str,
) -> dict[str, Any]:
    """Draft a zero-sum reallocation using fully settled, closed-month data only."""
    current = _pot_map(current_rows)
    target_now = float(current.get(target_pot, {}).get("reserved", 0) or 0)
    source_now = float(current.get(source_pot, {}).get("reserved", 0) or 0)
    target_history = []
    source_history = []
    for month in history:
        rows = _pot_map(month.get("rows", []))
        target = rows.get(target_pot)
        source = rows.get(source_pot)
        if target is not None:
            target_history.append({
                "month": month.get("month"),
                "used": float(target.get("used", 0) or 0),
                "reserved": float(target.get("reserved", 0) or 0),
                "confirmed_count": int(target.get("confirmed_count", 0) or 0),
            })
        if source is not None:
            source_history.append({
                "month": month.get("month"),
                "used": float(source.get("used", 0) or 0),
                "reserved": float(source.get("reserved", 0) or 0),
                "confirmed_count": int(source.get("confirmed_count", 0) or 0),
            })

    months = min(len(target_history), len(source_history))
    target_records = sum(row["confirmed_count"] for row in target_history)
    source_records = sum(row["confirmed_count"] for row in source_history)
    average_used = round(sum(row["used"] for row in target_history) / len(target_history), 2) if target_history else 0.0
    average_reserved = round(sum(row["reserved"] for row in target_history) / len(target_history), 2) if target_history else 0.0
    source_average_used = round(sum(row["used"] for row in source_history) / len(source_history), 2) if source_history else 0.0
    increase = 0.0
    if months >= 2 and target_records and source_records:
        gap = max(0.0, round(average_used - target_now, 2))
        reducible_source = max(0.0, round(source_now - source_average_used, 2))
        increase = min(gap, reducible_source)
    increase = round(increase, 2)
    return {
        "months": months,
        "target_records": target_records,
        "source_records": source_records,
        "target_pot": target_pot,
        "source_pot": source_pot,
        "current_target": target_now,
        "current_source": source_now,
        "average_used": average_used,
        "average_reserved": average_reserved,
        "source_average_used": source_average_used,
        "increase": increase,
        "suggested_target": round(target_now + increase, 2),
        "suggested_source": round(source_now - increase, 2),
        "preliminary": months < 3,
        "available": months >= 2 and bool(target_records) and bool(source_records) and increase > 0,
        "target_group": classify_pot(target_pot),
        "source_group": classify_pot(source_pot),
    }

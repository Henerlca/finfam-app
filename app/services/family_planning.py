"""Serviços puros para o planejamento familiar.

Este módulo só organiza resultados do motor financeiro existente. Não grava metas,
lançamentos ou status, nem contém dependências de HTTP, SQLAlchemy ou banco de dados.
As estruturas retornadas são deliberadamente simples para que as rotas possam
serializá-las diretamente nos contextos Jinja.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any
import unicodedata

from app.services.charts import build_line_chart
from app.services.card_installments import allocate_invoice_lines, line_amount_for_month
from app.services.finance_core import (
    FAMILY,
    card_limits,
    cashflow_items,
    month_id,
    month_key,
    month_range,
    month_start,
    normalize_transaction,
    projection,
)


def _number(value: Any) -> float:
    if value in (None, "", "-"):
        return 0.0
    if isinstance(value, str):
        text = value.strip().replace("R$", "").replace(" ", "")
        if "," in text:
            text = text.replace(".", "").replace(",", ".")
        value = text
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _rounded(value: Any) -> float:
    return round(_number(value), 2)


_CENT = Decimal("0.01")


def _decimal_amount(value: Any) -> Decimal:
    try:
        amount = Decimal(str(value or 0))
        return amount if amount.is_finite() else Decimal("0.00")
    except (TypeError, ValueError):
        return Decimal("0.00")


def _sum_currency(values: Iterable[Any]) -> float:
    total = sum((_decimal_amount(value) for value in values), Decimal("0.00"))
    return float(total.quantize(_CENT, rounding=ROUND_HALF_UP))


def _allocate_invoice_lines(purchases: list[dict[str, Any]]) -> float:
    """Compatibility wrapper around the shared deterministic invoice allocator."""
    return allocate_invoice_lines(purchases)


def _norm(value: Any) -> str:
    text = str(value or "").strip().casefold()
    return "".join(
        char for char in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(char)
    )


def _month_values(months: Iterable[Any]) -> list[date]:
    """Normalize a supplied horizon, preserving its date meaning and removing duplicates."""
    result: list[date] = []
    seen: set[str] = set()
    for value in months or ():
        if isinstance(value, Mapping):
            value = value.get("month") or value.get("month_key")
        try:
            current = month_start(value)
        except (TypeError, ValueError):
            continue
        if current is None:
            continue
        key = month_key(current)
        if key not in seen:
            seen.add(key)
            result.append(current)
    return sorted(result)


def _active(tx: dict[str, Any], current: date) -> bool:
    current_idx = month_id(current)
    start_idx = tx.get("start_id")
    end_idx = tx.get("end_id")
    return (
        current_idx is not None
        and start_idx is not None
        and int(start_idx) <= current_idx
        and current_idx <= int(end_idx or 999999)
    )


def _member_matches(tx: dict[str, Any], person: str) -> bool:
    return person == FAMILY or tx.get("member") == person


def _card_matches(tx: dict[str, Any], person: str) -> bool:
    if person == FAMILY:
        return True
    if person == "Ju":
        return tx.get("account_card") == "Nubank cartão"
    if person == "Hener":
        return tx.get("account_card") != "Nubank cartão"
    return True


def _status_value(bucket: Any, invoice_key: str, card_name: str) -> bool:
    """Read both App cashflow maps and convenient test/list representations."""
    if isinstance(bucket, bool):
        return bucket
    if isinstance(bucket, Mapping):
        candidates = (invoice_key, f"invoice:{card_name}", card_name)
        for candidate in candidates:
            if candidate not in bucket:
                continue
            value = bucket[candidate]
            if isinstance(value, Mapping):
                return bool(value.get("paid"))
            return bool(value)
        # Some callers pass {"statuses": {item_key: {"paid": true}}}.
        for key in ("statuses", "items", "cashflow"):
            if key in bucket:
                found = _status_value(bucket[key], invoice_key, card_name)
                if found:
                    return True
        return False
    if isinstance(bucket, (list, tuple, set)):
        for row in bucket:
            if isinstance(row, Mapping):
                row_key = str(row.get("item_key") or row.get("key") or "")
                if row_key in (invoice_key, f"invoice:{card_name}", card_name):
                    return bool(row.get("paid"))
        return False
    return False


def _paid_invoice(
    statuses_by_month: Mapping[Any, Any] | None,
    current: date,
    invoice_key: str,
    card_name: str,
) -> bool:
    if not statuses_by_month:
        return False
    key = month_key(current)
    bucket = statuses_by_month.get(key)
    if bucket is None:
        bucket = statuses_by_month.get(current)
    if bucket is None:
        bucket = statuses_by_month.get(current.isoformat())
    # A single-month status map is also accepted for callers that already selected month.
    if bucket is None and invoice_key in statuses_by_month:
        bucket = statuses_by_month
    return _status_value(bucket, invoice_key, card_name)


def _purchase_row(tx: dict[str, Any], month: Any = None) -> dict[str, Any]:
    overrides = tx.get("installment_overrides") or {}
    month_text = month_key(month) if month is not None else ""
    return {
        "id": tx.get("id"),
        "description": tx.get("description", ""),
        "category": tx.get("category", ""),
        "member": tx.get("member", ""),
        "type": tx.get("type", ""),
        "amount": _rounded(tx.get("amount")),
        "_raw_amount": line_amount_for_month(tx, month) if month is not None else tx.get("amount", 0),
        "total_amount": _rounded(tx.get("total_amount")),
        "installment_count": int(tx.get("installment_count") or 1),
        "installment": tx.get("type") == "Parcelado",
        "installment_adjusted": bool(month_text and isinstance(overrides, dict) and month_text in overrides),
        "linked_pot": tx.get("linked_pot", ""),
        "start_month": tx.get("start_month"),
        "end_month": tx.get("end_month"),
        "transaction_date": tx.get("transaction_date"),
    }


def installment_number_for_month(tx: Mapping[str, Any], month: Any) -> int | None:
    """Return the display-only installment ordinal for one card competence."""
    if str(tx.get("type") or "") != "Parcelado":
        return None
    try:
        count = int(tx.get("installment_count") or 1)
    except (TypeError, ValueError):
        return None
    if count < 2:
        return None
    current_idx = month_id(month)
    start_idx = month_id(tx.get("start_month"))
    end_idx = month_id(tx.get("end_month"))
    if current_idx is None or start_idx is None or current_idx < start_idx:
        return None
    if end_idx is not None and current_idx > end_idx:
        return None
    number = current_idx - start_idx + 1
    return number if 1 <= number <= count else None


def card_calendar(
    txs: Iterable[dict[str, Any]],
    cards: Iterable[dict[str, Any]],
    months: Iterable[Any],
    person: str = FAMILY,
    statuses_by_month: Mapping[Any, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return a card-first invoice calendar for exactly the supplied months.

    ``months`` is never extended by this function. Each card row has ``months``
    with the predicted invoice, installment subtotal, purchases, payment status,
    and the commitment/available values from :func:`card_limits` evaluated on the
    first day of that competence. ``card_by_name`` is repeated on each month and
    contains the faithful per-card invoice total, which avoids double-counting
    purchase detail as another expense.
    """
    normalized = [
        normalize_transaction(tx) for tx in txs
        if not tx.get("archived", False)
    ]
    card_rows = [dict(card) for card in cards]
    horizon = _month_values(months)
    by_name = {str(card.get("name", "")): card for card in card_rows if card.get("name")}
    # Keep cards present in transactions visible even when a partially populated
    # snapshot did not include the corresponding lookup row.
    for tx in normalized:
        if tx.get("card_flag") and tx.get("account_card") and tx["account_card"] not in by_name:
            by_name[tx["account_card"]] = {"name": tx["account_card"], "bank": "", "limit_amount": 0.0}

    result: list[dict[str, Any]] = []
    invoice_by_month: dict[str, dict[str, float]] = {}
    for card_name, card in by_name.items():
        months_result: list[dict[str, Any]] = []
        for current in horizon:
            purchases = [
                _purchase_row(tx, current)
                for tx in normalized
                if tx.get("card_flag")
                and tx.get("account_card") == card_name
                and _active(tx, current)
                and _card_matches(tx, person)
                and tx.get("type") != "Receita"
            ]
            for purchase in purchases:
                installment_number = installment_number_for_month(purchase, current)
                if installment_number is not None:
                    purchase["installment_number"] = installment_number
            invoice = _allocate_invoice_lines(purchases)
            installments = _sum_currency(
                row["amount"] for row in purchases if row.get("type") == "Parcelado"
            )
            card_limit_rows = card_limits(
                [tx for tx in normalized if _card_matches(tx, person)],
                [card],
                current.replace(day=1),
            )
            limits = card_limit_rows[0] if card_limit_rows else {
                "limit": _number(card.get("limit_amount")),
                "current_invoice": invoice,
                "future_installments": 0.0,
                "total_committed": invoice,
                "available": _number(card.get("limit_amount")) - invoice,
                "utilization": None,
            }
            key = f"{month_key(current)}:invoice:{card_name}"
            paid = _paid_invoice(statuses_by_month, current, key, card_name)
            card_by_name = {card_name: _rounded(invoice)}
            month_row = {
                "month": current,
                "month_key": month_key(current),
                "invoice": _rounded(invoice),
                "invoice_predicted": _rounded(invoice),
                "invoice_prevista": _rounded(invoice),
                "predicted_invoice": _rounded(invoice),
                "parcelas": _rounded(installments),
                "installments": _rounded(installments),
                "committed": _rounded(limits.get("total_committed")),
                "commit": _rounded(limits.get("total_committed")),
                "commitment": _rounded(limits.get("total_committed")),
                "total_committed": _rounded(limits.get("total_committed")),
                "available": _rounded(limits.get("available")),
                "limit": _rounded(limits.get("limit")),
                "utilization": limits.get("utilization"),
                "current_invoice": _rounded(limits.get("current_invoice")),
                "future_installments": _rounded(limits.get("future_installments")),
                "invoice_key": key,
                "paid": paid,
                "status": "paid" if paid else "projected",
                "invoice_status": "paid" if paid else "projected",
                "status_label": "Baixa confirmada" if paid else "Fatura prevista",
                "purchases": purchases,
                "details": purchases,
                "card_by_name": card_by_name,
            }
            months_result.append(month_row)
            invoice_by_month.setdefault(month_key(current), {})[card_name] = _rounded(invoice)
        total_invoice = sum(_number(row["invoice"]) for row in months_result)
        result.append({
            "card": card_name,
            "name": card_name,
            "bank": card.get("bank", ""),
            "limit": _rounded(card.get("limit_amount")),
            "months": months_result,
            "rows": months_result,
            "card_by_name": {card_name: _rounded(total_invoice)},
        })
    totals_by_card = {
        card_name: _rounded(sum(_number(month_values.get(card_name)) for month_values in invoice_by_month.values()))
        for card_name in by_name
    }
    for card in result:
        card["card_by_name"] = dict(totals_by_card)
        for month_row in card["months"]:
            month_row["card_by_name"] = dict(invoice_by_month.get(month_row["month_key"], {}))
    return result


def _scenario_signs(kind: Any, amount: float) -> tuple[str, float, float]:
    """Return (canonical kind, user cash effect, motor outflow sign).

    The user-facing effect is positive for extra income and negative for a new
    expense/contribution. ``projection`` subtracts ``monthly_amount`` from the
    ending balance, so its signed value is the inverse of the cash effect.
    """
    canonical = _norm(kind)
    if canonical in {"income", "receita", "renda", "ganho"}:
        return "income", amount, -amount
    if canonical in {"savings", "poupanca", "poupanca extra", "caixinha", "contribuicao", "contribuicao extra"}:
        return "savings", -amount, amount
    if canonical in {"expense", "despesa", "consumo", "essencial", "lazer", "gasto"}:
        return "expense", -amount, amount
    raise ValueError("kind deve ser income, expense ou savings")


def _scenario_active(current: Any, start_month: Any, duration: int) -> bool:
    current_idx, start_idx = month_id(current), month_id(start_month)
    return (
        current_idx is not None
        and start_idx is not None
        and duration > 0
        and start_idx <= current_idx < start_idx + duration
    )


def simulate_family(
    txs: Iterable[dict[str, Any]],
    accounts: Iterable[dict[str, Any]],
    settings: Mapping[str, Any] | None,
    closed: Mapping[str, bool] | None,
    person: str,
    kind: str,
    amount: Any,
    start_month: Any,
    duration: Any,
    end: Any,
) -> dict[str, Any]:
    """Compare the base projection with one temporary family scenario.

    ``income`` adds money, while ``expense`` and ``savings`` reduce available
    cash. Savings also overlays the displayed monthly savings field and is
    accumulated in the presentation rows; no transaction or pot is created.
    """
    settings = settings or {}
    value = _number(amount)
    months = max(0, int(_number(duration)))
    start = month_start(start_month)
    last = month_start(end)
    if start is None or last is None:
        raise ValueError("Informe as competências inicial e final do cenário")
    canonical, cash_effect, motor_amount = _scenario_signs(kind, value)
    minimum = _number(settings.get("minimum_balance", 0.0))
    base_month = settings.get("base_month") or start
    base_rows = projection(
        txs, accounts, base_month, last, person,
        closed_months=dict(closed or {}), minimum_balance=minimum,
    )
    alternative_rows = projection(
        txs, accounts, base_month, last, person,
        closed_months=dict(closed or {}), minimum_balance=minimum,
        scenarios=[{"start_month": start, "months": months, "monthly_amount": motor_amount}],
    )
    # projection is deterministic and both calls use the same bounds; zip keeps
    # the result safe even when a malformed empty range is supplied.
    base_output: list[dict[str, Any]] = []
    alternative_output: list[dict[str, Any]] = []
    compare: list[dict[str, Any]] = []
    extra_accumulated = 0.0
    for base, projected in zip(base_rows, alternative_rows):
        active = _scenario_active(base.get("month"), start, months)
        effect = cash_effect if active else 0.0
        alt = dict(projected)
        # AE/AF are calculated by the unchanged motor. The scenario is an
        # informational overlay: income/expense/savings affect these displayed
        # monthly results without changing the Excel formulas in finance_core.
        alt["monthly_balance_with_prior_impact"] = _rounded(
            _number(base.get("monthly_balance_with_prior_impact")) + effect
        )
        alt["monthly_balance_without_prior_impact"] = _rounded(
            _number(base.get("monthly_balance_without_prior_impact")) + effect
        )
        if canonical == "income" and active:
            alt["income"] = _rounded(_number(base.get("income")) + value)
        if canonical == "expense" and active:
            alt["scenario_expense"] = _rounded(value)
        if canonical == "savings" and active:
            extra_accumulated += value
            alt["savings_net"] = _rounded(_number(base.get("savings_net")) + value)
            alt["contribution_extra"] = _rounded(value)
        if canonical == "savings":
            alt["savings_accumulated"] = _rounded(
                _number(base.get("savings_accumulated")) + extra_accumulated
            )
        alt["scenario_effect"] = _rounded(effect)
        alt["scenario_active"] = active
        alt["scenario_kind"] = canonical
        alt["scenario_monthly_amount"] = _rounded(value if active else 0.0)
        # projected_ending_balance/gap already include the signed motor scenario
        # and therefore preserve rolling cash exactly.
        base_copy = dict(base)
        base_copy["scenario_effect"] = 0.0
        base_copy["scenario_active"] = False
        base_output.append(base_copy)
        alternative_output.append(alt)
        compare.append({
            "month": base.get("month"),
            "closed": bool(base.get("closed")),
            "base": base_copy,
            "alternative": alt,
            "base_ae": _rounded(base.get("monthly_balance_with_prior_impact")),
            "alternative_ae": alt["monthly_balance_with_prior_impact"],
            "alt_ae": alt["monthly_balance_with_prior_impact"],
            "base_af": _rounded(base.get("monthly_balance_without_prior_impact")),
            "alternative_af": alt["monthly_balance_without_prior_impact"],
            "alt_af": alt["monthly_balance_without_prior_impact"],
            "base_ag": _rounded(base.get("projected_ending_balance")),
            "alternative_ag": _rounded(alt.get("projected_ending_balance")),
            "alt_ag": _rounded(alt.get("projected_ending_balance")),
            "base_floor": _rounded(base.get("minimum_balance", minimum)),
            "alternative_floor": _rounded(alt.get("minimum_balance", minimum)),
            "floor": _rounded(alt.get("minimum_balance", minimum)),
            "scenario_effect": _rounded(effect),
        })
    chart = build_line_chart(
        compare,
        [
            ("base_ag", "Cenário-base", "#1b4d4b", False),
            ("alternative_ag", "Cenário alternativo", "#b47b35", False),
            ("floor", "Piso de segurança", "#a9433d", True),
        ],
        "family-scenario",
        "Saldo bancário no cenário-base, cenário alternativo e piso de segurança",
        max_x_labels=5,
    )
    summary_base = base_output[-1] if base_output else None
    summary_alternative = alternative_output[-1] if alternative_output else None
    summary = {
        "month": summary_alternative.get("month") if summary_alternative else None,
        "base": summary_base,
        "alternative": summary_alternative,
        "base_ending_balance": _rounded(summary_base.get("projected_ending_balance")) if summary_base else None,
        "alternative_ending_balance": _rounded(summary_alternative.get("projected_ending_balance")) if summary_alternative else None,
        "scenario_effect": _rounded(summary_alternative.get("scenario_effect")) if summary_alternative else 0.0,
    }
    return {
        "kind": canonical,
        "amount": _rounded(value),
        "duration": months,
        "start_month": start,
        "end_month": last,
        "monthly_effect": _rounded(cash_effect),
        "signed_monthly_amount": _rounded(cash_effect),
        "motor_monthly_amount": _rounded(motor_amount),
        "floor": _rounded(minimum),
        "base": base_output,
        "alternative": alternative_output,
        "base_rows": base_output,
        "alternative_rows": alternative_output,
        "compare": compare,
        "chart": chart,
        "summary": summary,
        "last_visible": summary,
        "note": "Cenário informativo; nenhum lançamento ou pote foi alterado.",
    }


def _goal_value(goal: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in goal and goal[name] not in (None, ""):
            return goal[name]
    return None


def _goal_meta(goal: Mapping[str, Any]) -> dict[str, Any]:
    opening = _goal_value(goal, "opening_amount", "balance", "saldo_anterior")
    opening_month = _goal_value(goal, "opening_month", "balance_month")
    target_month = _goal_value(goal, "target_month", "deadline", "target")
    return {
        "id": _goal_value(goal, "id") or "",
        "name": str(_goal_value(goal, "name") or "").strip(),
        "category": str(_goal_value(goal, "category") or "").strip(),
        "target_amount": _rounded(_goal_value(goal, "target_amount", "target_value", "amount")),
        "target_month": month_start(target_month) if target_month else None,
        "opening_amount": None if opening is None else _rounded(opening),
        "opening_month": month_start(opening_month) if opening_month else None,
    }


def _status_for_event(paid_by_month: Any, current: date, item_key: str, tx_id: Any) -> bool:
    if not paid_by_month:
        return False
    key = month_key(current)
    bucket = None
    if isinstance(paid_by_month, Mapping):
        bucket = paid_by_month.get(key)
        if bucket is None:
            bucket = paid_by_month.get(current)
        if bucket is None:
            bucket = paid_by_month.get(current.isoformat())
        if bucket is None and item_key in paid_by_month:
            bucket = paid_by_month
    if isinstance(bucket, bool):
        return bucket
    if isinstance(bucket, Mapping):
        for candidate in (item_key, f"{key}:tx:{tx_id}", str(tx_id)):
            if candidate in bucket:
                value = bucket[candidate]
                return bool(value.get("paid")) if isinstance(value, Mapping) else bool(value)
        for nested in ("statuses", "items", "cashflow"):
            if nested in bucket and _status_for_event({key: bucket[nested]}, current, item_key, tx_id):
                return True
        return False
    if isinstance(bucket, (list, tuple, set)):
        for row in bucket:
            if isinstance(row, Mapping):
                if str(row.get("item_key") or row.get("key") or "") in (item_key, f"{key}:tx:{tx_id}"):
                    return bool(row.get("paid"))
                if row.get("transaction_id") is not None and str(row.get("transaction_id")) == str(tx_id):
                    return bool(row.get("paid"))
            elif str(row) in (item_key, f"{key}:tx:{tx_id}", str(tx_id)):
                return True
    return False


def _forecast_category_value(row: Mapping[str, Any], category: str) -> float | None:
    values = row.get("savings_by_category")
    if isinstance(values, Mapping):
        wanted = _norm(category)
        for name, value in values.items():
            if _norm(name) == wanted:
                return _number(value)
    for field in ("planned_contribution", "contribution", "planned_savings"):
        if field in row and row.get(field) is not None:
            return _number(row.get(field))
    return None


def goal_progress(
    goal: Mapping[str, Any],
    txs: Iterable[dict[str, Any]],
    paid_by_month: Mapping[Any, Any] | None,
    forecast_rows: Iterable[Mapping[str, Any]],
    as_of: Any,
    accounts: Iterable[dict[str, Any]] | None = None,
    cards: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Calculate a cautious goal balance from confirmed events and a plan.

    Only ``Caixinha`` and ``Saque Caixinha`` rows whose *category* matches the
    goal are considered. If ``opening_amount`` is supplied, rows before its
    ``opening_month`` are ignored so a previously known balance is not counted
    twice. Confirmed events require a paid state in ``paid_by_month``; no implicit
    contribution is invented.
    """
    meta = _goal_meta(goal)
    current = month_start(as_of)
    target = meta["target_month"]
    if current is None:
        raise ValueError("Informe as_of como data ou competência válida")
    if target is None:
        raise ValueError("A meta precisa de uma competência alvo")
    target_idx, current_idx = month_id(target), month_id(current)
    if target_idx is None or current_idx is None:
        raise ValueError("Competência alvo inválida")
    category = meta["category"]
    candidates = [
        normalize_transaction(tx) for tx in txs
        if not tx.get("archived", False)
        and tx.get("type") in ("Caixinha", "Saque Caixinha")
        and _norm(tx.get("category")) == _norm(category)
    ]
    rows = [dict(row) for row in forecast_rows or ()]
    row_months = []
    for row in rows:
        value = month_start(row.get("month"))
        if value is not None:
            row_months.append(value)
    earliest = min(
        [tx["start_month"] for tx in candidates if tx.get("start_month") is not None]
        + [current],
    )
    horizon_end = max([target] + row_months)
    # cashflow_items is the source of event keys/status semantics when a root
    # snapshot is available; the fallback below keeps the service pure/testable.
    accounts_list = list(accounts or [])
    cards_list = list(cards or [])
    events: list[dict[str, Any]] = []
    for month in month_range(earliest, horizon_end):
        items = cashflow_items(candidates, cards_list, accounts_list, month, FAMILY)
        for item in items:
            if item.get("type") not in ("Caixinha", "Saque Caixinha"):
                continue
            event = dict(item)
            event["month"] = month
            event["month_key"] = month_key(month)
            event["net_goal"] = _number(item.get("amount")) if item.get("type") == "Caixinha" else -_number(item.get("amount"))
            event["confirmed"] = _status_for_event(
                paid_by_month, month, str(item.get("item_key", "")), item.get("transaction_id")
            )
            events.append(event)
    opening_amount = meta["opening_amount"]
    opening_idx = month_id(meta["opening_month"]) if meta["opening_month"] else None
    def included_in_snapshot(event_month: Any) -> bool:
        idx = month_id(event_month)
        if idx is None:
            return False
        if opening_amount is not None and opening_idx is not None:
            return idx >= opening_idx
        if opening_amount is not None:
            # Without a month, the supplied balance is treated as the known
            # balance at ``as_of``; earlier/current events must not be added
            # again. Future planning remains visible below.
            return idx > current_idx
        return True

    confirmed_by_month: dict[str, float] = {}
    confirmed_events = []
    for event in events:
        event_idx = month_id(event["month"])
        if event_idx is None or event_idx > current_idx or event_idx > target_idx:
            continue
        if not included_in_snapshot(event["month"]) or not event["confirmed"]:
            continue
        key = event["month_key"]
        confirmed_by_month[key] = confirmed_by_month.get(key, 0.0) + _number(event["net_goal"])
        confirmed_events.append(event)
    confirmed = sum(confirmed_by_month.values())
    known_balance = (opening_amount or 0.0) + confirmed

    # Future plan uses category-specific projection detail where available. If a
    # projection does not carry that detail, derive it from scheduled cash items.
    future_by_month: dict[str, float] = {}
    explicit_forecast_months: set[str] = set()
    for row in rows:
        month = month_start(row.get("month"))
        idx = month_id(month) if month else None
        if month is None or idx is None or idx <= current_idx or idx > target_idx:
            continue
        value = _forecast_category_value(row, category)
        if value is not None:
            key = month_key(month)
            future_by_month[key] = _number(value)
            explicit_forecast_months.add(key)
    for event in events:
        idx = month_id(event["month"])
        if idx is None or idx <= current_idx or idx > target_idx or not included_in_snapshot(event["month"]):
            continue
        key = event["month_key"]
        future_by_month.setdefault(key, 0.0)
        # An explicit category value from forecast_rows wins over this fallback.
        if key not in explicit_forecast_months:
            future_by_month[key] += _number(event["net_goal"])
    for event in events:
        if (month_id(event["month"]) == current_idx and current_idx <= target_idx
                and included_in_snapshot(event["month"]) and not event["confirmed"]):
            key = event["month_key"]
            future_by_month[key] = future_by_month.get(key, 0.0) + _number(event["net_goal"])
    planned_contribution = sum(future_by_month.values())
    planned_end = known_balance + planned_contribution
    remaining = max(0.0, meta["target_amount"] - known_balance)
    months_left = max(0, target_idx - current_idx)
    required_monthly = (remaining / months_left) if months_left and remaining > 0 else remaining

    surpluses = []
    for row in rows:
        month = month_start(row.get("month"))
        idx = month_id(month) if month else None
        if idx is None or idx <= current_idx or idx > target_idx:
            continue
        for field in ("free_after_obligatory_and_savings", "surplus", "free_after"):
            if row.get(field) is not None:
                surpluses.append(_number(row.get(field)))
                break
    surplus = min(surpluses) if surpluses else None
    contribution_fits = None if surplus is None else required_monthly <= max(0.0, surplus) + 0.005
    on_track = planned_end + 0.005 >= meta["target_amount"]
    if not rows and remaining > 0:
        on_track = False
    planned_status = "No prazo" if on_track else "Requer revisão"
    return {
        **meta,
        "as_of": current,
        "confirmed": _rounded(confirmed),
        "confirmed_events": confirmed_events,
        "confirmed_by_month": {key: _rounded(value) for key, value in confirmed_by_month.items()},
        "known_balance": _rounded(known_balance),
        "remaining": _rounded(remaining),
        "months_left": months_left,
        "months_available": months_left,
        "required_monthly": _rounded(required_monthly),
        "planned_contribution": _rounded(planned_contribution),
        "planned_end": _rounded(planned_end),
        "planned_balance": _rounded(planned_end),
        "future_by_month": {key: _rounded(value) for key, value in future_by_month.items()},
        "surplus": None if surplus is None else _rounded(surplus),
        "contribution_fits_surplus": None if surplus is None else _rounded(surplus),
        "contribution_fits": contribution_fits,
        "on_track": on_track,
        "status": planned_status,
        "caution": "Saldo anterior informado é separado dos eventos confirmados; contribuições não são criadas automaticamente.",
    }


__all__ = ["card_calendar", "installment_number_for_month", "simulate_family", "goal_progress"]

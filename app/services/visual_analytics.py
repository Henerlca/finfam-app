"""Dados para visuais financeiros; não altera projeções nem estados do motor."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable

from app.services.finance_core import FAMILY, month_id, month_start, normalize_transaction

CENT = Decimal("0.01")
SALARY_TERMS = ("salario", "salário", "folha de pagamento", "holerite")


def _money(value: Any) -> float:
    try:
        return float(Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP))
    except Exception:
        return 0.0


def _is_salary(row: dict[str, Any]) -> bool:
    text = f"{row.get('category', '')} {row.get('description', '')}".casefold()
    return any(term in text for term in SALARY_TERMS)


def _active_for_month(transactions: Iterable[dict[str, Any]], month: Any, person: str) -> list[dict[str, Any]]:
    selected = month_start(month)
    idx = month_id(selected) if selected else None
    if idx is None:
        return []
    rows = []
    for source in transactions:
        if source.get("archived", False):
            continue
        row = normalize_transaction(source)
        if row.get("start_id") is None or not (row["start_id"] <= idx <= int(row.get("end_id") or 999999999)):
            continue
        if person != FAMILY and row.get("member") != person:
            continue
        rows.append(row)
    return rows


def _status_maps(cash_items: Iterable[dict[str, Any]]) -> tuple[set[int], set[str], dict[str, float]]:
    paid_tx: set[int] = set()
    paid_cards: set[str] = set()
    paid_by_type: dict[str, float] = {}
    for item in cash_items:
        if not item.get("paid"):
            continue
        kind = str(item.get("type") or "")
        paid_by_type[kind] = paid_by_type.get(kind, 0.0) + float(item.get("signed_amount") or 0)
        tx_id = item.get("transaction_id")
        if tx_id is not None:
            paid_tx.add(int(tx_id))
        key = str(item.get("item_key") or "")
        marker = ":invoice:"
        if marker in key:
            paid_cards.add(key.split(marker, 1)[1])
    return paid_tx, paid_cards, paid_by_type


def waterfall_steps(
    monthly: dict[str, Any],
    transactions: Iterable[dict[str, Any]],
    cash_items: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return opening + signed movements + closing, matching ending_balance to cents."""
    person = str(monthly.get("person") or FAMILY)
    active = _active_for_month(transactions, monthly.get("month"), person)
    item_rows = list(cash_items)
    paid_tx, paid_cards, _ = _status_maps(item_rows)
    by_id = {int(row.get("id") or 0): row for row in active}
    salary = sum(
        float(row["amount"]) for row in active
        if row["type"] == "Receita" and _is_salary(row)
    )
    salary = min(float(monthly.get("income") or 0), salary)
    other_income = max(0.0, float(monthly.get("income") or 0) - salary)

    def confirmed_transactions(predicate) -> float:
        return sum(
            float(item.get("signed_amount") or 0)
            for item in item_rows
            if item.get("paid") and item.get("transaction_id") is not None
            and predicate(by_id.get(int(item["transaction_id"]), {}))
        )

    confirmed_income_salary = confirmed_transactions(lambda row: row.get("type") == "Receita" and _is_salary(row))
    confirmed_income_other = confirmed_transactions(lambda row: row.get("type") == "Receita" and not _is_salary(row))
    confirmed_transfer_in = sum(float(item.get("signed_amount") or 0) for item in item_rows if item.get("paid") and item.get("type") == "Transf. Receb.")
    confirmed_transfer_out = sum(float(item.get("signed_amount") or 0) for item in item_rows if item.get("paid") and item.get("type") == "Transf. Env.")
    confirmed_card = -sum(float(row["amount"]) for row in active if row["card_flag"] == 1 and row["type"] != "Receita" and row["account_card"] in paid_cards)
    confirmed_savings = sum(
        float(item.get("signed_amount") or 0) for item in item_rows
        if item.get("paid") and item.get("type") in {"Caixinha", "Saque Caixinha"}
    )

    direct_by_category: dict[str, float] = {}
    confirmed_by_category: dict[str, float] = {}
    for row in active:
        if row.get("card_flag") or row.get("type") not in {"Fixo", "Débito/Pix"}:
            continue
        category = str(row.get("category") or "Sem categoria").strip() or "Sem categoria"
        direct_by_category[category] = direct_by_category.get(category, 0.0) + float(row.get("amount") or 0)
        if int(row.get("id") or 0) in paid_tx:
            confirmed_by_category[category] = confirmed_by_category.get(category, 0.0) + float(row.get("amount") or 0)

    definitions = [
        ("Salário", salary, "income", confirmed_income_salary),
        ("Outras receitas", other_income, "income", confirmed_income_other),
        ("Transferências recebidas", float(monthly.get("transfer_in") or 0), "transfer", confirmed_transfer_in),
        ("Transferências enviadas", -float(monthly.get("transfer_out") or 0), "transfer", confirmed_transfer_out),
        ("Fatura de cartão", -float(monthly.get("card_total") or 0), "expense", confirmed_card),
    ]
    steps: list[dict[str, Any]] = [{
        "label": "Saldo do mês anterior", "value": _money(monthly.get("starting_balance")),
        "kind": "total", "confirmed_value": 0.0, "status": "Saldo de referência",
    }]
    for label, amount, kind, confirmed in definitions:
        amount = _money(amount)
        confirmed = _money(confirmed)
        if amount >= 0:
            confirmed = max(0.0, min(amount, confirmed))
        else:
            confirmed = min(0.0, max(amount, confirmed))
        remainder = abs(amount) - abs(confirmed)
        status = "Confirmado" if abs(amount) < 0.005 or remainder < 0.005 else ("Parcial" if abs(confirmed) >= 0.005 else "Previsto")
        steps.append({"label": label, "value": amount, "kind": kind, "confirmed_value": confirmed, "status": status})
    for category, amount in sorted(direct_by_category.items(), key=lambda pair: (-pair[1], pair[0].casefold())):
        expense = -_money(amount)
        confirmed = -_money(confirmed_by_category.get(category, 0.0))
        status = "Confirmado" if abs(expense - confirmed) < 0.005 else ("Parcial" if abs(confirmed) >= 0.005 else "Previsto")
        steps.append({"label": category, "value": expense, "kind": "expense_category", "confirmed_value": confirmed, "status": status})

    direct_total = float(monthly.get("fixed") or 0) + float(monthly.get("debit_no_pot") or 0) + float(monthly.get("debit_with_pot") or 0)
    direct_difference = _money(direct_total - sum(direct_by_category.values()))
    if abs(direct_difference) >= 0.005:
        expense = -direct_difference
        steps.append({
            "label": "Outras despesas", "value": expense, "kind": "expense_category",
            "confirmed_value": 0.0, "status": "Previsto",
        })
    definitions_after_categories = [
        ("Reserva útil prevista", -float(monthly.get("reserve_need") or 0), "reserve", 0.0),
        ("Caixinhas líquidas", -float(monthly.get("savings_net") or 0), "savings", confirmed_savings),
    ]
    for label, amount, kind, confirmed in definitions_after_categories:
        amount, confirmed = _money(amount), _money(confirmed)
        if amount >= 0:
            confirmed = max(0.0, min(amount, confirmed))
        else:
            confirmed = min(0.0, max(amount, confirmed))
        remainder = abs(amount) - abs(confirmed)
        status = "Confirmado" if abs(amount) < 0.005 or remainder < 0.005 else ("Parcial" if abs(confirmed) >= 0.005 else "Previsto")
        steps.append({"label": label, "value": amount, "kind": kind, "confirmed_value": confirmed, "status": status})
    steps.append({
        "label": "Saldo final previsto", "value": _money(monthly.get("ending_balance")),
        "kind": "total", "confirmed_value": 0.0, "status": "Projeção do mês",
    })
    return [step for step in steps if step["kind"] == "total" or abs(float(step["value"])) >= 0.005]


def category_comparison(
    transactions: Iterable[dict[str, Any]],
    month: Any,
    person: str,
    cash_items: Iterable[dict[str, Any]],
    previous_cash_items: Iterable[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Group planned outflows by category and identify only explicitly paid items."""
    selected = month_start(month)
    if selected is None:
        return []
    current_items = list(cash_items)
    paid_tx, paid_cards, _ = _status_maps(current_items)
    previous_paid_tx, previous_paid_cards, _ = _status_maps(previous_cash_items)
    current = _active_for_month(transactions, selected, person)
    previous_month = selected.replace(day=1) - timedelta(days=1)
    previous = _active_for_month(transactions, previous_month, person) if previous_month else []
    totals: dict[str, dict[str, float]] = {}

    def is_spend(row: dict[str, Any]) -> bool:
        is_card = bool(row.get("card_flag"))
        return (is_card and row.get("type") != "Receita") or (not is_card and row.get("type") in {"Fixo", "Débito/Pix"})

    def add_current(rows: list[dict[str, Any]]) -> None:
        for row in rows:
            is_card = bool(row.get("card_flag"))
            if not is_spend(row):
                continue
            category = str(row.get("category") or "Sem categoria").strip() or "Sem categoria"
            bucket = totals.setdefault(category, {"planned": 0.0, "confirmed": 0.0, "previous": 0.0})
            amount = float(row.get("amount") or 0)
            bucket["planned"] += amount
            if (is_card and row.get("account_card") in paid_cards) or (not is_card and int(row.get("id") or 0) in paid_tx):
                bucket["confirmed"] += amount

    def add_previous_actual(rows: list[dict[str, Any]]) -> None:
        for row in rows:
            if not is_spend(row):
                continue
            is_card = bool(row.get("card_flag"))
            paid = row.get("account_card") in previous_paid_cards if is_card else int(row.get("id") or 0) in previous_paid_tx
            if not paid:
                continue
            category = str(row.get("category") or "Sem categoria").strip() or "Sem categoria"
            bucket = totals.setdefault(category, {"planned": 0.0, "confirmed": 0.0, "previous": 0.0})
            bucket["previous"] += float(row.get("amount") or 0)

    add_current(current)
    add_previous_actual(previous)
    result = []
    for category, values in totals.items():
        planned = _money(values["planned"])
        confirmed = _money(values["confirmed"])
        previous_value = _money(values["previous"])
        delta = _money(confirmed - previous_value)
        result.append({
            "category": category, "planned": planned, "confirmed": confirmed,
            "previous": previous_value, "change": delta,
            "change_percent": (delta / previous_value * 100) if previous_value else None,
        })
    return sorted(result, key=lambda row: (-row["planned"], row["category"].casefold()))

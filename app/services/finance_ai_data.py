from __future__ import annotations

from collections import defaultdict
from datetime import date
import re
from typing import Any

from sqlalchemy.orm import Session

from app.auth import family_password
from app.repository import cashflow_statuses, legacy_statuses, read_snapshot
from app.services.assistant_agent import redact_known_identifiers
from app.services.charts import classify_pot
from app.services.horizon import effective_end
from app.services.finance_core import (
    FAMILY, MEMBERS, apply_payment_statuses, cashflow_items, confirmed_pot_usage, card_limits,
    month_from_id, month_id, month_key, month_start, normalize_transaction,
    pot_monthly_status, projection,
)


EXPENSE_ITEM_TYPES = {"Fixo", "Débito/Pix", "Fatura cartão"}
CONFIRMED_PURCHASE_TYPES = {"Fixo", "Débito/Pix", "Crédito à vista", "Parcelado"}
MAX_CATALOG_CHARS = 1_000_000
MAX_DESCRIPTION_CHARS = 120
MAX_LABEL_CHARS = 96


def _money(value: Any) -> float:
    try:
        amount = float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return round(amount, 2)


def _reserve_group(name: str) -> str:
    normalized = str(name or "").casefold()
    if any(word in normalized for word in ("gasolina", "combustível", "combustivel")):
        return "Gasolina"
    if any(word in normalized for word in ("lazer", "viagem", "presentes", "hobby", "entretenimento")):
        return "Lazer e escolhas"
    if any(word in normalized for word in ("casa", "moradia", "mercado", "aliment", "saúde", "saude", "escola", "energia", "aluguel")):
        return "Essenciais"
    return classify_pot(normalized)


def _grouped_reserve_rows(rows: list[dict[str, Any]], amount_field: str) -> dict[str, float]:
    grouped: defaultdict[str, float] = defaultdict(float)
    for row in rows:
        grouped[_reserve_group(str(row.get("pot", "")))] += float(row.get(amount_field, 0) or 0)
    return {name: _money(amount) for name, amount in sorted(grouped.items())}


def _active_for_month(transaction: dict[str, Any], idx: int) -> bool:
    start = transaction.get("start_id")
    end = transaction.get("end_id")
    return start is not None and start <= idx and (end is None or end >= idx)


def _safe_label(value: Any, protected: list[str], limit: int = MAX_LABEL_CHARS) -> str:
    text = " ".join(str(value or "").replace("\x00", "").split()).strip()
    if not text:
        return ""
    text = redact_known_identifiers(text, protected)
    # Do not let a pasted token, credential or long identifier reach the model.
    text = re.sub(r"(?i)(?:bearer\s+|token\s*[:=]\s*)\S+", "[dado privado]", text)
    text = re.sub(r"[A-Za-z0-9_-]{32,}", "[dado privado]", text)
    text = re.sub(r"\b\d{8,}\b", "[dado privado]", text)
    return text[:limit].strip()


def _safe_description(value: Any, protected: list[str]) -> str:
    text = _safe_label(value, protected, MAX_DESCRIPTION_CHARS)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[dado privado]", text)
    text = re.sub(r"\b\d{3}[. -]?\d{3}[. -]?\d{3}[- ]?\d{2}\b", "[dado privado]", text)
    return text[:MAX_DESCRIPTION_CHARS].strip()


def _month_list(start: Any, end: Any) -> list[str]:
    first, last = month_start(start), month_start(end)
    if first is None or last is None or last < first:
        return []
    start_id, end_id = month_id(first), month_id(last)
    if start_id is None or end_id is None:
        return []
    return [month_key(month_from_id(index)) for index in range(start_id, end_id + 1)]


def _pseudonyms(values: list[str], prefix: str) -> dict[str, str]:
    unique = sorted({str(value).strip() for value in values if str(value).strip()}, key=str.casefold)
    return {value: f"{prefix}:{index}" for index, value in enumerate(unique, 1)}


def _catalog_statuses(
    txs: list[dict[str, Any]],
    accounts: list[dict[str, Any]],
    cards: list[dict[str, Any]],
    db: Session,
    start: date,
    end: date,
) -> dict[int, set[str]]:
    """Collect payment states locally; card purchases inherit their invoice state."""
    states: dict[int, set[str]] = defaultdict(set)
    first_id, last_id = month_id(start), month_id(end)
    if first_id is None or last_id is None or last_id < first_id:
        return states
    for index in range(first_id, last_id + 1):
        month = month_from_id(index)
        items = apply_payment_statuses(
            cashflow_items(txs, cards, accounts, month, FAMILY),
            legacy_statuses(db, month), cashflow_statuses(db, month),
        )
        paid_cards = {
            str(item.get("item_key", "")).split(":invoice:", 1)[1]
            for item in items
            if item.get("paid") and item.get("type") == "Fatura cartão" and ":invoice:" in str(item.get("item_key", ""))
        }
        for item in items:
            tx_id = item.get("transaction_id")
            if tx_id is not None:
                states[int(tx_id)].add("confirmed" if item.get("paid") else "pending")
        for raw in txs:
            tx = normalize_transaction(raw)
            if tx["archived"] or not _active_for_month(tx, index):
                continue
            if tx["card_flag"]:
                states[tx["id"]].add("confirmed" if tx["account_card"] in paid_cards else "pending")
    return states


def _monthly_row(
    month: date,
    items: list[dict[str, Any]],
    txs: list[dict[str, Any]],
    cards: list[dict[str, Any]],
    settings: dict[str, Any],
    closed: dict[str, bool],
    db: Session,
) -> dict[str, Any]:
    relevant = [item for item in items if item.get("type") in EXPENSE_ITEM_TYPES or item.get("type") in ("Receita", "Caixinha", "Saque Caixinha")]
    paid = [item for item in relevant if item.get("paid")]
    pending = [item for item in relevant if not item.get("paid")]
    expenses = [item for item in relevant if item.get("type") in EXPENSE_ITEM_TYPES]
    incomes = [item for item in relevant if item.get("type") == "Receita"]
    savings = [item for item in relevant if item.get("type") in ("Caixinha", "Saque Caixinha")]
    expense_total = sum(float(item.get("amount", 0) or 0) for item in expenses)
    confirmed_expense = sum(float(item.get("amount", 0) or 0) for item in expenses if item.get("paid"))
    pending_expense = expense_total - confirmed_expense
    income_total = sum(float(item.get("amount", 0) or 0) for item in incomes)
    confirmed_income = sum(float(item.get("amount", 0) or 0) for item in incomes if item.get("paid"))
    deposits = sum(float(item.get("amount", 0) or 0) for item in savings if item.get("type") == "Caixinha")
    withdrawals = sum(float(item.get("amount", 0) or 0) for item in savings if item.get("type") == "Saque Caixinha")
    paid_cards = {
        str(item.get("item_key", "")).split(":invoice:", 1)[1]
        for item in items
        if item.get("paid") and item.get("type") == "Fatura cartão" and ":invoice:" in str(item.get("item_key", ""))
    }
    idx = month_id(month)
    category_amounts: defaultdict[str, float] = defaultdict(float)
    category_counts: defaultdict[str, int] = defaultdict(int)
    paid_ids = {int(item["transaction_id"]) for item in items if item.get("paid") and item.get("transaction_id") is not None}
    if idx is not None:
        for raw in txs:
            tx = normalize_transaction(raw)
            if tx["archived"] or tx["type"] not in CONFIRMED_PURCHASE_TYPES or not _active_for_month(tx, idx):
                continue
            if tx["card_flag"]:
                if tx["account_card"] not in paid_cards:
                    continue
            elif tx["id"] not in paid_ids:
                continue
            category = _reserve_group(str(tx.get("category") or "Sem categoria"))
            category_amounts[category] += float(tx.get("amount", 0) or 0)
            category_counts[category] += 1
    pot_rows = pot_monthly_status(txs, month, FAMILY, settings.get("pot_order") or []).get("rows", [])
    usage = confirmed_pot_usage(txs, month, items, [row["pot"] for row in pot_rows])
    fully_settled = bool(relevant) and not pending and bool(closed.get(month_key(month), False))
    return {
        "month": month_key(month),
        "closed": bool(closed.get(month_key(month), False)),
        "fully_settled": fully_settled,
        "planned": {"income": _money(income_total), "expenses": _money(expense_total), "net": _money(income_total - expense_total)},
        "confirmed": {"income": _money(confirmed_income), "expenses": _money(confirmed_expense), "net": _money(confirmed_income - confirmed_expense)},
        "pending": {"income": _money(income_total - confirmed_income), "expenses": _money(pending_expense), "items": len(pending)},
        "confirmed_item_count": len(paid),
        "pending_item_count": len(pending),
        "planned_reserve_contributions_by_group": _grouped_reserve_rows(pot_rows, "reserved"),
        "confirmed_reserve_use_by_group": _grouped_reserve_rows([{"pot": name, "amount": value.get("amount", 0)} for name, value in usage.items()], "amount"),
        "cashbox": {"planned_deposits": _money(deposits), "planned_withdrawals": _money(withdrawals), "planned_net": _money(deposits - withdrawals)},
        "expense_categories": [
            {"category": name, "amount": _money(amount), "records": category_counts[name]}
            for name, amount in sorted(category_amounts.items(), key=lambda item: item[1], reverse=True)[:12]
        ],
    }


def _build_catalog(
    txs: list[dict[str, Any]],
    accounts: list[dict[str, Any]],
    cards: list[dict[str, Any]],
    lookups: dict[str, list[str]],
    settings: dict[str, Any],
    status_map: dict[int, set[str]],
    current: date,
    end: date,
    protected: list[str],
    include_descriptions: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    account_names = [str(row.get("name", "")) for row in accounts]
    card_names = [str(row.get("name", "")) for row in cards]
    member_names = [*MEMBERS, *(str(value) for value in lookups.get("membro", [])), FAMILY]
    pot_names = [str(tx.get("linked_pot") or "") for tx in txs]
    pot_names += [str(tx.get("description") or "") for tx in txs if tx.get("type") == "Reserva"]
    account_refs = _pseudonyms(account_names, "account")
    card_refs = _pseudonyms(card_names, "card")
    member_refs = _pseudonyms(member_names, "member")
    pot_refs = _pseudonyms([name for name in pot_names if name], "pot")
    categories = sorted({str(tx.get("category") or "") for tx in txs if str(tx.get("category") or "").strip()}, key=str.casefold)
    category_refs = _pseudonyms(categories, "category")
    catalog: list[dict[str, Any]] = []
    for raw in sorted(txs, key=lambda row: (str(row.get("transaction_date") or ""), int(row.get("id") or 0))):
        tx = normalize_transaction(raw)
        if tx["archived"]:
            continue
        configured_base = month_start(settings.get("base_month")) or current
        first_month = max(tx.get("start_month") or current, configured_base)
        last_month = min(tx.get("end_month") or end, end)
        first_idx, last_idx = month_id(first_month), month_id(last_month)
        count_months = max(0, last_idx - first_idx + 1) if first_idx is not None and last_idx is not None else 0
        states = status_map.get(tx["id"], set())
        if "confirmed" in states and "pending" in states:
            status = "partial"
        elif "confirmed" in states:
            status = "confirmed"
        elif "pending" in states:
            status = "pending"
        else:
            status = "planned"
        account_name = str(tx.get("account_card") or "")
        pot_name = str(tx.get("linked_pot") or "") or (str(tx.get("description") or "") if tx.get("type") == "Reserva" else "")
        exact_date = tx.get("transaction_date")
        item: dict[str, Any] = {
            "ref": f"tx:{tx['id']}",
            "member_ref": member_refs.get(str(tx.get("member") or FAMILY), member_refs.get(FAMILY, "member:1")),
            "category_ref": category_refs.get(str(tx.get("category") or "")) if str(tx.get("category") or "") else None,
            "category": _safe_label(tx.get("category"), protected) or "Sem categoria",
            "type": _safe_label(tx.get("type"), protected, 64),
            "transaction_date": exact_date.isoformat() if isinstance(exact_date, date) else (str(exact_date) if exact_date else None),
            "start_month": month_key(first_month),
            "end_month": month_key(last_month) if tx.get("end_month") else None,
            "month_count": count_months,
            "amount": _money(tx.get("amount")),
            "total_amount": _money(tx.get("total_amount")),
            "count": int(tx.get("installment_count") or 1),
            "status": status,
            "account_ref": account_refs.get(account_name) if account_name in account_refs else None,
            "card_ref": card_refs.get(account_name) if account_name in card_refs else None,
            "member": member_refs.get(str(tx.get("member") or FAMILY), "member:1"),
            "pot_ref": pot_refs.get(pot_name) if pot_name else None,
            "destination_ref": account_refs.get(str(tx.get("destination_account") or "")) if tx.get("destination_account") else None,
        }
        if include_descriptions:
            item["description"] = _safe_description(tx.get("description"), protected)
        catalog.append(item)
    refs = {"accounts": account_refs, "cards": card_refs, "members": member_refs, "pots": pot_refs, "categories": category_refs}
    # Never silently omit records: if a future data set exceeds the transport budget, retain
    # deterministic coverage metadata and a compact item for every record.
    import json
    serialized = json.dumps(catalog, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    coverage = {"total": len(catalog), "included": len(catalog), "truncated": False, "from": None, "to": None}
    if len(serialized) > MAX_CATALOG_CHARS:
        compact = []
        for item in catalog:
            compact.append({key: item.get(key) for key in ("ref", "member_ref", "category", "description", "type", "transaction_date", "start_month", "end_month", "month_count", "amount", "total_amount", "count", "status", "account_ref", "card_ref", "pot_ref", "destination_ref")})
        catalog = compact
        serialized = json.dumps(catalog, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        coverage["compacted"] = True
    if len(serialized) > MAX_CATALOG_CHARS:
        # A hard bound is preferable to an implicit transport failure. Keep a prefix only
        # with explicit counts/range, never claim that it is complete.
        total_before = len(catalog)
        kept: list[dict[str, Any]] = []
        size = 2
        for item in catalog:
            encoded = len(json.dumps(item, ensure_ascii=False, separators=(",", ":"))) + 1
            if size + encoded > MAX_CATALOG_CHARS:
                break
            kept.append(item)
            size += encoded
        catalog = kept
        coverage.update({"included": len(kept), "truncated": True, "omitted": total_before - len(kept)})
        if kept:
            coverage["from"] = kept[0]["ref"]
            coverage["to"] = kept[-1]["ref"]
    return catalog, {"coverage": coverage, "reference_maps": refs}


def build_financial_summary(db: Session, requested_months: int = 6, *, include_descriptions: bool = False) -> dict[str, Any]:
    """Return a privacy-safe family snapshot with legacy aggregates plus complete planning data."""
    months_requested = requested_months if requested_months in (3, 6, 12) else 6
    txs, accounts, cards, lookups, settings, closed = read_snapshot(db)
    protected_identifiers = [
        *MEMBERS,
        family_password(),
        *(str(row.get("name", "")) for row in accounts),
        *(str(row.get("name", "")) for row in cards),
        *(str(value) for value in lookups.get("membro", [])),
    ]
    today_month = date.today().replace(day=1)
    current_idx = month_id(today_month)
    base_idx = month_id(settings.get("base_month", "2026-11-01"))
    assert current_idx is not None
    base_month = month_from_id(base_idx) if base_idx is not None else today_month
    history_start = min(base_month, today_month)
    history_end = today_month
    sample_start = max(base_month, month_from_id(current_idx - months_requested + 1))
    sample: list[tuple[date, list[dict[str, Any]], list[dict[str, Any]], bool]] = []
    monthly_summary: list[dict[str, Any]] = []
    confirmed_deposits = confirmed_withdrawals = 0.0
    paid_purchase_count = confirmed_item_count = unconfirmed_item_count = fully_settled_month_count = 0
    history: list[dict[str, Any]] = []
    history_idx_start, history_idx_end = month_id(history_start), month_id(history_end)
    if history_idx_start is not None and history_idx_end is not None:
        for index in range(history_idx_start, history_idx_end + 1):
            month = month_from_id(index)
            items = apply_payment_statuses(cashflow_items(txs, cards, accounts, month, FAMILY), legacy_statuses(db, month), cashflow_statuses(db, month))
            row = _monthly_row(month, items, txs, cards, settings, closed, db)
            history.append(row)
            if month >= sample_start:
                relevant = [item for item in items if item.get("type") in EXPENSE_ITEM_TYPES or item.get("type") == "Receita"]
                sample.append((month, items, relevant, bool(closed.get(month_key(month), False))))
    for month, items, relevant, month_closed in sample:
        paid_relevant = [item for item in relevant if item.get("paid")]
        unpaid_relevant = [item for item in relevant if not item.get("paid")]
        month_fully_settled = bool(relevant) and not unpaid_relevant and month_closed
        confirmed_item_count += len(paid_relevant)
        unconfirmed_item_count += len(unpaid_relevant)
        fully_settled_month_count += int(month_fully_settled)
        income = sum(float(item.get("amount", 0) or 0) for item in items if item.get("type") == "Receita" and item.get("paid"))
        expenses = sum(float(item.get("amount", 0) or 0) for item in items if item.get("type") in EXPENSE_ITEM_TYPES and item.get("paid"))
        deposits = sum(float(item.get("amount", 0) or 0) for item in items if item.get("type") == "Caixinha" and item.get("paid"))
        withdrawals = sum(float(item.get("amount", 0) or 0) for item in items if item.get("type") == "Saque Caixinha" and item.get("paid"))
        confirmed_deposits += deposits
        confirmed_withdrawals += withdrawals
        pot_rows = pot_monthly_status(txs, month, FAMILY, settings.get("pot_order") or []).get("rows", [])
        paid_ids = {int(item["transaction_id"]) for item in items if item.get("paid") and item.get("transaction_id") is not None}
        paid_cards = {str(item.get("item_key", "")).split(":invoice:", 1)[1] for item in items if item.get("paid") and item.get("type") == "Fatura cartão" and ":invoice:" in str(item.get("item_key", ""))}
        idx = month_id(month)
        cats: defaultdict[str, float] = defaultdict(float)
        counts: defaultdict[str, int] = defaultdict(int)
        if idx is not None:
            for raw in txs:
                tx = normalize_transaction(raw)
                if tx["archived"] or tx["type"] not in CONFIRMED_PURCHASE_TYPES or not _active_for_month(tx, idx):
                    continue
                if tx["card_flag"] and tx["account_card"] not in paid_cards:
                    continue
                if not tx["card_flag"] and tx["id"] not in paid_ids:
                    continue
                category = _reserve_group(str(tx.get("category") or "Sem categoria"))
                cats[category] += float(tx.get("amount", 0) or 0)
                counts[category] += 1
                paid_purchase_count += 1
        monthly_summary.append({
            "month": month_key(month), "month_closed": month_closed, "month_fully_settled": month_fully_settled,
            "confirmed_item_count": len(paid_relevant), "unconfirmed_item_count": len(unpaid_relevant),
            "confirmed_income": _money(income), "confirmed_external_expenses": _money(expenses),
            "remaining_after_external_expenses": _money(income - expenses),
            "confirmed_cashbox_deposits": _money(deposits), "confirmed_cashbox_withdrawals": _money(withdrawals),
            "confirmed_cashbox_net": _money(deposits - withdrawals),
            "planned_reserve_contributions_by_group": _grouped_reserve_rows(pot_rows, "reserved"),
            "confirmed_reserve_use_by_group": _grouped_reserve_rows([{"pot": name, "amount": value.get("amount", 0)} for name, value in confirmed_pot_usage(txs, month, items, [row["pot"] for row in pot_rows]).items()], "amount"),
            "expense_categories": [{"category": name, "amount": _money(amount), "records": counts[name]} for name, amount in sorted(cats.items(), key=lambda item: item[1], reverse=True)[:12]],
        })

    forecast_rows = projection(txs, accounts, settings.get("base_month", "2026-11-01"), effective_end(txs, settings), FAMILY, closed_months=closed, minimum_balance=float(settings.get("minimum_balance") or 0.0))
    forecast = [{
        "month": month_key(row["month"]), "status": "fechado" if row.get("closed") else "previsto",
        "income": _money(row.get("income")), "card_bills": _money(row.get("card_total")),
        "fixed_expenses": _money(row.get("fixed")), "debit_pix_expenses": _money(float(row.get("debit_no_pot", 0) or 0) + float(row.get("debit_with_pot", 0) or 0)),
        "budget_result_with_prior_commitments": _money(row.get("monthly_balance_with_prior_impact")),
        "budget_result_without_prior_commitments": _money(row.get("monthly_balance_without_prior_impact")),
        "projected_bank_balance": _money(row.get("projected_ending_balance")), "distance_to_safety_floor": _money(row.get("gap_to_minimum")),
        "cashbox_accumulation_planned": _money(row.get("savings_accumulated")), "planned_reserves": _money(row.get("planned_reserve")), "cashbox_monthly_net": _money(row.get("savings_net")),
    } for row in forecast_rows if row["month"] >= today_month]
    full_future_plan = {"from": forecast[0]["month"] if forecast else None, "to": forecast[-1]["month"] if forecast else None,
        "columns": ["month", "income", "card_bills", "fixed_expenses", "debit_pix_expenses", "planned_reserves", "cashbox_monthly_net", "cashbox_accumulation_planned", "budget_result_with_prior_commitments", "budget_result_without_prior_commitments", "projected_bank_balance", "distance_to_safety_floor"],
        "rows": []}
    full_future_plan["rows"] = [[row[column] for column in full_future_plan["columns"]] for row in forecast]
    visible_forecast = forecast[:12]
    reserve_plan_next_12 = [{"month": row["month"], "contributions_by_group": _grouped_reserve_rows(pot_monthly_status(txs, row["month"], FAMILY, settings.get("pot_order") or []).get("rows", []), "reserved")} for row in visible_forecast]
    end = effective_end(txs, settings)
    # Estados já confirmados são apurados até hoje. Eventos futuros continuam
    # planejados, sem repetir consultas de status para cada competência futura.
    status_map = _catalog_statuses(txs, accounts, cards, db, history_start, today_month)
    catalog, catalog_meta = _build_catalog(txs, accounts, cards, lookups, settings, status_map, today_month, end, protected_identifiers, include_descriptions)
    status_totals: defaultdict[str, int] = defaultdict(int)
    for item in catalog:
        status_totals[str(item.get("status"))] += 1
    configured_goals = settings.get("family_goals")
    goals = configured_goals if isinstance(configured_goals, list) else []
    goals_summary = [{"ref": f"goal:{index}", "name": _safe_label(goal.get("name"), protected_identifiers), "category": _safe_label(goal.get("category"), protected_identifiers), "target_amount": _money(goal.get("target_amount")), "target_month": str(goal.get("target_month") or "")[:7]} for index, goal in enumerate(goals, 1) if isinstance(goal, dict)]
    credit_summary = []
    for credit in card_limits(txs, cards, today_month):
        name = credit["card"]
        bills = [{"month": month_key(row["month"]), "amount": _money(row.get("card_by_name", {}).get(name, 0))}
                 for row in forecast_rows if row["month"] >= today_month]
        credit_summary.append({"card_ref": catalog_meta["reference_maps"]["cards"].get(name),
            "limit": _money(credit["limit"]), "committed": _money(credit["total_committed"]),
            "available": _money(credit["available"]), "planned_bills": _money(sum(row["amount"] for row in bills)),
            "monthly_bills": bills})
    safe_reference_maps = {name: sorted(mapping.values()) for name, mapping in catalog_meta["reference_maps"].items()}
    top_category_amounts: defaultdict[str, float] = defaultdict(float)
    top_category_counts: defaultdict[str, int] = defaultdict(int)
    for monthly in monthly_summary:
        for item in monthly.get("expense_categories", []):
            top_category_amounts[str(item.get("category") or "Sem categoria")] += float(item.get("amount", 0) or 0)
            top_category_counts[str(item.get("category") or "Sem categoria")] += int(item.get("records", 0) or 0)
    top_categories = [{"category": name, "confirmed_amount": _money(amount), "record_count": top_category_counts[name]} for name, amount in sorted(top_category_amounts.items(), key=lambda item: item[1], reverse=True)[:10]]
    return {
        "scope": "Família, visão agregada",
        "confirmed_sample": {"months_requested": months_requested, "months_available": len(sample), "from": month_key(sample[0][0]) if sample else None, "to": month_key(sample[-1][0]) if sample else None, "confirmed_records_only": True, "months_may_be_partial": True, "fully_settled_month_count": fully_settled_month_count, "monthly": monthly_summary, "top_categories": top_categories, "confirmed_cashbox_deposits_total": _money(confirmed_deposits), "confirmed_cashbox_withdrawals_total": _money(confirmed_withdrawals), "confirmed_expense_transaction_count": paid_purchase_count, "confirmed_item_count": confirmed_item_count, "unconfirmed_item_count": unconfirmed_item_count},
        "history": {"from": month_key(history_start), "to": month_key(history_end), "base_to_now": True, "rows": history},
        "planning": {"from": full_future_plan["from"], "to": full_future_plan["to"], "rows": forecast, "complete": True},
        "projection_next_12_months": visible_forecast,
        "planned_reserves_next_12_months": reserve_plan_next_12,
        "full_future_plan": full_future_plan,
        "transaction_catalog": catalog,
        "catalog": catalog,
        "catalog_coverage": catalog_meta["coverage"],
        # Only pseudonyms leave this process. Real names remain local for review mapping.
        "reference_maps": safe_reference_maps,
        "catalog_status_totals": dict(status_totals),
        "goals_summary": goals_summary,
        "credit_summary": credit_summary,
        "limitations": ["Histórico inclui todas as competências desde a base até agora, com planejado, confirmado e pendente separados.", "Fatura de cartão é consolidada uma única vez; compras individuais não são somadas novamente ao total de despesas.", "O catálogo usa referências pseudonimizadas locais e só inclui descrições sanitizadas quando a autorização da consulta permite.", "Valores futuros são projeções do app e não pagamentos confirmados."],
    }

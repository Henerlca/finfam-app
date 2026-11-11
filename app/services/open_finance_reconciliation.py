from __future__ import annotations

import unicodedata
from calendar import monthrange
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable

from app.services.card_billing import (
    DEFAULT_CARD_CLOSING_DAY, closing_day_for_card, validate_closing_day,
)
from app.services.finance_core import FAMILY, as_date, month_id, month_key, month_label, month_start, monthly_details

CENT = Decimal("0.01")


def money(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return None


def credit_used(account: Any) -> Decimal | None:
    """Limit utilization is total credit limit minus available credit, never a bill."""
    total = money(getattr(account, "credit_limit", None))
    available = money(getattr(account, "available_credit_limit", None))
    if total is None or available is None:
        return None
    return (total - available).quantize(CENT, rounding=ROUND_HALF_UP)


def external_card_closing_day(
    account_id: str,
    mappings: dict[str, dict[str, str]],
    local_cards: Iterable[dict[str, Any]],
    settings: dict[str, Any] | None = None,
    provider_closing_date: Any = None,
) -> tuple[int, str]:
    """Use a configured close day only after an explicit valid card mapping.

    An institution-based suggestion is intentionally not enough to choose a
    personalized billing cycle; unlinked cards use the visible day-3 default.
    """
    observed_close = as_date(provider_closing_date)
    if observed_close is not None:
        return observed_close.day, "provider_bill"
    mapping = mappings.get(str(account_id), {})
    mapped_name = mapping.get("name") if mapping.get("kind") == "CREDIT" else None
    cards = list(local_cards)
    card_names = {str(card.get("name") or "") for card in cards}
    if mapped_name and mapped_name in card_names:
        return closing_day_for_card(mapped_name, cards, settings), "configured"
    return DEFAULT_CARD_CLOSING_DAY, "default"


def latest_provider_closing_date(
    bills: Iterable[Any], account_id: str, invoice_month: Any,
) -> date | None:
    """Find the latest BRL statement close before the target invoice cycle."""
    target = month_start(invoice_month)
    if target is None:
        return None
    dates = [
        closing
        for bill in bills
        if str(getattr(bill, "account_id", "")) == str(account_id)
        and str(getattr(bill, "currency_code", "BRL") or "BRL").upper() == "BRL"
        and (closing := as_date(getattr(bill, "closing_date", None))) is not None
        and closing < target
    ]
    return max(dates, default=None)


def clean_mappings(raw: Any) -> dict[str, dict[str, str]]:
    """Accept only explicit account IDs mapped to a compatible internal name."""
    if not isinstance(raw, dict):
        return {}
    output: dict[str, dict[str, str]] = {}
    for account_id, record in raw.items():
        if not isinstance(account_id, str) or not account_id.strip() or not isinstance(record, dict):
            continue
        kind = str(record.get("kind") or "").upper()
        name = str(record.get("name") or "").strip()
        if kind in {"BANK", "CREDIT"} and name:
            output[account_id] = {"kind": kind, "name": name}
    return output


def _normalized_words(value: Any) -> set[str]:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char)).casefold()
    ignored = {"bank", "banco", "cartao", "card", "credito", "credit", "conta", "account", "par2"}
    return {word for word in "".join(char if char.isalnum() else " " for char in text).split() if word not in ignored}


def suggested_internal_name(
    institution: str,
    account_type: str,
    local_accounts: Iterable[dict[str, Any]],
    local_cards: Iterable[dict[str, Any]],
) -> str | None:
    """Return a UI-only hint for a unique institution match; never persist it."""
    source = _normalized_words(institution)
    if not source:
        return None
    if str(account_type).upper() == "BANK":
        candidates = [
            str(row.get("name") or "") for row in local_accounts
            if source & _normalized_words(row.get("name"))
        ]
    elif str(account_type).upper() == "CREDIT":
        candidates = [
            str(row.get("name") or "") for row in local_cards
            if source & (_normalized_words(row.get("bank")) | _normalized_words(row.get("name")))
        ]
    else:
        return None
    unique = sorted(set(name for name in candidates if name), key=str.casefold)
    return unique[0] if len(unique) == 1 else None


def _next_month(month: date) -> date:
    index = month.year * 12 + month.month
    return date(index // 12, index % 12 + 1, 1)


def card_cycle_bounds(invoice_month: Any, closing_day: Any) -> tuple[date, date, int]:
    """Return (first included purchase date, closing date, normalized close day).

    A purchase made on the closing date belongs to that invoice; the next cycle
    begins the following calendar day. Short months clamp the configured day.
    """
    target = month_start(invoice_month)
    if target is None:
        raise ValueError("Informe uma competência válida para projetar a fatura.")
    closing = validate_closing_day(closing_day)
    previous_month_last = target - timedelta(days=1)
    previous_close = previous_month_last.replace(day=min(closing, previous_month_last.day))
    cycle_start = previous_close + timedelta(days=1)
    target_close = target.replace(day=min(closing, monthrange(target.year, target.month)[1]))
    return cycle_start, target_close, closing


def project_card_cycle(
    transactions: Iterable[Any],
    account_id: str,
    invoice_month: Any,
    closing_day: Any,
) -> dict[str, Any]:
    """Project purchases in one card cycle without treating payments as charges.

    DEBIT transactions with POSTED/PENDING status form the estimate, split by
    source status. CREDIT transactions (which may include payments, refunds or
    other adjustments) are reported separately and never subtracted implicitly.
    """
    cycle_start, cycle_end, closing = card_cycle_bounds(invoice_month, closing_day)
    debits = {"POSTED": Decimal("0.00"), "PENDING": Decimal("0.00")}
    debit_counts = {"POSTED": 0, "PENDING": 0}
    credits = {"POSTED": Decimal("0.00"), "PENDING": Decimal("0.00")}
    credit_counts = {"POSTED": 0, "PENDING": 0}
    unclassified_count = 0
    installment_count = 0
    purchase_rows: list[dict[str, Any]] = []

    for row in transactions:
        if str(getattr(row, "account_id", "")) != str(account_id):
            continue
        transaction_date = as_date(getattr(row, "occurred_on", None))
        if transaction_date is None or not cycle_start <= transaction_date <= cycle_end:
            continue
        transaction_type = str(getattr(row, "transaction_type", "")).upper()
        status = str(getattr(row, "status", "")).upper()
        amount = money(getattr(row, "amount", None))
        currency = str(getattr(row, "currency_code", "BRL") or "BRL").upper()
        if currency != "BRL" or amount is None or status not in {"POSTED", "PENDING"} or transaction_type not in {"DEBIT", "CREDIT"}:
            unclassified_count += 1
            continue
        try:
            total_installments = int(getattr(row, "total_installments", None) or 0)
        except (TypeError, ValueError):
            total_installments = 0
        if total_installments > 1:
            installment_count += 1
        if transaction_type == "DEBIT":
            debits[status] += abs(amount)
            debit_counts[status] += 1
            try:
                installment_number = int(getattr(row, "installment_number", None) or 0) or None
            except (TypeError, ValueError):
                installment_number = None
            purchase_rows.append({
                "occurred_on": transaction_date,
                "description": str(getattr(row, "description", "") or "Compra sem descrição"),
                "category": str(getattr(row, "category", "") or ""),
                "amount": abs(amount),
                "status": status,
                "installment_number": installment_number,
                "total_installments": total_installments or None,
            })
        else:
            # Keep the source's direction as a negative adjustment for display,
            # but do not apply it to the invoice estimate without classifying it.
            credits[status] -= abs(amount)
            credit_counts[status] += 1

    debit_count = sum(debit_counts.values())
    credit_count = sum(credit_counts.values())
    projected = debits["POSTED"] + debits["PENDING"] if debit_count else None
    return {
        "account_id": str(account_id),
        "invoice_month": month_start(invoice_month),
        "cycle_start": cycle_start,
        "cycle_end": cycle_end,
        "closing_day": closing,
        "posted_charges": debits["POSTED"],
        "pending_charges": debits["PENDING"],
        "projected_total": projected,
        "posted_count": debit_counts["POSTED"],
        "pending_count": debit_counts["PENDING"],
        "credit_adjustments_posted": credits["POSTED"],
        "credit_adjustments_pending": credits["PENDING"],
        "credit_count": credit_count,
        "credit_count_posted": credit_counts["POSTED"],
        "credit_count_pending": credit_counts["PENDING"],
        "unclassified_count": unclassified_count,
        "installment_count": installment_count,
        "purchase_rows": sorted(
            purchase_rows,
            key=lambda item: (item["occurred_on"], item["description"].casefold(), item["amount"]),
        ),
        "review_required": credit_count > 0 or unclassified_count > 0,
    }


def _period(due_month: date, selected_month: date) -> str:
    due_id = month_id(due_month)
    selected_id = month_id(selected_month)
    if due_id is None or selected_id is None:
        return "unknown"
    if due_id < selected_id:
        return "past"
    if due_id == selected_id:
        return "current"
    return "future"


def _month_text(due_month: date) -> str:
    return month_label(due_month)


def build_invoice_groups(
    credit_accounts: Iterable[Any],
    bills: Iterable[Any],
    mappings: dict[str, dict[str, str]],
    local_cards: list[dict[str, Any]],
    transactions: list[dict[str, Any]],
    closed: dict[str, bool],
    base_month: Any,
    selected_month: Any,
    projected_cycles: dict[tuple[str, str], dict[str, Any]] | None = None,
    projection_targets: dict[str, str | None] | None = None,
    source_updated_by_account: dict[str, str | None] | None = None,
) -> list[dict[str, Any]]:
    """Group closed bills and open transaction-cycle projections by due month.

    Actual provider bills take precedence. If none exists for a month, a
    transaction-based projection may be shown. FinFam comparisons still require
    an explicit account/card mapping. Credit limits and balances are never used
    as invoice amounts.
    """
    selected = month_start(selected_month) or date.today().replace(day=1)
    following = _next_month(selected)
    base = month_start(base_month)
    accounts = list(credit_accounts)
    cycles = projected_cycles or {}
    targets = projection_targets or {}
    source_dates = source_updated_by_account or {}
    account_by_id = {str(row.account_id): row for row in accounts}
    bill_totals: dict[tuple[str, str, str], dict[str, Any]] = {}
    all_months: dict[str, set[str]] = defaultdict(set)
    unknown: dict[tuple[str, str], dict[str, Any]] = {}

    for bill in bills:
        account_id = str(getattr(bill, "account_id", "") or "")
        if account_id not in account_by_id:
            continue
        currency = str(getattr(bill, "currency_code", "BRL") or "BRL").upper()
        due = as_date(getattr(bill, "due_date", None))
        total = money(getattr(bill, "total_amount", None)) or Decimal("0.00")
        payment = money(getattr(bill, "payment_amount_observed", None)) or Decimal("0.00")
        if due is None:
            key_unknown = (account_id, currency)
            aggregate = unknown.setdefault(key_unknown, {
                "account": account_by_id[account_id], "currency": currency,
                "total_amount": Decimal("0.00"), "payment_observed": Decimal("0.00"), "count": 0,
            })
            aggregate["total_amount"] += total
            aggregate["payment_observed"] += payment
            aggregate["count"] += 1
            continue
        due_month = due.replace(day=1)
        month_text = month_key(due_month)
        key = (account_id, month_text, currency)
        aggregate = bill_totals.setdefault(key, {
            "account": account_by_id[account_id], "month": due_month, "month_key": month_text,
            "currency": currency, "total_amount": Decimal("0.00"),
            "payment_observed": Decimal("0.00"), "count": 0,
            "closing_dates": [], "due_dates": [],
        })
        aggregate["total_amount"] += total
        aggregate["payment_observed"] += payment
        aggregate["count"] += 1
        if getattr(bill, "closing_date", None):
            aggregate["closing_dates"].append(str(bill.closing_date))
        aggregate["due_dates"].append(str(bill.due_date))
        all_months[account_id].add(month_text)

    cards_by_name = {str(row.get("name") or ""): row for row in local_cards}
    current_key = month_key(selected)
    next_key = month_key(following)
    for account in accounts:
        account_id = str(account.account_id)
        mapping = mappings.get(account_id, {})
        mapped_card = mapping.get("name") if mapping.get("kind") == "CREDIT" else None
        if mapped_card in cards_by_name:
            all_months[account_id].update((current_key, next_key))
    for account_id, month_text in cycles:
        if account_id in account_by_id:
            all_months[account_id].add(month_text)

    output: dict[str, list[dict[str, Any]]] = {"past": [], "current": [], "future": [], "unknown": []}
    for account_id, currency in sorted(unknown):
        aggregate = unknown[(account_id, currency)]
        account = aggregate["account"]
        output["unknown"].append({
            **aggregate,
            "month_label": "Vencimento não informado",
            "account_name": str(account.name), "owner": str(account.owner),
            "month": None, "month_key": "", "account_id": account_id,
            "mapping_name": mappings.get(account_id, {}).get("name"),
            "provider_total": aggregate["total_amount"],
            "payment_observed": aggregate["payment_observed"],
            "bill_count": aggregate["count"], "closing_dates": [], "due_dates": [],
            "projected_total": None, "posted_charges": None, "pending_charges": None,
            "posted_count": 0, "pending_count": 0,
            "credit_adjustments_posted": None, "credit_adjustments_pending": None,
            "credit_count": 0, "credit_count_posted": 0, "credit_count_pending": 0,
            "unclassified_count": 0, "installment_count": 0,
            "cycle_start": None, "cycle_end": None, "closing_day": None,
            "purchase_rows": [],
            "review_required": False,
            "finfam_amount": None, "difference": None,
            "provider_missing": False, "projection_available": False,
            "comparison_available": False, "comparison_source": "bill",
            "display_amount": aggregate["total_amount"],
            "display_amount_label": f"Fatura Pluggy · {currency}" if currency != "BRL" else "Fatura Pluggy",
            "source_updated_at": source_dates.get(account_id),
        })

    for account in accounts:
        account_id = str(account.account_id)
        mapping = mappings.get(account_id, {})
        mapped_card = mapping.get("name") if mapping.get("kind") == "CREDIT" else None
        for month_text in all_months.get(account_id, set()):
            due_month = month_start(month_text)
            if due_month is None:
                continue
            foreign_aggregates = sorted(
                (
                    value for (bill_account_id, bill_month, currency), value in bill_totals.items()
                    if bill_account_id == account_id and bill_month == month_text and currency != "BRL"
                ),
                key=lambda value: str(value["currency"]),
            )
            for foreign in foreign_aggregates:
                currency = str(foreign["currency"])
                group = _period(due_month, selected)
                output[group].append({
                    "account_name": str(account.name), "owner": str(account.owner),
                    "institution": str(account.institution), "account_id": account_id,
                    "month": due_month, "month_key": month_text, "month_label": _month_text(due_month),
                    "currency": currency,
                    "provider_total": foreign["total_amount"],
                    "payment_observed": foreign["payment_observed"],
                    "bill_count": int(foreign["count"]),
                    "closing_dates": list(foreign["closing_dates"]),
                    "due_dates": list(foreign["due_dates"]),
                    "projected_total": None, "posted_charges": None, "pending_charges": None,
                    "posted_count": 0, "pending_count": 0,
                    "credit_adjustments_posted": None, "credit_adjustments_pending": None,
                    "credit_count": 0, "credit_count_posted": 0, "credit_count_pending": 0,
                    "unclassified_count": 0, "installment_count": 0,
                    "cycle_start": None, "cycle_end": None, "closing_day": None,
                    "purchase_rows": [],
                    "closing_day_source": None, "review_required": False,
                    "finfam_amount": None, "difference": None,
                    "mapping_name": mapped_card, "provider_missing": False,
                    "projection_available": False, "comparison_available": False,
                    "comparison_source": "bill", "display_amount": foreign["total_amount"],
                    "display_amount_label": f"Fatura Pluggy · {currency}",
                    "source_updated_at": source_dates.get(account_id),
                })
            aggregate = bill_totals.get((account_id, month_text, "BRL"))
            cycle = cycles.get((account_id, month_text))
            if aggregate is None and cycle is None:
                if mapped_card not in cards_by_name or month_text not in {current_key, next_key}:
                    continue
            planned = None
            if mapped_card in cards_by_name and (base is None or due_month >= base):
                details = monthly_details(
                    transactions, due_month, FAMILY,
                    closed=bool(closed.get(month_key(due_month), False)),
                )
                planned = money(details.get("card_by_name", {}).get(mapped_card, 0))
            provider_total = aggregate["total_amount"] if aggregate else None
            provider_payment = aggregate["payment_observed"] if aggregate else None
            cycle_total = money(cycle.get("projected_total")) if cycle else None
            comparison_amount = provider_total if provider_total is not None else cycle_total
            comparison_source = "bill" if provider_total is not None else "transactions" if cycle_total is not None else "unavailable"
            difference = None
            if comparison_amount is not None and planned is not None:
                difference = (comparison_amount - planned).quantize(CENT, rounding=ROUND_HALF_UP)
            group = _period(due_month, selected)
            output[group].append({
                "account_name": str(account.name), "owner": str(account.owner),
                "institution": str(account.institution), "account_id": account_id,
                "month": due_month, "month_key": month_text, "month_label": _month_text(due_month),
                "currency": aggregate["currency"] if aggregate else "BRL",
                "provider_total": provider_total, "payment_observed": provider_payment,
                "bill_count": int(aggregate["count"]) if aggregate else 0,
                "closing_dates": list(aggregate["closing_dates"]) if aggregate else [],
                "due_dates": list(aggregate["due_dates"]) if aggregate else [],
                "projected_total": cycle_total,
                "posted_charges": money(cycle.get("posted_charges")) if cycle else None,
                "pending_charges": money(cycle.get("pending_charges")) if cycle else None,
                "posted_count": int(cycle.get("posted_count", 0)) if cycle else 0,
                "pending_count": int(cycle.get("pending_count", 0)) if cycle else 0,
                "credit_adjustments_posted": money(cycle.get("credit_adjustments_posted")) if cycle else None,
                "credit_adjustments_pending": money(cycle.get("credit_adjustments_pending")) if cycle else None,
                "credit_count": int(cycle.get("credit_count", 0)) if cycle else 0,
                "credit_count_posted": int(cycle.get("credit_count_posted", 0)) if cycle else 0,
                "credit_count_pending": int(cycle.get("credit_count_pending", 0)) if cycle else 0,
                "unclassified_count": int(cycle.get("unclassified_count", 0)) if cycle else 0,
                "installment_count": int(cycle.get("installment_count", 0)) if cycle else 0,
                "cycle_start": cycle.get("cycle_start") if cycle else None,
                "cycle_end": cycle.get("cycle_end") if cycle else None,
                "closing_day": cycle.get("closing_day") if cycle else None,
                "closing_day_source": cycle.get("closing_day_source") if cycle else None,
                "provider_closing_date": cycle.get("provider_closing_date") if cycle else None,
                "purchase_rows": list(cycle.get("purchase_rows", [])) if cycle else [],
                "review_required": bool(cycle.get("review_required")) if cycle else False,
                "finfam_amount": planned, "difference": difference,
                "mapping_name": mapped_card,
                "provider_missing": aggregate is None,
                "projection_available": cycle_total is not None,
                "comparison_available": difference is not None,
                "comparison_source": comparison_source,
                "display_amount": provider_total if provider_total is not None else cycle_total,
                "display_amount_label": "Fatura Pluggy" if provider_total is not None else "Projeção pelo ciclo de transações" if cycle_total is not None else "Fatura não disponível",
                "source_updated_at": source_dates.get(account_id),
            })

    # Keep the selected competence and the following cycle; historical totals remain intact.
    visible_months = {current_key, next_key}
    for key in ("past", "current", "future"):
        output[key] = [row for row in output[key] if row.get("month_key") in visible_months]
    output["unknown"] = []
    output["past"].sort(key=lambda row: (row["month"], row["account_name"].casefold()), reverse=True)
    output["current"].sort(key=lambda row: (row["account_name"].casefold(), row["currency"]))
    output["future"].sort(key=lambda row: (row["month"], row["account_name"].casefold(), row["currency"]))
    return [
        {"key": "past", "label": "Meses anteriores", "rows": output["past"], "collapsed": True},
        {"key": "current", "label": "Fatura do mês selecionado", "rows": output["current"], "collapsed": False},
        {"key": "future", "label": "Ciclo seguinte", "rows": output["future"], "collapsed": False},
        {"key": "unknown", "label": "Vencimento não informado", "rows": output["unknown"], "collapsed": True},
    ]

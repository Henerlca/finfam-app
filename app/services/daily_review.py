"""Regras seguras e somente-leitura para a revisão cotidiana.

A fila usa exatamente a mesma fonte do Caixa: ``cashflow_items`` com os
status legados e os status explícitos do aplicativo aplicados. Este módulo não
faz commits; a única escrita da revisão em lote fica no router, usando a
função já existente ``save_cashflow_status``.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from app.repository import cashflow_statuses, legacy_statuses, read_snapshot
from app.services.finance_core import (
    FAMILY,
    apply_payment_statuses,
    as_date,
    cashflow_items,
    month_key,
    month_start,
)

PEOPLE = (FAMILY, "Hener", "Ju")
REVIEW_STATES = ("all", "confirmed", "pending", "predicted")
RETURN_PATHS = ("/today",)

# A baixa em lote é deliberadamente conservadora. Uma data exata já foi
# informada pela família; a fatura é um compromisso agregador confiável e pode
# ser confirmada sem transformar cada compra projetada em pagamento.
SAFE_BATCH_DATE_PRECISIONS = frozenset({"exact"})
SAFE_BATCH_TYPES = frozenset({"Fatura cartão"})
_BRAZIL_TZ = ZoneInfo("America/Sao_Paulo")


def normalize_person(value: Any) -> str:
    value = str(value or FAMILY).strip()
    return value if value in PEOPLE else FAMILY


def normalize_month(value: Any) -> str:
    """Return the canonical ``YYYY-MM`` key or raise a user-facing error."""
    try:
        return month_key(value)
    except (TypeError, ValueError):
        raise ValueError("Informe um mês válido para a revisão.") from None


def normalize_return_to(value: Any, default: str = "/today") -> str:
    """Allow only the two page paths that can originate a review form.

    Query strings are intentionally not accepted here: filters are posted as
    separate form fields and rebuilt by ``build_return_url``. This blocks an
    open redirect while retaining the user's local page context.
    """
    value = str(value or default).strip()
    if value not in RETURN_PATHS:
        raise ValueError("Destino de retorno inválido.")
    return value


def validate_paid_on(value: Any, *, today: date | None = None) -> str | None:
    """Validate an explicitly entered exact payment date.

    Empty leaves the optional payment date unchanged. A correction must not
    silently invent a payment day.
    """
    if value in (None, ""):
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = as_date(raw)
    except ValueError:
        raise ValueError("Informe uma data de baixa válida.") from None
    if parsed is None or len(raw) != 10 or raw[4] != "-" or raw[7] != "-":
        raise ValueError("A data de baixa precisa conter dia, mês e ano.")
    if parsed > current_brazil_day(today):
        raise ValueError("A data da baixa não pode estar no futuro.")
    return parsed.isoformat()


def current_brazil_day(today: date | None = None) -> date:
    """Return today's date in Brazil, with an injectable day for tests."""
    return today if today is not None else datetime.now(_BRAZIL_TZ).date()


def status_change_block_reason(month: Any, closed: bool, *, today: date | None = None) -> str | None:
    """Prevent confirmations/undo in future or closed competencies."""
    key = normalize_month(month)
    current_key = month_key(current_brazil_day(today))
    if key > current_key:
        return "Competência futura: as baixas só podem ser confirmadas quando o mês começar."
    if closed:
        return "Competência fechada: reabra o mês no Caixa para alterar baixas."
    return None


def validate_item_confirmation(item: Mapping[str, Any], paid_on: str | None, *, today: date | None = None) -> None:
    """Require a real, non-future paid date when the scheduled day is ahead."""
    current_day = current_brazil_day(today)
    if paid_on:
        parsed_paid_on = as_date(paid_on)
        if parsed_paid_on is None or parsed_paid_on > current_day:
            raise ValueError("A data da baixa precisa ser hoje ou anterior.")
        return
    planned_date = as_date(item.get("transaction_date"))
    if planned_date is not None and planned_date > current_day:
        raise ValueError("A data prevista ainda não chegou. Informe a data real da baixa para confirmar um pagamento antecipado.")


def annotate_review_actions(
    context: dict[str, Any], month: Any, closed: bool, *, today: date | None = None,
) -> dict[str, Any]:
    """Add server-derived actionability to the visible review rows."""
    current_day = current_brazil_day(today)
    reason = status_change_block_reason(month, closed, today=current_day)
    context["status_change_allowed"] = reason is None
    context["status_change_reason"] = reason or ""
    context["status_today"] = current_day.isoformat()
    for item in context.get("review_items", []):
        planned_date = as_date(item.get("transaction_date"))
        needs_paid_on = bool(not item.get("paid") and planned_date is not None and planned_date > current_day)
        item["requires_paid_on"] = needs_paid_on
        item["batch_confirmable"] = bool(
            not item.get("paid") and not needs_paid_on
            and (item.get("date_precision") in SAFE_BATCH_DATE_PRECISIONS or item.get("type") in SAFE_BATCH_TYPES)
        )
    return context


def monthly_cash_items(
    snapshot: Sequence[Any],
    month: Any,
    person: str = FAMILY,
    *,
    statuses: Mapping[str, Mapping[str, Any]] | None = None,
    legacy_rows: Iterable[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Build the current month's reconciliable items without changing data.

    ``snapshot`` is the tuple returned by :func:`read_snapshot`. Keeping this
    helper independent from a Session lets page handlers compute the queue
    from one consistent snapshot and lets tests exercise it with an in-memory
    fixture. ``statuses`` should be the result of ``cashflow_statuses``.
    """
    if not snapshot or len(snapshot) < 3:
        return []
    txs, accounts, cards = snapshot[0], snapshot[1], snapshot[2]
    selected_person = normalize_person(person)
    canonical_month = normalize_month(month)
    items = cashflow_items(txs, cards, accounts, canonical_month, selected_person)
    reconciled = apply_payment_statuses(
        items,
        [dict(row) for row in legacy_rows],
        {str(key): dict(value) for key, value in (statuses or {}).items()},
    )
    return [decorate_review_item(item) for item in reconciled]


def monthly_cash_items_from_db(db: Any, month: Any, person: str = FAMILY) -> list[dict[str, Any]]:
    """Read a review queue from a Session; this function never commits."""
    canonical_month = normalize_month(month)
    snapshot = read_snapshot(db)
    return monthly_cash_items(
        snapshot,
        canonical_month,
        person,
        statuses=cashflow_statuses(db, canonical_month),
        legacy_rows=legacy_statuses(db, canonical_month),
    )


def build_daily_review_context_from_db(
    db: Any,
    month: Any,
    person: str = FAMILY,
    **filters: Any,
) -> dict[str, Any]:
    """Build the page contract from one read-only Session snapshot."""
    canonical_month = normalize_month(month)
    return build_daily_review_context(
        read_snapshot(db),
        canonical_month,
        person,
        statuses=cashflow_statuses(db, canonical_month),
        legacy_rows=legacy_statuses(db, canonical_month),
        **filters,
    )


def decorate_review_item(item: Mapping[str, Any]) -> dict[str, Any]:
    """Add display-only state labels while preserving finance-core values."""
    row = dict(item)
    row["review_state"] = review_state(row)
    row["review_label"] = {
        "confirmed": "Confirmado",
        "pending": "Pendente",
        "predicted": "Previsto",
    }[row["review_state"]]
    row["status_origin_label"] = {
        "app": "salvo no app",
        "planilha": "origem planilha",
        "novo": "a confirmar",
    }.get(str(row.get("status_origin") or "novo"), "a confirmar")
    return row


def review_state(item: Mapping[str, Any]) -> str:
    """Return one visible state: confirmed, pending, or predicted.

    Dated unpaid items need attention now (``pending``). Recurring occurrences
    remain predicted until a person confirms them.
    """
    if bool(item.get("paid")):
        return "confirmed"
    return "pending" if item.get("date_precision") == "exact" else "predicted"


def filter_review_items(
    items: Iterable[Mapping[str, Any]],
    *,
    query: Any = "",
    bank: Any = "",
    state: Any = "all",
) -> list[dict[str, Any]]:
    """Filter a queue without changing the source list or summary totals."""
    query_text = str(query or "").strip().casefold()
    bank_text = str(bank or "").strip().casefold()
    selected_state = str(state or "all").strip().casefold()
    aliases = {"paid": "confirmed", "unpaid": "pending", "all": "all"}
    selected_state = aliases.get(selected_state, selected_state)
    if selected_state not in REVIEW_STATES:
        selected_state = "all"
    result: list[dict[str, Any]] = []
    for original in items:
        item = decorate_review_item(original)
        searchable = " ".join(
            str(item.get(field) or "")
            for field in ("description", "category", "account", "member", "type")
        ).casefold()
        if query_text and query_text not in searchable:
            continue
        if bank_text and bank_text not in str(item.get("account") or "").casefold():
            continue
        if selected_state != "all" and item["review_state"] != selected_state:
            continue
        result.append(item)
    return result


_REVIEW_GROUPS = (
    ("invoices", "Faturas de cartão"),
    ("transfers", "Transferências"),
    ("bank_expenses", "Débito/Pix e contas fixas"),
    ("income_reserves", "Receitas e reservas"),
)


def order_review_items(items: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Prioritize pending rows, then apply stable display groups only."""
    priorities = ("pending", "other")
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {
        (priority, group): [] for priority in priorities for group, _label in _REVIEW_GROUPS
    }
    for original in items:
        item = dict(original)
        kind = str(item.get("type") or "")
        if kind == "Fatura cartão":
            group = "invoices"
        elif kind in {"Transf. Env.", "Transf. Receb."}:
            group = "transfers"
        elif kind in {"Fixo", "Débito/Pix"}:
            group = "bank_expenses"
        else:
            group = "income_reserves"
        priority = "pending" if review_state(item) == "pending" else "other"
        buckets[(priority, group)].append(item)

    ordered: list[dict[str, Any]] = []
    for priority in priorities:
        for group, label in _REVIEW_GROUPS:
            rows = buckets[(priority, group)]
            count = len(rows)
            if not rows:
                continue
            group_label = f"Pendentes · {label}" if priority == "pending" else label
            for item in rows:
                item["review_group_key"] = f"{priority}_{group}"
                item["review_group_label"] = group_label
                item["review_group_count"] = count
                ordered.append(item)
    return ordered


def review_summary(items: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Count the complete queue; call this before applying display filters."""
    rows = [decorate_review_item(item) for item in items]
    counts = {state: sum(item["review_state"] == state for item in rows) for state in ("confirmed", "pending", "predicted")}
    return {
        "total": len(rows),
        "confirmed": counts["confirmed"],
        "pending": counts["pending"],
        "predicted": counts["predicted"],
        "unconfirmed": counts["pending"] + counts["predicted"],
        "safe_batch": sum(is_safe_for_batch(item) and not item.get("paid") for item in rows),
        "amount": sum(float(item.get("amount") or 0) for item in rows),
        "filtered": False,
    }


def build_daily_review_context(
    snapshot: Sequence[Any],
    month: Any,
    person: str = FAMILY,
    *,
    statuses: Mapping[str, Mapping[str, Any]] | None = None,
    legacy_rows: Iterable[Mapping[str, Any]] = (),
    query: Any = "",
    bank: Any = "",
    state: Any = "all",
    review_status: Any = None,
) -> dict[str, Any]:
    """Return the context contract pages can spread into ``render``.

    The summary is intentionally calculated from *all* monthly items, not the
    filtered list, so search and status filters never alter consolidated
    balances or counts.
    """
    all_items = monthly_cash_items(snapshot, month, person, statuses=statuses, legacy_rows=legacy_rows)
    selected_review_status = state if review_status is None else review_status
    visible = filter_review_items(all_items, query=query, bank=bank, state=selected_review_status)
    summary = review_summary(all_items)
    visible_summary = review_summary(visible)
    visible_summary["filtered"] = True
    ordered_visible = order_review_items(visible)
    return {
        "review_items": ordered_visible,
        "items": ordered_visible,
        "pending_items": [item for item in ordered_visible if not item.get("paid")],
        "review_all_items": all_items,
        "review_summary": summary,
        "review_visible_summary": visible_summary,
        "review_filters": {"q": str(query or ""), "bank": str(bank or ""), "state": str(selected_review_status or "all")},
        "review_month": normalize_month(month),
        "review_person": normalize_person(person),
    }


def is_safe_for_batch(item: Mapping[str, Any]) -> bool:
    """Whether confirming this item in a group is low-risk."""
    return item.get("date_precision") in SAFE_BATCH_DATE_PRECISIONS or item.get("type") in SAFE_BATCH_TYPES


def related_transfer_items(items: Iterable[Mapping[str, Any]], selected: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Keep both sides of a selected family transfer in sync."""
    selected_key = str(selected.get("item_key") or "")
    if selected.get("type") not in ("Transf. Env.", "Transf. Receb."):
        return [dict(selected)]
    tx_id = selected.get("transaction_id")
    related = [
        dict(item)
        for item in items
        if item.get("type") in ("Transf. Env.", "Transf. Receb.")
        and (
            tx_id is not None and item.get("transaction_id") == tx_id
            or tx_id is None and str(item.get("item_key") or "").rsplit(":", 1)[0] == selected_key.rsplit(":", 1)[0]
        )
    ]
    return related or [dict(selected)]


def safe_batch_selection(
    items: Iterable[Mapping[str, Any]],
    item_keys: Iterable[Any],
    *,
    action: str = "confirm",
) -> dict[str, Any]:
    """Resolve checked keys against the current queue and flag unsafe rows."""
    rows = [dict(item) for item in items]
    by_key = {str(item.get("item_key")): item for item in rows if item.get("item_key")}
    keys = list(dict.fromkeys(str(key).strip() for key in item_keys if str(key).strip()))
    selected = [by_key[key] for key in keys if key in by_key]
    missing = [key for key in keys if key not in by_key]
    normalized_action = str(action or "confirm").strip().casefold()
    unsafe = [] if normalized_action == "undo" else [item for item in selected if not is_safe_for_batch(item)]
    return {
        "selected": selected,
        "missing": missing,
        "unsafe": unsafe,
        "already_confirmed": [item for item in selected if item.get("paid")],
        "action": normalized_action,
    }


def build_return_url(path: Any, *, month: Any, person: Any = FAMILY, horizon: Any = "12", **filters: Any) -> str:
    """Rebuild a safe page URL from explicit context/filter form fields."""
    destination = normalize_return_to(path)
    params: dict[str, str] = {
        "month": normalize_month(month),
        "person": normalize_person(person),
        "horizon": "all" if str(horizon) == "all" else "12",
    }
    allowed = ("from", "to", "q", "bank", "state", "status")
    for key in allowed:
        value = filters.get(key)
        if value not in (None, ""):
            params[key] = str(value)
    return f"{destination}?{urlencode(params)}"


# Explicit aliases make the page-wiring contract discoverable to the existing
# route module without coupling this service to its private helpers.
monthly_cash_review_items = monthly_cash_items
get_daily_review_context = build_daily_review_context
read_daily_review_context = build_daily_review_context_from_db

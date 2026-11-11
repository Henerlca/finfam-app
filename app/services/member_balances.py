"""Saldos bancários por titular, separados do resultado orçamentário.

Este módulo é deliberadamente somente leitura.  Ele monta a lista familiar de
itens de caixa primeiro (a mesma lista usada por Caixa/Hoje), aplica os status
salvos e só então restringe os itens às contas cujo titular é a pessoa.  Assim,
um lançamento atribuído a uma pessoa no orçamento não é imputado à sua conta,
e uma compra paga por uma conta pessoal não é perdida só porque o membro do
lançamento é diferente.

A série expõe dois números diferentes para a competência observada:
``confirmed_balance`` usa somente baixas confirmadas e ``projected_balance``
usa todos os itens previstos da conta, inclusive os pendentes.  Nenhuma data de
baixa é inferida; ``paid_on`` só é carregado pelo ``apply_payment_statuses``.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any, cast

from app.services.finance_core import (
    FAMILY,
    apply_payment_statuses,
    cashflow_items,
    cashflow_summary,
    month_key,
    month_range,
    month_start,
    opening_account_balances,
)

__all__ = [
    "member_bank_series",
    "build_member_bank_series",
    "member_bank_row",
    "member_bank_rows_from",
]


def _number(value: Any) -> float:
    """Coerce numeric values without changing the source objects."""
    if value in (None, "", "-"):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _money(value: Any) -> float:
    """Return display/API money values at cent precision."""
    return round(_number(value), 2)


def _month_or_none(value: Any) -> date | None:
    try:
        return month_start(value)
    except (TypeError, ValueError):
        return None


def _normal_month_key(value: Any) -> str | None:
    month = _month_or_none(value)
    return month.isoformat()[:7] if month else None


def _state_dict(value: Any) -> dict[str, dict[str, Any]]:
    """Normalize an explicit-status payload to the repository's mapping shape."""
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for key, state in value.items():
        if isinstance(state, Mapping):
            result[str(key)] = dict(state)
        elif isinstance(state, bool):
            result[str(key)] = {"paid": state}
    return result


def _explicit_for_month(
    statuses: Mapping[str, Any] | Iterable[Mapping[str, Any]] | None,
    month: date,
    observed_key: str,
) -> dict[str, dict[str, Any]]:
    """Select statuses for one month without leaking observed state to forecasts.

    ``cashflow_statuses(db, month)`` returns a direct ``item_key -> state``
    mapping, while callers that already have a complete snapshot may provide a
    ``month -> mapping`` object or rows carrying ``month``.  Direct payloads
    are intentionally used only for ``observed_key`` because they contain no
    month metadata and must not be mistaken for future confirmations.
    """
    if statuses is None:
        return {}
    key = month_key(month)
    if isinstance(statuses, Mapping):
        status_mapping = cast(Mapping[Any, Any], statuses)
        nested = status_mapping.get(key) or status_mapping.get(month.isoformat())
        if isinstance(nested, Mapping):
            return _state_dict(nested)
        # Direct repository shape: item keys include the YYYY-MM prefix.
        if key == observed_key:
            return _state_dict(statuses)
        return {}

    rows = list(statuses)
    if key != observed_key:
        return {}
    selected: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        row_month = row.get("month")
        if row_month not in (None, "") and _normal_month_key(row_month) != key:
            continue
        item_key = row.get("item_key")
        if item_key:
            selected[str(item_key)] = {
                "paid": bool(row.get("paid")),
                "paid_on": row.get("paid_on"),
            }
    return selected


def _legacy_for_month(
    legacy_rows: Mapping[str, Any] | Iterable[Mapping[str, Any]] | None,
    month: date,
    observed_key: str,
) -> list[dict[str, Any]]:
    """Select legacy checkbox rows for one month, keeping future rows pending."""
    if legacy_rows is None:
        return []
    key = month_key(month)
    if isinstance(legacy_rows, Mapping):
        legacy_mapping = cast(Mapping[Any, Any], legacy_rows)
        nested = legacy_mapping.get(key) or legacy_mapping.get(month.isoformat())
        if nested is None:
            return []
        if isinstance(nested, Mapping):
            return [dict(nested)]
        return [dict(row) for row in nested if isinstance(row, Mapping)]

    rows = list(legacy_rows)
    if key != observed_key:
        return []
    result = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        row_month = row.get("month")
        if row_month not in (None, "") and _normal_month_key(row_month) != key:
            continue
        result.append(dict(row))
    return result


def _closed_for_month(closed_months: Mapping[str, Any] | None, month: date) -> bool:
    if not closed_months:
        return False
    key = month_key(month)
    return bool(closed_months.get(key, closed_months.get(month.isoformat(), False)))


def _settings_month(settings: Mapping[str, Any] | None, name: str, fallback: Any) -> date | None:
    value = (settings or {}).get(name, fallback)
    return _month_or_none(value)


def _account_rows_for_person(accounts: list[dict[str, Any]], person: str) -> list[dict[str, Any]]:
    if person == FAMILY:
        return list(accounts)
    return [account for account in accounts if str(account.get("owner", "")) == person]


def _sum_signed(items: Iterable[Mapping[str, Any]]) -> float:
    return sum(_number(item.get("signed_amount")) for item in items)


def _account_change(items: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    changes: dict[str, float] = {}
    for item in items:
        account = str(item.get("account") or "")
        if not account:
            continue
        changes[account] = changes.get(account, 0.0) + _number(item.get("signed_amount"))
    return changes


def member_bank_series(
    transactions: Iterable[Mapping[str, Any]],
    accounts: Iterable[Mapping[str, Any]],
    cards: Iterable[Mapping[str, Any]],
    settings: Mapping[str, Any] | None,
    statuses: Mapping[str, Any] | Iterable[Mapping[str, Any]] | None = None,
    legacy_rows: Mapping[str, Any] | Iterable[Mapping[str, Any]] | None = None,
    closed_months: Mapping[str, Any] | None = None,
    person: str = "Ju",
    *,
    start: Any = None,
    end: Any = None,
    observed_month: Any = None,
) -> list[dict[str, Any]]:
    """Build a titular-account bank series without writing or mutating input data.

    Parameters mirror the read-only snapshot used by the routes.  ``statuses``
    and ``legacy_rows`` may be the direct payload returned for one month, or a
    mapping/list carrying several months.  A direct payload is applied only to
    ``observed_month`` (which defaults to ``settings['reference_date']``), so a
    current checkbox never confirms an item in a future month.

    ``start``/``end`` are optional month bounds.  When omitted they use
    ``settings['base_month']`` and ``settings['forecast_end']``.  The returned
    rows are ordered by month and can be passed directly to
    ``build_line_chart`` using the ``confirmed`` and ``projected`` fields.
    """
    txs = [dict(row) for row in transactions]
    account_rows = [dict(row) for row in accounts]
    card_rows = [dict(row) for row in cards]
    config = settings or {}

    base_month = _settings_month(config, "base_month", start or date.today().replace(day=1))
    observed = _month_or_none(observed_month) or _settings_month(config, "reference_date", date.today())
    first = _month_or_none(start) or base_month or observed
    last = _month_or_none(end) or _settings_month(config, "forecast_end", observed)
    if first is None or last is None:
        return []
    # A selected month outside the configured horizon is still renderable; the
    # caller can pass effective_end when it has future transactions/scenarios.
    if observed is not None and observed < first:
        first = observed
    if observed is not None and observed > last:
        last = observed
    if first > last:
        return []

    observed_key = month_key(observed) if observed else month_key(first)
    owned_accounts = _account_rows_for_person(account_rows, person)
    owned_names = {str(row.get("name") or "") for row in owned_accounts}
    output: list[dict[str, Any]] = []

    for month in month_range(first, last):
        # Always build the household ledger first.  Filtering this result by
        # titular account is the key distinction from the budget's member view.
        family_items = cashflow_items(txs, card_rows, account_rows, month, FAMILY)
        reconciled_family = apply_payment_statuses(
            family_items,
            _legacy_for_month(legacy_rows, month, observed_key),
            _explicit_for_month(statuses, month, observed_key),
        )
        member_items = [item for item in reconciled_family if item.get("account") in owned_names]
        planned_member_items = [item for item in family_items if item.get("account") in owned_names]

        # This is deliberately the same opening used by /today.  It carries
        # planned prior months, not a member's budget result or a fabricated
        # payment date.
        opening_by_account = opening_account_balances(
            txs, account_rows, base_month or first, month, cards=card_rows,
        )
        opening_total = sum(_number(opening_by_account.get(name)) for name in owned_names)
        planned_change = _sum_signed(planned_member_items)
        planned_by_account = _account_change(planned_member_items)
        projected_total = opening_total + planned_change

        is_observed = observed is not None and month_key(month) == observed_key
        summary: dict[str, Any] = {}
        if is_observed:
            # cashflow_summary owns the confirmed account-balance aggregation;
            # account_items keeps it on the complete household ledger while
            # ``member_items`` keeps personal counters scoped to this person.
            summary = cashflow_summary(
                member_items,
                account_rows,
                opening_by_account,
                person,
                account_items=reconciled_family,
            )
            confirmed_total = _number(summary.get("live_total"))
            confirmed_change = _sum_signed(item for item in member_items if item.get("paid"))
            pending_items = [item for item in member_items if not item.get("paid")]
            pending_count = len(pending_items)
            pending_amount = sum(_number(item.get("amount")) for item in pending_items)
            pending_change = _sum_signed(pending_items)
            status_scope = "observed"
        else:
            confirmed_total = None
            confirmed_change = None
            pending_items = []
            pending_count = None
            pending_amount = None
            pending_change = None
            status_scope = "future"

        account_summary_by_name = {
            str(row.get("account")): row for row in (summary.get("account_balances", []) if is_observed else [])
        }
        account_balances = []
        for account in owned_accounts:
            name = str(account.get("name") or "")
            observed_account = account_summary_by_name.get(name, {})
            account_confirmed = observed_account.get("live") if is_observed else None
            account_confirmed_change = observed_account.get("paid_change") if is_observed else None
            account_projected_change = planned_by_account.get(name, 0.0)
            account_balances.append({
                "account": name,
                "owner": account.get("owner", ""),
                "opening": _money(opening_by_account.get(name)),
                "confirmed_change": _money(account_confirmed_change) if is_observed else None,
                "confirmed": _money(account_confirmed) if is_observed else None,
                "projected_change": _money(account_projected_change),
                "projected": _money(_number(opening_by_account.get(name)) + account_projected_change),
            })

        row = {
            "month": month,
            "month_key": month_key(month),
            "person": person,
            "is_observed": is_observed,
            "status_scope": status_scope,
            "closed": _closed_for_month(closed_months, month),
            "accounts": [name for name in sorted(owned_names, key=str.casefold)],
            "account_balances": account_balances,
            "opening_balance": _money(opening_total),
            "planned_change": _money(planned_change),
            "confirmed_change": _money(confirmed_change) if is_observed else None,
            "pending_change": _money(pending_change) if is_observed else None,
            "pending_count": pending_count,
            "pending_amount": _money(pending_amount) if is_observed else None,
            "confirmed_balance": _money(confirmed_total) if is_observed else None,
            "projected_balance": _money(projected_total),
            # Short names make chart specifications declarative and mirror the
            # existing chart service's field-oriented API.
            "confirmed": _money(confirmed_total) if is_observed else None,
            "projected": _money(projected_total),
        }
        output.append(row)

    return output


# A descriptive alias is useful to route callers and keeps the module API easy
# to discover without introducing a second implementation.
build_member_bank_series = member_bank_series


def member_bank_row(series: Iterable[Mapping[str, Any]], month: Any) -> dict[str, Any] | None:
    """Return a copy of the row for ``month`` from a member bank series."""
    wanted = _normal_month_key(month)
    if wanted is None:
        return None
    for row in series:
        if str(row.get("month_key") or _normal_month_key(row.get("month"))) == wanted:
            return dict(row)
    return None


def member_bank_rows_from(series: Iterable[Mapping[str, Any]], month: Any) -> list[dict[str, Any]]:
    """Return copied series rows at or after ``month`` for page/chart horizons."""
    wanted = _month_or_none(month)
    if wanted is None:
        return []
    result = []
    for row in series:
        row_month = _month_or_none(row.get("month"))
        if row_month is not None and row_month >= wanted:
            result.append(dict(row))
    return result

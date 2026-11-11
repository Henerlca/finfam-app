"""Pure helpers for credit-card closing dates and first-bill suggestions.

The application stores the per-card values in the existing ``AppSetting`` row
under :data:`CARD_CLOSING_DAYS_SETTING_KEY`; this module deliberately has no
SQLAlchemy or route dependencies so it can be used by both routes and tests.
"""

from __future__ import annotations

import math
import re
from calendar import monthrange
from datetime import date, datetime
from numbers import Real
from typing import Any, Iterable, Mapping

DEFAULT_CARD_CLOSING_DAY = 3
CARD_CLOSING_DAYS_SETTING_KEY = "card_closing_days"
CARD_BILLING_TYPES = frozenset({"Crédito à vista", "Parcelado"})

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})(?:-01)?$")
_MISSING = object()


def _value(record: Any, key: str, default: Any = _MISSING) -> Any:
    if isinstance(record, Mapping):
        if key in record:
            return record[key]
    else:
        value = getattr(record, key, _MISSING)
        if value is not _MISSING:
            return value
    return default


def validate_closing_day(value: Any) -> int:
    """Validate and normalize a closing day to an integer in ``1..31``.

    ``bool`` is rejected explicitly even though Python considers it an
    ``int``.  Numeric values must be finite and integral; this also accepts a
    JSON value such as ``3.0`` while rejecting ``3.5``, NaN and infinity.
    """

    if isinstance(value, bool) or value is None:
        raise ValueError("O dia de fechamento deve ser um inteiro entre 1 e 31.")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("O dia de fechamento deve ser um inteiro entre 1 e 31.")
        try:
            numeric = float(text)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("O dia de fechamento deve ser um inteiro entre 1 e 31.") from None
    elif isinstance(value, Real):
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("O dia de fechamento deve ser um inteiro entre 1 e 31.") from None
    else:
        raise ValueError("O dia de fechamento deve ser um inteiro entre 1 e 31.")
    if not math.isfinite(numeric) or not numeric.is_integer():
        raise ValueError("O dia de fechamento deve ser um inteiro entre 1 e 31.")
    day = int(numeric)
    if not 1 <= day <= 31:
        raise ValueError("O dia de fechamento deve estar entre 1 e 31.")
    return day


def _exact_date(value: Any) -> date:
    """Parse a complete ISO calendar date."""

    if isinstance(value, bool) or value is None:
        raise ValueError("Informe a data do lançamento para sugerir a primeira fatura.")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ValueError("Informe a data do lançamento para sugerir a primeira fatura.")
    text = value.strip()
    if not _DATE_RE.fullmatch(text):
        raise ValueError("Informe a data do lançamento para sugerir a primeira fatura.")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ValueError("Informe uma data válida para sugerir a primeira fatura.") from None


def normalize_month(value: Any) -> str:
    """Return a valid month as ``YYYY-MM-01``.

    Both the HTML ``month`` value (``YYYY-MM``) and the backend's canonical
    value (``YYYY-MM-01``) are accepted.  No date is guessed for malformed
    input.
    """

    if isinstance(value, bool) or value is None:
        raise ValueError("Informe um mês válido para a primeira fatura.")
    text = str(value).strip()
    match = _MONTH_RE.fullmatch(text)
    if not match:
        raise ValueError("Informe um mês válido para a primeira fatura.")
    year, month = int(match.group(1)), int(match.group(2))
    try:
        date(year, month, 1)
    except ValueError:
        raise ValueError("Informe um mês válido para a primeira fatura.") from None
    return f"{year:04d}-{month:02d}-01"


def suggest_first_bill_month(transaction_date: Any, closing_day: Any = DEFAULT_CARD_CLOSING_DAY) -> str:
    """Suggest the first invoice month for an exact purchase date.

    The closing day is clamped to the actual last day of the purchase month,
    so February and short months behave naturally.  A purchase on the
    effective closing day belongs to the current month; a purchase after it
    belongs to the following month, including December to January rollover.
    """

    purchase = _exact_date(transaction_date)
    closing = validate_closing_day(closing_day)
    last_day = monthrange(purchase.year, purchase.month)[1]
    effective_closing = min(closing, last_day)
    if purchase.day > effective_closing:
        if purchase.month == 12:
            purchase = date(purchase.year + 1, 1, 1)
        else:
            purchase = date(purchase.year, purchase.month + 1, 1)
    return purchase.replace(day=1).isoformat()


def _stored_closing_days(settings: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(settings, Mapping):
        return {}
    raw = settings.get(CARD_CLOSING_DAYS_SETTING_KEY, {})
    return raw if isinstance(raw, Mapping) else {}


def card_closing_days(
    cards: Iterable[Any] | None = None,
    settings: Mapping[str, Any] | None = None,
) -> dict[str, int]:
    """Return sanitized ``card name -> closing day`` values.

    A card's explicit ``closing_day`` property wins, then the persisted
    ``card_closing_days`` setting, then the default day 3.  Invalid legacy
    values are treated as the default rather than making a form unusable.
    Unknown persisted names are retained so a settings round-trip does not
    silently discard them; callers that save settings can pass only current
    cards if they want a compact map.
    """

    stored = _stored_closing_days(settings)
    result: dict[str, int] = {}
    rows = list(cards or [])
    for card in rows:
        name = str(_value(card, "name", "") or "").strip()
        if not name:
            continue
        explicit = _value(card, "closing_day", _MISSING)
        raw = explicit if explicit is not _MISSING and explicit not in (None, "") else stored.get(name, DEFAULT_CARD_CLOSING_DAY)
        try:
            result[name] = validate_closing_day(raw)
        except ValueError:
            result[name] = DEFAULT_CARD_CLOSING_DAY
    for raw_name, raw_day in stored.items():
        name = str(raw_name).strip()
        if not name or name in result:
            continue
        try:
            result[name] = validate_closing_day(raw_day)
        except ValueError:
            result[name] = DEFAULT_CARD_CLOSING_DAY
    return result


def closing_day_for_card(
    card_name: Any,
    cards: Iterable[Any] | None = None,
    settings: Mapping[str, Any] | None = None,
) -> int:
    """Look up a card's closing day, falling back safely to day 3."""

    name = str(card_name or "").strip()
    return card_closing_days(cards, settings).get(name, DEFAULT_CARD_CLOSING_DAY)


def cards_with_closing_days(
    cards: Iterable[Any] | None,
    settings: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Copy card rows and add the normalized ``closing_day`` property."""

    rows = list(cards or [])
    mapping = card_closing_days(rows, settings)
    enriched: list[dict[str, Any]] = []
    for card in rows:
        if isinstance(card, Mapping):
            row = dict(card)
        else:
            row = {key: getattr(card, key) for key in ("name", "bank", "limit_amount") if hasattr(card, key)}
        name = str(row.get("name") or "").strip()
        if name:
            row["closing_day"] = mapping.get(name, DEFAULT_CARD_CLOSING_DAY)
        enriched.append(row)
    return enriched


def card_billing_context(
    cards: Iterable[Any] | None,
    settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the two values templates need for card-billing UI data."""

    mapping = card_closing_days(cards, settings)
    return {"cards": cards_with_closing_days(cards, settings), "card_closing_days": mapping}


def validate_card_closing_days_form(
    form: Mapping[str, Any],
    cards: Iterable[Any] | None,
    settings: Mapping[str, Any] | None = None,
) -> dict[str, int]:
    """Validate every indexed settings field before a caller commits anything.

    Missing fields retain their current/default value, which keeps old clients
    compatible.  A present blank, non-finite, non-integral or out-of-range
    field raises before returning any partially accepted map.
    """

    rows = list(cards or [])
    current = card_closing_days(rows, settings)
    result: dict[str, int] = {}
    for index, card in enumerate(rows):
        name = str(_value(card, "name", "") or "").strip()
        if not name:
            continue
        field = f"card_closing_day_{index}"
        if field not in form:
            result[name] = current.get(name, DEFAULT_CARD_CLOSING_DAY)
            continue
        raw = form.get(field)
        try:
            result[name] = validate_closing_day(raw)
        except ValueError:
            raise ValueError(f"Informe um dia de fechamento válido para o cartão {name}.") from None
    return result


def first_bill_month_for_form(
    form: Mapping[str, Any],
    cards: Iterable[Any] | None = None,
    settings: Mapping[str, Any] | None = None,
) -> str:
    """Resolve the first bill month from a complete transaction date."""

    tx_type = str(form.get("type", "") or "").strip()
    raw_first_bill = str(form.get("first_bill_month", "") or "").strip()
    purchase = _exact_date(form.get("transaction_date", ""))
    if tx_type not in CARD_BILLING_TYPES:
        return purchase.replace(day=1).isoformat()
    if raw_first_bill:
        return normalize_month(raw_first_bill)
    closing = closing_day_for_card(form.get("account_card", ""), cards, settings)
    return suggest_first_bill_month(purchase, closing)


# Explicit aliases keep the integration points readable in routes.py and make
# the pure operations convenient to import from focused tests.
get_card_closing_days = card_closing_days
get_cards_with_closing_days = cards_with_closing_days
resolve_first_bill_month = first_bill_month_for_form
validate_card_closing_days = validate_card_closing_days_form


__all__ = [
    "CARD_BILLING_TYPES",
    "CARD_CLOSING_DAYS_SETTING_KEY",
    "DEFAULT_CARD_CLOSING_DAY",
    "card_billing_context",
    "card_closing_days",
    "closing_day_for_card",
    "cards_with_closing_days",
    "first_bill_month_for_form",
    "get_card_closing_days",
    "get_cards_with_closing_days",
    "normalize_month",
    "resolve_first_bill_month",
    "suggest_first_bill_month",
    "validate_card_closing_days",
    "validate_card_closing_days_form",
    "validate_closing_day",
]

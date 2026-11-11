"""Card billing tests use synthetic values only; no application database is touched."""

from __future__ import annotations

import math
import shutil
from pathlib import Path

import pytest

from app.services.card_billing import (
    card_billing_context,
    first_bill_month_for_form,
    suggest_first_bill_month,
    validate_card_closing_days_form,
    validate_closing_day,
)


@pytest.mark.parametrize(
    ("card_day", "purchase", "expected"),
    [
        (3, "2031-05-02", "2031-05-01"),  # before closing
        (15, "2031-05-15", "2031-05-01"),  # exactly on another card's closing
        (3, "2031-05-04", "2031-06-01"),  # after closing
    ],
)
def test_suggestion_uses_each_card_closing_day(card_day, purchase, expected):
    assert suggest_first_bill_month(purchase, card_day) == expected


def test_leap_short_months_and_december_rollover_are_calendar_safe():
    assert suggest_first_bill_month("2032-02-29", 31) == "2032-02-01"
    assert suggest_first_bill_month("2031-02-28", 31) == "2031-02-01"
    assert suggest_first_bill_month("2031-02-28", 27) == "2031-03-01"
    assert suggest_first_bill_month("2035-12-31", 31) == "2035-12-01"
    assert suggest_first_bill_month("2035-12-31", 3) == "2036-01-01"


def test_invalid_date_and_closing_day_values_are_rejected_including_bool_and_nonfinite():
    for value in (True, False, 0, 32, 3.5, float("nan"), float("inf"), ""):
        with pytest.raises(ValueError):
            validate_closing_day(value)
    for value in ("2031-02-29", "2031-02-", "2031-02", True, None):
        with pytest.raises(ValueError):
            suggest_first_bill_month(value, 3)


def test_form_context_enriches_cards_and_uses_default_three():
    context = card_billing_context(
        [{"name": "Visa", "bank": "Conta", "limit_amount": 1000}, {"name": "Master", "closing_day": 12}],
        {"card_closing_days": {"Visa": 7}},
    )
    assert context["card_closing_days"] == {"Visa": 7, "Master": 12}
    assert [card["closing_day"] for card in context["cards"]] == [7, 12]
    assert context["card_closing_days"].get("Missing", 3) == 3


def test_all_indexed_settings_fields_validate_before_result_is_returned():
    cards = [{"name": "Visa"}, {"name": "Master"}, {"name": "Amex"}]
    assert validate_card_closing_days_form(
        {"card_closing_day_0": "1", "card_closing_day_1": "31", "card_closing_day_2": "3"}, cards
    ) == {"Visa": 1, "Master": 31, "Amex": 3}
    with pytest.raises(ValueError):
        validate_card_closing_days_form(
            {"card_closing_day_0": "1", "card_closing_day_1": "nan", "card_closing_day_2": "3"}, cards
        )


def test_first_bill_requires_complete_purchase_date_for_automatic_or_manual_values():
    cards = [{"name": "Visa"}]
    settings = {"card_closing_days": {"Visa": 3}}
    assert first_bill_month_for_form(
        {"type": "Parcelado", "transaction_date": "2031-05-03", "account_card": "Visa", "first_bill_month": ""},
        cards,
        settings,
    ) == "2031-05-01"
    with pytest.raises(ValueError, match="data do lançamento"):
        first_bill_month_for_form(
            {
                "type": "Parcelado",
                "transaction_date": "2031-05",
                "account_card": "Visa",
                "first_bill_month": "",
            },
            cards,
            settings,
        )
    with pytest.raises(ValueError, match="data do lançamento"):
        first_bill_month_for_form(
            {"type": "Crédito à vista", "transaction_date": "2031-05", "first_bill_month": "2031-06"},
            cards,
            settings,
        )
    # A complete transaction date still allows a deliberate manual override.
    assert first_bill_month_for_form(
        {"type": "Crédito à vista", "transaction_date": "2031-05-04", "first_bill_month": "2031-06"},
        cards,
        settings,
    ) == "2031-06-01"


def test_browser_manual_override_reset_and_source_change_without_database():
    playwright = pytest.importorskip("playwright.sync_api")
    browser_path = shutil.which("chromium")
    if not browser_path:
        pytest.skip("Chromium não disponível para o teste de UI isolado")
    root = Path(__file__).resolve().parents[1]
    script = (root / "app/static/js/card-billing.js").read_text()
    markup = """
      <form data-transaction-form data-card-closing-days='{"Visa":3,"Master":15}'>
        <input name="transaction_date" type="date" value="2031-05-03">
        <select name="type" data-type-selector>
          <option>Débito/Pix</option><option>Parcelado</option>
        </select>
        <select name="account_card">
          <option value="">Escolha</option>
          <option value="Visa" data-closing-day="3">Visa</option>
          <option value="Master" data-closing-day="15">Master</option>
        </select>
        <label data-card-billing-field hidden>Mês<input name="first_bill_month" data-first-bill-month>
          <span data-first-bill-mode></span><small data-first-bill-status></small>
          <button type="button" data-reset-first-bill>Usar sugestão</button>
        </label>
      </form>
    """
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=browser_path, args=["--no-sandbox"])
        page = browser.new_page()
        page.set_content(f"<body>{markup}<script>{script}</script></body>")
        page.locator('[name="account_card"]').select_option("Visa")
        page.locator('[name="type"]').select_option(label="Parcelado")
        assert page.locator('[name="first_bill_month"]').input_value() == "2031-05"
        page.locator('[name="first_bill_month"]').fill("2031-07")
        assert page.locator("[data-first-bill-mode]").inner_text() == "Manual"
        # A manual value survives a reset request only until the user requests the suggestion.
        page.locator("[data-reset-first-bill]").click()
        assert page.locator('[name="first_bill_month"]').input_value() == "2031-05"
        assert page.locator("[data-first-bill-mode]").inner_text() == "Automática"
        page.locator('[name="first_bill_month"]').fill("2031-07")
        page.locator('[name="account_card"]').select_option("Master")
        assert page.locator('[name="first_bill_month"]').input_value() == "2031-05"
        assert page.locator("[data-first-bill-mode]").inner_text() == "Automática"
        browser.close()


def test_browser_preserves_prefilled_existing_value_on_load():
    playwright = pytest.importorskip("playwright.sync_api")
    browser_path = shutil.which("chromium")
    if not browser_path:
        pytest.skip("Chromium não disponível para o teste de UI isolado")
    root = Path(__file__).resolve().parents[1]
    script = (root / "app/static/js/card-billing.js").read_text()
    markup = """
      <form data-transaction-form data-card-closing-days='{"Visa":3}'>
        <input name="transaction_date" type="date" value="2031-05-04">
        <select name="type" data-type-selector><option>Crédito à vista</option></select>
        <select name="account_card"><option value="Visa" data-closing-day="3" selected>Visa</option></select>
        <label data-card-billing-field>Mês<input name="first_bill_month" data-first-bill-month value="2031-09">
          <span data-first-bill-mode></span><small data-first-bill-status></small>
          <button type="button" data-reset-first-bill>Usar sugestão</button>
        </label>
      </form>
    """
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=browser_path, args=["--no-sandbox"])
        page = browser.new_page()
        page.set_content(f"<body>{markup}<script>{script}</script></body>")
        assert page.locator('[name="first_bill_month"]').input_value() == "2031-09"
        assert page.locator("[data-first-bill-mode]").inner_text() == "Manual"
        assert not page.locator("[data-reset-first-bill]").is_disabled()
        page.locator("[data-reset-first-bill]").click()
        assert page.locator('[name="first_bill_month"]').input_value() == "2031-06"
        assert page.locator("[data-first-bill-mode]").inner_text() == "Automática"
        browser.close()

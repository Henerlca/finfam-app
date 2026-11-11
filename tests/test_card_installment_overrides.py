from __future__ import annotations

from unittest.mock import patch

import pytest

from app.routes import _transaction_from_form
from app.models import Card
from app.repository import _normalized_transaction
from app.services.family_planning import card_calendar
from app.services.finance_core import (
    card_limits, cashflow_items, confirmed_pot_usage, monthly_details,
    opening_account_balances, projection,
)


def purchase(overrides=None):
    return {
        "id": 901, "member": "Família", "category": "Casa", "description": "Compra parcelada",
        "type": "Parcelado", "account_card": "Cartão X", "amount": 100,
        "total_amount": 300, "installment_count": 3,
        "start_month": "2031-01-01", "end_month": "2031-03-01",
        "destination_account": "", "linked_pot": "Viagem", "card_flag": 1,
        "payment_bank": "Conta", "transaction_date": "2031-01-04",
        "date_precision": "exact", "archived": False,
        "installment_overrides": overrides or {},
    }


CARDS = [{"name": "Cartão X", "bank": "Conta", "limit_amount": 500}]
ACCOUNTS = [{"name": "Conta", "owner": "Família", "initial_balance": 1000}]


def calendar_month(rows, key):
    return next(row for row in rows[0]["months"] if row["month_key"] == key)


def test_no_overrides_preserves_current_baseline_results():
    tx = purchase()
    old = dict(tx)
    months = ["2031-01", "2031-02", "2031-03"]
    cal = card_calendar([tx], CARDS, months)
    flow = [cashflow_items([tx], CARDS, ACCOUNTS, month)[0]["amount"] for month in months]
    details = [monthly_details([tx], month)["card_total"] for month in months]
    assert [calendar_month(cal, month)["invoice"] for month in months] == [100.0, 100.0, 100.0]
    assert flow == [100.0, 100.0, 100.0]
    assert details == [100.0, 100.0, 100.0]
    assert tx == old


def test_override_changes_only_selected_competence_and_matches_cashflow_projection():
    tx = purchase({"2031-02": "75.25"})
    months = ["2031-01", "2031-02", "2031-03"]
    cal = card_calendar([tx], CARDS, months)
    invoices = [calendar_month(cal, month)["invoice"] for month in months]
    flows = [cashflow_items([tx], CARDS, ACCOUNTS, month)[0] for month in months]
    projected = projection([tx], ACCOUNTS, "2031-01", "2031-03")
    assert invoices == [100.0, 75.25, 100.0]
    assert [item["amount"] for item in flows] == invoices
    assert [item["signed_amount"] for item in flows] == [-100.0, -75.25, -100.0]
    assert [row["card_total"] for row in projected] == invoices
    assert [row["ending_balance"] for row in projected] == [900.0, 824.75, 724.75]
    assert tx["amount"] == 100 and tx["total_amount"] == 300


def test_negative_credit_at_sight_override_is_refund_and_increases_cashflow_signed_amount():
    tx = dict(purchase({"2031-02": "-20.00"}), type="Crédito à vista", amount=20,
              total_amount=20, installment_count=1, start_month="2031-02-01", end_month="2031-02-01")
    invoice = calendar_month(card_calendar([tx], CARDS, ["2031-02"]), "2031-02")
    item = cashflow_items([tx], CARDS, ACCOUNTS, "2031-02")[0]
    assert invoice["invoice"] == -20.0
    assert item["amount"] == -20.0
    assert item["signed_amount"] == 20.0
    assert monthly_details([tx], "2031-02")["card_total"] == -20.0


def test_negative_override_is_rejected_for_installment_purchase_before_persistence():
    tx = purchase({"2031-02": "-0.01"})
    card = Card(name="Cartão X", bank="Conta", limit_amount=500)
    with pytest.raises(ValueError, match="Ajustes negativos só são permitidos"):
        _normalized_transaction(tx, [card])


def test_negative_credit_at_sight_is_signed_through_invoice_balance_and_limit():
    tx = dict(purchase(), type="Crédito à vista", amount=-23.9, total_amount=-23.9,
              installment_count=1, start_month="2031-01-01", end_month="2031-01-01")
    invoice = calendar_month(card_calendar([tx], CARDS, ["2031-01"]), "2031-01")
    item = cashflow_items([tx], CARDS, ACCOUNTS, "2031-01")[0]
    details = monthly_details([tx], "2031-01")
    limit = card_limits([tx], CARDS, "2031-01")[0]
    projected = projection([tx], ACCOUNTS, "2031-01", "2031-01")[0]
    assert invoice["invoice"] == -23.9
    assert item["amount"] == -23.9 and item["signed_amount"] == 23.9
    assert details["card_total"] == -23.9
    assert limit["current_invoice"] == -23.9 and limit["available"] == 523.9
    assert projected["ending_balance"] == 1023.9


def test_cent_allocation_matches_all_card_views_only_in_adjusted_month():
    first = dict(purchase({"2031-01": "0.34"}), amount=0.335, total_amount=1.005)
    second = dict(purchase(), id=902, amount=0.335, total_amount=1.005)
    rows = [first, second]
    invoice = calendar_month(card_calendar(rows, CARDS, ["2031-01"]), "2031-01")["invoice"]
    flow = cashflow_items(rows, CARDS, ACCOUNTS, "2031-01")[0]["amount"]
    details = monthly_details(rows, "2031-01")["card_total"]
    limit = card_limits(rows, CARDS, "2031-01")[0]["current_invoice"]
    assert invoice == flow == details == limit == 0.68
    assert first["amount"] == second["amount"] == 0.335
    assert first["total_amount"] == second["total_amount"] == 1.005


def test_negative_transaction_form_is_limited_to_credit_at_sight_card_refunds():
    snapshot = ([], [], [{"name": "Cartão X", "bank": "Conta", "closing_day": 3}], [],
                {"card_closing_days": {"Cartão X": 3}}, {})
    form = {
        "type": "Crédito à vista", "transaction_date": "2031-01-12", "total_amount": "-23,90",
        "installment_count": "1", "account_card": "Cartão X", "first_bill_month": "2031-02",
        "description": "Estorno", "member": "Hener", "category": "Compras",
    }
    with patch("app.routes._snapshot", return_value=snapshot):
        result = _transaction_from_form(form)
        assert result["amount"] == result["total_amount"] == -23.9
        for changes in (
            {"type": "Débito/Pix", "account_card": "Conta"},
            {"type": "Parcelado", "account_card": "Cartão X", "installment_count": "2"},
            {"type": "Crédito à vista", "account_card": "Conta"},
        ):
            with pytest.raises(ValueError):
                _transaction_from_form({**form, **changes})


def test_limit_opening_balance_and_confirmed_pot_usage_use_final_installment():
    tx = purchase({"2031-02": "75.25"})
    limit = card_limits([tx], CARDS, "2031-02-01")[0]
    assert limit["current_invoice"] == 75.25
    assert limit["total_committed"] == 175.25
    assert limit["available"] == 324.75
    opening = opening_account_balances([tx], ACCOUNTS, "2031-01", "2031-04", cards=CARDS)
    assert opening["Conta"] == 724.75
    paid = [{"paid": True, "type": "Fatura cartão", "item_key": "2031-02:invoice:Cartão X"}]
    assert confirmed_pot_usage([tx], "2031-02", paid, ["Viagem"]) == {
        "Viagem": {"amount": 75.25, "count": 1}
    }


def test_installment_override_after_2030_is_applied():
    tx = dict(purchase({"2032-01": "88.88"}), start_month="2031-12-01", end_month="2032-02-01")
    cal = card_calendar([tx], CARDS, ["2032-01"])
    flow = cashflow_items([tx], CARDS, ACCOUNTS, "2032-01")[0]
    projected = projection([tx], ACCOUNTS, "2032-01", "2032-01")[0]
    assert calendar_month(cal, "2032-01")["invoice"] == 88.88
    assert flow["amount"] == 88.88
    assert projected["card_total"] == 88.88

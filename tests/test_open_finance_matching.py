from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from app.services.open_finance_matching import enforce_one_to_one, match_bank_transaction


TODAY = date(2026, 10, 29)
MAPPING = {"kind": "BANK", "name": "Conta Hener"}
ACCOUNT = SimpleNamespace(account_type="BANK")


def external(**overrides):
    values = {
        "transaction_id": "bank-pix-1", "currency_code": "BRL", "status": "POSTED",
        "transaction_type": "DEBIT", "occurred_on": "2026-10-15", "amount": Decimal("3250.00"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def planned(**overrides):
    values = {
        "item_key": "2026-10:tx:1", "transaction_id": 1, "type": "Fixo",
        "description": "Aluguel", "category": "Casa", "account": "Conta Hener",
        "transaction_date": "2026-10-15", "month": date(2026, 10, 1),
        "amount": Decimal("3200.00"), "paid": False,
        "confirmation_block_reason": "",
    }
    values.update(overrides)
    return values


def test_explicit_posted_bank_pix_matches_rent_and_offers_individual_approval():
    result = match_bank_transaction(external(), ACCOUNT, MAPPING, [planned()], today=TODAY)

    assert result["state"] == "candidate"
    assert result["can_confirm"] is True
    candidate = result["candidate"]
    assert candidate["description"] == "Aluguel"
    assert candidate["amount"] == Decimal("3200.00")
    assert candidate["external_amount"] == Decimal("3250.00")
    assert candidate["difference"] == Decimal("50.00")
    assert candidate["percent_difference"] == Decimal("1.56")
    assert candidate["tolerance"] == Decimal("50.00")
    assert candidate["month_key"] == "2026-10"


def test_pending_transaction_can_suggest_but_never_confirm():
    result = match_bank_transaction(
        external(status="PENDING"), ACCOUNT, MAPPING, [planned()], today=TODAY,
    )
    assert result["state"] == "candidate"
    assert result["pending_bank"] is True
    assert result["can_confirm"] is False


def test_amount_tolerance_is_bounded_and_date_window_is_four_days():
    too_large = match_bank_transaction(
        external(amount=Decimal("3251.00")), ACCOUNT, MAPPING, [planned()], today=TODAY,
    )
    too_late = match_bank_transaction(
        external(occurred_on="2026-10-20"), ACCOUNT, MAPPING, [planned()], today=TODAY,
    )
    assert too_large["state"] == "none"
    assert too_late["state"] == "none"


def test_direction_account_currency_and_internal_transfers_are_not_guessed():
    direction = match_bank_transaction(
        external(transaction_type="CREDIT"), ACCOUNT, MAPPING, [planned()], today=TODAY,
    )
    wrong_account = match_bank_transaction(
        external(), ACCOUNT, {"kind": "BANK", "name": "Conta Ju"}, [planned()], today=TODAY,
    )
    foreign = match_bank_transaction(
        external(currency_code="USD"), ACCOUNT, MAPPING, [planned()], today=TODAY,
    )
    transfer = match_bank_transaction(
        external(), ACCOUNT, MAPPING, [planned(type="Transferência")], today=TODAY,
    )
    assert direction["state"] == "none"
    assert wrong_account["state"] == "none"
    assert foreign["reason"] == "currency"
    assert transfer["state"] == "none"


def test_multiple_local_candidates_are_ambiguous_and_never_confirmable():
    result = match_bank_transaction(
        external(), ACCOUNT, MAPPING,
        [planned(), planned(item_key="2026-10:tx:2", transaction_id=2, description="Outro gasto")],
        today=TODAY,
    )
    assert result["state"] == "ambiguous"
    assert len(result["candidates"]) == 2


def test_two_bank_transactions_cannot_claim_one_planned_occurrence():
    first = match_bank_transaction(external(), ACCOUNT, MAPPING, [planned()], today=TODAY)
    second = match_bank_transaction(
        external(transaction_id="bank-pix-2", amount=Decimal("3249.00")),
        ACCOUNT, MAPPING, [planned()], today=TODAY,
    )
    result = enforce_one_to_one({"bank-pix-1": first, "bank-pix-2": second})
    assert result["bank-pix-1"]["state"] == "ambiguous"
    assert result["bank-pix-2"]["state"] == "ambiguous"
    assert not result["bank-pix-1"]["can_confirm"]


def test_already_paid_occurrence_and_closed_or_future_month_cannot_be_confirmed():
    paid = match_bank_transaction(
        external(), ACCOUNT, MAPPING, [planned(paid=True)], today=TODAY,
    )
    closed = match_bank_transaction(
        external(), ACCOUNT, MAPPING,
        [planned(confirmation_block_reason="Competência fechada.")], today=TODAY,
    )
    future = match_bank_transaction(
        external(occurred_on="2026-11-01"), ACCOUNT, MAPPING, [planned()], today=TODAY,
    )
    assert paid["state"] == "already_confirmed"
    assert closed["state"] == "candidate" and not closed["can_confirm"]
    assert future["state"] == "unavailable"


CARD_ACCOUNT = SimpleNamespace(account_type="CREDIT")
CARD_MAPPING = {"kind": "CREDIT", "name": "Visa Hener"}
CARD_ROWS = [{"name": "Visa Hener"}]
CARD_SETTINGS = {"card_closing_days": {"Visa Hener": 3}}


def card_external(**overrides):
    values = {
        "transaction_id": "card-purchase-1", "currency_code": "BRL", "status": "POSTED",
        "transaction_type": "DEBIT", "occurred_on": "2026-10-15",
        "amount": Decimal("100.00"), "installment_number": 1, "total_installments": 10,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def card_planned(**overrides):
    values = {
        "id": 9, "type": "Parcelado", "description": "Geladeira",
        "category": "Casa", "transaction_date": "2026-10-15",
        "start_month": "2026-11-01", "end_month": "2027-08-01",
        "installment_count": 10, "amount": Decimal("100.00"),
        "card_flag": 1, "account_card": "Visa Hener", "archived": False,
    }
    values.update(overrides)
    return values


def test_card_purchase_matches_configured_first_bill_and_installment_cycle_without_confirming():
    from app.services.open_finance_matching import match_card_purchase

    first = match_card_purchase(
        card_external(installment_number=1), CARD_ACCOUNT, CARD_MAPPING,
        [card_planned()], CARD_ROWS, CARD_SETTINGS, today=TODAY,
    )
    second = match_card_purchase(
        card_external(transaction_id="card-purchase-2", installment_number=2),
        CARD_ACCOUNT, CARD_MAPPING, [card_planned()], CARD_ROWS, CARD_SETTINGS, today=TODAY,
    )

    assert first["state"] == "candidate"
    assert first["candidate"]["first_bill_month"] == "2026-11"
    assert first["candidate"]["invoice_month"] == "2026-11"
    assert first["can_confirm"] is False
    assert second["state"] == "candidate"
    assert second["candidate"]["invoice_month"] == "2026-12"
    assert second["candidate"]["installment_number"] == 2


def test_card_match_requires_configured_cycle_installment_metadata_and_five_cent_maximum():
    from app.services.open_finance_matching import match_card_purchase

    wrong_cycle = match_card_purchase(
        card_external(), CARD_ACCOUNT, CARD_MAPPING,
        [card_planned(start_month="2026-10-01", end_month="2027-07-01")],
        CARD_ROWS, CARD_SETTINGS, today=TODAY,
    )
    wrong_installments = match_card_purchase(
        card_external(installment_number=2, total_installments=12),
        CARD_ACCOUNT, CARD_MAPPING, [card_planned()], CARD_ROWS, CARD_SETTINGS, today=TODAY,
    )
    within = match_card_purchase(
        card_external(amount=Decimal("100.05")), CARD_ACCOUNT, CARD_MAPPING,
        [card_planned()], CARD_ROWS, CARD_SETTINGS, today=TODAY,
    )
    outside = match_card_purchase(
        card_external(amount=Decimal("100.06")), CARD_ACCOUNT, CARD_MAPPING,
        [card_planned()], CARD_ROWS, CARD_SETTINGS, today=TODAY,
    )

    assert wrong_cycle["state"] == "none"
    assert wrong_installments["state"] == "none"
    assert within["candidate"]["difference"] == Decimal("0.05")
    assert outside["state"] == "none"


def test_card_refunds_and_competing_external_rows_do_not_produce_a_confirmable_match():
    from app.services.open_finance_matching import match_card_purchase

    refund = match_card_purchase(
        card_external(transaction_type="CREDIT"), CARD_ACCOUNT, CARD_MAPPING,
        [card_planned()], CARD_ROWS, CARD_SETTINGS, today=TODAY,
    )
    first = match_card_purchase(
        card_external(), CARD_ACCOUNT, CARD_MAPPING, [card_planned()], CARD_ROWS, CARD_SETTINGS, today=TODAY,
    )
    second = match_card_purchase(
        card_external(transaction_id="card-purchase-2", amount=Decimal("99.95")),
        CARD_ACCOUNT, CARD_MAPPING, [card_planned()], CARD_ROWS, CARD_SETTINGS, today=TODAY,
    )
    competed = enforce_one_to_one({"card-purchase-1": first, "card-purchase-2": second})

    assert refund["state"] == "unavailable"
    assert competed["card-purchase-1"]["state"] == "ambiguous"
    assert competed["card-purchase-2"]["state"] == "ambiguous"
    assert not competed["card-purchase-1"].get("can_confirm", False)

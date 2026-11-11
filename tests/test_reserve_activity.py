from app.services.reserve_activity import daily_reserve_summary, reserve_usage_details


def _transaction(**overrides):
    row = {
        "id": 1,
        "member": "Hener",
        "category": "Casa",
        "description": "Despesa vinculada",
        "type": "Débito/Pix",
        "account_card": "Conta Hener",
        "amount": 100,
        "total_amount": 100,
        "installment_count": 1,
        "start_month": "2026-10-01",
        "end_month": "2026-10-01",
        "destination_account": "",
        "linked_pot": "Emergência",
        "card_flag": 0,
        "payment_bank": "Conta Hener",
        "transaction_date": "2026-10-05",
        "date_precision": "exact",
        "archived": False,
    }
    row.update(overrides)
    return row


def _reserve(**overrides):
    values = {
        "id": 90, "member": "Hener", "type": "Reserva", "description": "Emergência",
        "category": "", "amount": 500, "total_amount": 500, "installment_count": 1,
        "linked_pot": "", "start_month": "2026-10-01", "end_month": "2026-12-01",
        "transaction_date": "2026-10-01",
    }
    values.update(overrides)
    return _transaction(**values)


def test_cycle_curve_matches_engine_total_and_uses_competence_day():
    from app.services.finance_core import pot_monthly_status

    transactions = [
        _reserve(),
        _transaction(id=1, amount=100, transaction_date="2026-10-05"),
        _transaction(
            id=2, amount=200, total_amount=600, type="Parcelado", installment_count=3,
            account_card="Visa", card_flag=1, start_month="2026-11-01",
            end_month="2027-01-01", transaction_date="2026-10-20",
        ),
        _transaction(
            id=3, amount=25, type="Crédito à vista", account_card="Visa", card_flag=1,
            start_month="2026-11-01", end_month="2026-11-01", transaction_date="2026-11-20",
        ),
    ]
    engine = pot_monthly_status(transactions, "2026-10", "Família")
    result = daily_reserve_summary(transactions, "2026-10", "Família", 500)

    assert engine["totals"]["used"] == 725
    assert result["used"] == 725
    assert result["detail_used"] == 725
    assert result["integrity_ok"] is True
    assert result["reserved_total"] == 500
    assert result["percent"] == 145
    assert len(result["rows"]) == 31
    assert result["rows"][4]["projected"] == 100
    assert result["rows"][18]["projected"] == 100
    assert result["rows"][19]["projected"] == 725
    assert result["rows"][-1]["projected"] == result["used"]
    assert result["rows"][19]["tooltip_label"] == "Dia 20 · competência/ciclo 10/2026"


def test_month_uses_only_rows_assigned_to_its_budget_cycle():
    transactions = [
        _reserve(),
        _transaction(
            id=3, type="Fixo", amount=75, total_amount=75,
            start_month="2026-10-01", end_month="2026-12-01",
            transaction_date="2026-10-05", date_precision="recurrence",
        ),
    ]
    result = daily_reserve_summary(transactions, "2026-11", "Família", 500)

    assert result["used"] == 0
    assert result["integrity_ok"] is True
    assert result["rows"][4]["projected"] == 0
    assert result["rows"][-1]["projected"] == 0
    assert result["rows"][4]["tooltip_label"] == "Dia 05 · competência/ciclo 11/2026"


def test_person_filter_matches_member_ownership():
    transactions = [
        _reserve(id=90, member="Hener", amount=500),
        _reserve(id=91, member="Ju", amount=600),
        _transaction(id=1, member="Hener", amount=125),
        _transaction(id=2, member="Ju", amount=300, transaction_date="2026-10-06"),
    ]
    result = daily_reserve_summary(transactions, "2026-10", "Hener", 500)

    assert result["used"] == 125
    assert result["rows"][4]["projected"] == 125
    assert result["rows"][-1]["projected"] == 125


def test_pot_detail_sums_exactly_to_the_engine_usage_for_installments():
    from app.services.finance_core import pot_monthly_status

    transactions = [
        _reserve(id=90, amount=150, end_month="2026-10-01"),
        _transaction(
            id=1, amount=33.33, total_amount=100, type="Parcelado", installment_count=3,
            account_card="Visa", card_flag=1, start_month="2026-11-01",
            end_month="2027-01-01", transaction_date="2026-10-20",
        ),
        _transaction(id=2, amount=99, linked_pot="Outra reserva"),
    ]
    status = pot_monthly_status(transactions, "2026-10", "Família")
    details = reserve_usage_details(transactions, "2026-10", "Família")

    emergency = next(row for row in status["rows"] if row["pot"] == "Emergência")
    assert emergency["used"] == 99.99
    assert sum(row["amount_cents"] for row in details["Emergência"]) == 9999
    assert details["Emergência"][0]["installment_count"] == 3
    assert details["Emergência"][0]["unit_amount_cents"] == 3333


def test_installment_detail_rounds_after_multiplying_preserving_total_cents():
    from app.services.finance_core import pot_monthly_status

    transactions = [
        _reserve(),
        _transaction(
            id=2, amount=59.995, total_amount=239.98, type="Parcelado", installment_count=4,
            account_card="Visa", card_flag=1, start_month="2026-11-01",
            end_month="2027-02-01", transaction_date="2026-10-20",
        ),
    ]
    status = pot_monthly_status(transactions, "2026-10", "Família")
    details = reserve_usage_details(transactions, "2026-10", "Família")
    result = daily_reserve_summary(
        transactions, "2026-10", "Família", 500,
        usage_details_by_pot=details,
        active_pot_names=[row["pot"] for row in status["rows"]],
        expected_used_total=status["totals"]["used"],
    )

    assert status["totals"]["used"] == 239.98
    assert sum(row["amount_cents"] for row in details["Emergência"]) == 23998
    assert details["Emergência"][0]["unit_amount_cents"] == 6000
    assert result["integrity_ok"] is True
    assert result["rows"][-1]["projected"] == 239.98


def test_october_cycle_total_includes_two_later_expenses_without_drift():
    from app.services.finance_core import pot_monthly_status

    reserve = _reserve(amount=2980, end_month="2030-12-01")
    baseline = _transaction(id=20, amount=4014.92, transaction_date="2026-10-01")
    added = [
        _transaction(id=21, amount=31.00, transaction_date="2026-10-31"),
        _transaction(id=22, amount=15.44, transaction_date="2026-10-31"),
    ]
    transactions = [reserve, baseline, *added]
    status = pot_monthly_status(transactions, "2026-10", "Família")
    details = reserve_usage_details(transactions, "2026-10", "Família")
    result = daily_reserve_summary(
        transactions, "2026-10", "Família", status["totals"]["reserved"],
        usage_details_by_pot=details,
        active_pot_names=[row["pot"] for row in status["rows"]],
        expected_used_total=status["totals"]["used"],
    )

    assert status["totals"]["used"] == 4061.36
    assert status["totals"]["used"] - sum(item["amount"] for item in added) == 4014.92
    assert result["detail_used"] == 4061.36
    assert result["integrity_ok"] is True
    assert result["rows"][-1]["projected"] == 4061.36


def test_pot_detail_excludes_archived_transactions_and_matches_cents():
    from app.services.finance_core import pot_monthly_status

    transactions = [
        _reserve(id=90, amount=150, end_month="2026-10-01"),
        _transaction(id=11, amount=99.99, linked_pot="Emergência"),
        _transaction(id=12, amount=42.60, linked_pot="Emergência", archived=True),
    ]
    status = pot_monthly_status(transactions, "2026-10", "Família")
    details = reserve_usage_details(transactions, "2026-10", "Família")

    emergency = next(row for row in status["rows"] if row["pot"] == "Emergência")
    assert emergency["used"] == 99.99
    assert sum(row["amount_cents"] for row in details["Emergência"]) == 9999
    assert all(row["transaction_id"] != 12 for row in details["Emergência"])


def test_inconsistent_detail_never_produces_a_divergent_curve():
    transactions = [_reserve(), _transaction(id=1, amount=100)]
    result = daily_reserve_summary(
        transactions,
        "2026-10",
        "Família",
        500,
        usage_details_by_pot={"Emergência": [{"date": "2026-10-05", "amount_cents": 9999}]},
        active_pot_names=["Emergência"],
        expected_used_total=100,
    )

    assert result["used"] == 100
    assert result["detail_used"] == 99.99
    assert result["integrity_ok"] is False
    assert result["rows"] == []

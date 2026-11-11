from app.services.charts import build_category_chart, build_waterfall_chart
from app.services.finance_core import monthly_details
from app.services.visual_analytics import category_comparison, waterfall_steps


def _tx(id, kind, amount, category, description, *, start="2041-02-01", end="2041-02-01", card=0, linked="", member="Hener", account="Conta Hener"):
    return {
        "id": id, "member": member, "category": category, "description": description,
        "type": kind, "account_card": account, "amount": amount, "total_amount": amount,
        "installment_count": 1, "start_month": start, "end_month": end,
        "destination_account": "", "linked_pot": linked, "card_flag": card,
        "payment_bank": account, "transaction_date": start[:7] + "-10", "date_precision": "exact", "archived": False,
    }


def test_waterfall_reconciles_to_engine_ending_balance_beyond_2030():
    rows = [
        _tx(1, "Receita", 2345, "Salário", "Salário Hener"),
        _tx(2, "Receita", 300, "Outras receitas", "Reembolso"),
        _tx(3, "Fixo", 1200, "Moradia", "Aluguel"),
        _tx(4, "Débito/Pix", 250, "Alimentação", "Mercado"),
        _tx(5, "Débito/Pix", 100, "Transporte", "Combustível", linked="Gasolina"),
        _tx(6, "Crédito à vista", 400, "Lazer", "Cinema", card=1, account="Cartão Hener"),
        _tx(7, "Reserva", 200, "Reserva", "Gasolina"),
        _tx(8, "Caixinha", 150, "Curto Prazo", "Aporte"),
    ]
    detail = monthly_details(rows, "2041-02-01", starting_balance=2500)
    steps = waterfall_steps(detail, rows, [])
    chart = build_waterfall_chart(steps, "wf-2041", "Cascata fevereiro/2041")
    assert chart is not None
    final = round(sum(row["value"] for row in steps[1:-1]) + steps[0]["value"], 2)
    assert final == round(detail["ending_balance"], 2)
    assert chart["reconciled"] is True
    assert chart["bars"][-1]["total"] is True
    assert chart["bars"][-1]["end"] == round(detail["ending_balance"], 2)
    assert [row["label"] for row in steps[:4]] == ["Saldo do mês anterior", "Salário", "Outras receitas", "Fatura de cartão"]
    assert not any(row["label"].startswith("Transferências") for row in steps)


def test_category_chart_separates_planned_confirmed_and_previous_month():
    txs = [
        _tx(1, "Débito/Pix", 120, "Alimentação", "Mercado", start="2041-01-01", end="2041-01-01"),
        _tx(2, "Débito/Pix", 150, "Alimentação", "Mercado", start="2041-02-01", end="2041-02-01"),
        _tx(3, "Crédito à vista", 300, "Lazer", "Cinema", start="2041-02-01", end="2041-02-01", card=1, account="Cartão Hener"),
    ]
    cash_items = [
        {"item_key": "2041-02:tx:2", "transaction_id": 2, "type": "Débito/Pix", "amount": 150, "signed_amount": -150, "paid": True},
    ]
    previous_cash_items = [
        {"item_key": "2041-01:tx:1", "transaction_id": 1, "type": "Débito/Pix", "amount": 120, "signed_amount": -120, "paid": True},
    ]
    rows = category_comparison(txs, "2041-02-01", "Família", cash_items, previous_cash_items)
    food = next(row for row in rows if row["category"] == "Alimentação")
    leisure = next(row for row in rows if row["category"] == "Lazer")
    assert food == {"category": "Alimentação", "planned": 150.0, "confirmed": 150.0, "previous": 120.0, "change": 30.0, "change_percent": 25.0}
    assert leisure["confirmed"] == 0
    assert leisure["planned"] == 300
    chart = build_category_chart(rows)
    assert chart is not None
    assert chart["aria_label"]
    assert any(bar["series"] == "Confirmado no app" for bar in chart["bars"])
    assert all("R$" in bar["title"] for bar in chart["bars"])
    mobile_food = next(row for row in chart["mobile_categories"] if row["category"] == "Alimentação")
    assert [series["label"] for series in mobile_food["series"]] == ["Planejado", "Confirmado", "Mês anterior"]
    assert [series["value"] for series in mobile_food["series"]] == [150.0, 150.0, 120.0]


def test_waterfall_geometry_distinguishes_confirmed_from_forecast():
    steps = [
        {"label": "Saldo inicial", "value": 1000, "kind": "total", "confirmed_value": 0},
        {"label": "Salário", "value": 500, "kind": "income", "confirmed_value": 200, "status": "Parcial"},
        {"label": "Mercado", "value": -100, "kind": "expense_category", "confirmed_value": -30, "status": "Parcial"},
        {"label": "Saldo final", "value": 1400, "kind": "total", "confirmed_value": 0},
    ]
    chart = build_waterfall_chart(steps, "wf-partial", "Cascata com baixas parciais")
    assert chart is not None and chart["reconciled"] is True
    assert len(chart["connectors"]) == len(chart["bars"]) - 1
    for bar in chart["bars"][1:3]:
        assert bar["confirmed_height"] > 0
        assert bar["confirmed_height"] < bar["height"]
        assert bar["forecast_opacity"] == 0.28
        assert "Confirmado no app:" in bar["title"]

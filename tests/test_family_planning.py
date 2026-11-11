from __future__ import annotations

import unittest

from app.services.family_planning import card_calendar, goal_progress, installment_number_for_month, simulate_family


BASE_TX = {
    "id": 1,
    "member": "Família",
    "category": "Emergência",
    "description": "Reserva mensal",
    "type": "Caixinha",
    "account_card": "Conta",
    "amount": 100,
    "total_amount": 100,
    "installment_count": 1,
    "start_month": "2026-01-01",
    "end_month": "2026-03-01",
    "destination_account": "",
    "linked_pot": "",
    "card_flag": 0,
    "payment_bank": "Conta",
    "transaction_date": "2026-01-01",
    "date_precision": "recurrence",
    "archived": False,
}


class FamilyPlanningServiceTests(unittest.TestCase):
    def test_card_calendar_uses_supplied_horizon_and_paid_invoice_key(self):
        tx = dict(BASE_TX, id=10, category="Compra", description="Notebook", type="Parcelado",
                  account_card="Cartão A", amount=200, total_amount=400, installment_count=2,
                  start_month="2026-02-01", end_month="2026-03-01", card_flag=1,
                  payment_bank="Conta")
        calendar = card_calendar(
            [tx], [{"name": "Cartão A", "bank": "Banco", "limit_amount": 1000}],
            ["2026-02-01", "2026-03-01"],
            statuses_by_month={"2026-02": {"2026-02:invoice:Cartão A": {"paid": True}}},
        )
        self.assertEqual(len(calendar), 1)
        self.assertEqual([row["month_key"] for row in calendar[0]["months"]], ["2026-02", "2026-03"])
        self.assertTrue(calendar[0]["months"][0]["paid"])
        self.assertEqual(calendar[0]["months"][0]["card_by_name"], {"Cartão A": 200.0})
        self.assertEqual(len(calendar[0]["months"][0]["purchases"]), 1)

    def test_card_calendar_numbers_installments_from_first_bill_through_last(self):
        tx = dict(BASE_TX, id=11, category="Casa", description="Geladeira", type="Parcelado",
                  account_card="Cartão A", amount=100, total_amount=300, installment_count=3,
                  start_month="2026-02-01", end_month="2026-04-01", card_flag=1,
                  payment_bank="Conta")
        calendar = card_calendar(
            [tx], [{"name": "Cartão A", "bank": "Banco", "limit_amount": 1000}],
            ["2026-02-01", "2026-03-01", "2026-04-01", "2026-05-01"],
        )
        rows = calendar[0]["months"]
        self.assertEqual([row["purchases"][0]["installment_number"] for row in rows[:3]], [1, 2, 3])
        self.assertEqual([row["purchases"][0]["installment_count"] for row in rows[:3]], [3, 3, 3])
        self.assertEqual([row["invoice"] for row in rows[:3]], [100.0, 100.0, 100.0])
        self.assertEqual(rows[3]["purchases"], [])
        self.assertIsNone(installment_number_for_month(dict(tx, type="Crédito à vista"), "2026-03-01"))

    def test_fractional_cent_card_lines_sum_to_the_monthly_invoice_total(self):
        first = dict(
            BASE_TX, id=70, category="Casa", description="Compra A", type="Parcelado",
            account_card="Cartão A", amount=59.995, total_amount=239.98, installment_count=4,
            start_month="2026-11-01", end_month="2027-02-01", card_flag=1,
        )
        second = dict(
            BASE_TX, id=71, category="Eletrônicos", description="Compra B", type="Parcelado",
            account_card="Cartão A", amount=53.625, total_amount=429.00, installment_count=8,
            start_month="2026-11-01", end_month="2027-06-01", card_flag=1,
        )

        calendar = card_calendar(
            [first, second], [{"name": "Cartão A", "bank": "Banco", "limit_amount": 1000}],
            ["2026-11-01"],
        )
        row = calendar[0]["months"][0]

        self.assertEqual(row["invoice"], 113.62)
        self.assertEqual(sum(item["amount"] for item in row["purchases"]), 113.62)
        self.assertEqual(row["purchases"][0]["amount"], 60.00)

    def test_simulation_signs_cash_and_stops_after_duration(self):
        income = dict(BASE_TX, id=20, category="Renda", description="Salário", type="Receita",
                      amount=1000, total_amount=1000, start_month="2026-01-01", end_month="2026-03-01")
        result = simulate_family(
            [income], [{"name": "Conta", "owner": "Família", "initial_balance": 0}],
            {"base_month": "2026-01-01", "minimum_balance": 0}, {}, "Família",
            "income", 100, "2026-02-01", 1, "2026-04-01",
        )
        self.assertEqual(result["motor_monthly_amount"], -100.0)
        self.assertEqual(result["compare"][1]["scenario_effect"], 100.0)
        self.assertEqual(result["compare"][2]["scenario_effect"], 0.0)
        self.assertEqual(result["compare"][0]["base_ae"], result["compare"][0]["alternative_ae"])
        self.assertIsNotNone(result["chart"])

    def test_savings_overlay_accumulates_without_mutating_transactions(self):
        tx = dict(BASE_TX)
        original = dict(tx)
        result = simulate_family(
            [tx], [{"name": "Conta", "owner": "Família", "initial_balance": 0}],
            {"base_month": "2026-01-01", "minimum_balance": 0}, {}, "Família",
            "savings", 50, "2026-02-01", 2, "2026-03-01",
        )
        self.assertEqual(tx, original)
        self.assertEqual(result["alternative"][1]["contribution_extra"], 50.0)
        self.assertEqual(result["alternative"][1]["savings_accumulated"], 250.0)
        self.assertEqual(result["alternative"][0]["savings_accumulated"], 100.0)

    def test_goal_uses_only_paid_matching_category_and_opening_balance(self):
        first = dict(BASE_TX, id=30, start_month="2025-12-01", end_month="2025-12-01", amount=500)
        current = dict(BASE_TX, id=31, start_month="2026-02-01", end_month="2026-02-01", amount=200)
        wrong_category = dict(current, id=32, category="Lazer", amount=900)
        progress = goal_progress(
            {"id": "g1", "name": "Emergência", "category": "Emergência", "target_amount": 1000,
             "target_month": "2026-04-01", "opening_amount": 400, "opening_month": "2026-01-01"},
            [first, current, wrong_category],
            {"2026-02": {"2026-02:tx:31": {"paid": True}}},
            [{"month": "2026-03-01", "savings_by_category": {"Emergência": 200}, "free_after": 300},
             {"month": "2026-04-01", "savings_by_category": {"Emergência": 200}, "free_after": 300}],
            "2026-02-15",
        )
        self.assertEqual(progress["confirmed"], 200.0)
        self.assertEqual(progress["known_balance"], 600.0)
        self.assertEqual(progress["remaining"], 400.0)
        self.assertEqual(progress["planned_end"], 1000.0)
        self.assertTrue(progress["on_track"])
        self.assertNotIn(900.0, progress["future_by_month"].values())


if __name__ == "__main__":
    unittest.main()

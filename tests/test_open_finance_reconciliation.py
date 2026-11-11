from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from app.services.open_finance_matching import match_card_purchase
from app.services.open_finance_reconciliation import (
    build_invoice_groups,
    card_cycle_bounds,
    credit_used,
    external_card_closing_day,
    latest_provider_closing_date,
    project_card_cycle,
)


class OpenFinanceReconciliationTests(unittest.TestCase):
    def test_invoice_groups_show_only_context_month_and_next_invoice(self):
        account = SimpleNamespace(
            account_id="itau-card", name="Cartão Itaú", owner="Hener", institution="Itaú",
        )
        bills = [
            SimpleNamespace(account_id="itau-card", currency_code="BRL", due_date=month,
                            total_amount=Decimal(amount), payment_amount_observed=Decimal("0.00"),
                            closing_date=month)
            for month, amount in [
                ("2026-09-12", "100.00"), ("2026-10-12", "200.00"),
                ("2026-11-12", "300.00"), ("2026-12-12", "400.00"),
            ]
        ]
        groups = build_invoice_groups(
            [account], bills, {"itau-card": {"kind": "CREDIT", "name": "Itaú Master"}},
            [{"name": "Itaú Master", "bank": "Itaú"}], [], {}, "2026-08-01", "2026-10-01",
        )

        visible_months = {row["month_key"] for group in groups for row in group["rows"]}
        self.assertEqual(visible_months, {"2026-10", "2026-11"})
        self.assertEqual(next(group for group in groups if group["key"] == "past")["rows"], [])
        self.assertEqual(next(group for group in groups if group["key"] == "unknown")["rows"], [])

    def test_cycle_boundaries_honor_close_day_and_years_after_2030(self):
        start, end, closing = card_cycle_bounds("2031-11-01", 3)
        self.assertEqual((start, end, closing), (date(2031, 10, 4), date(2031, 11, 3), 3))

        start, end, _ = card_cycle_bounds("2032-03-01", 31)
        self.assertEqual(start, date(2032, 3, 1))
        self.assertEqual(end, date(2032, 3, 31))

    def test_november_projection_sums_debits_and_keeps_credits_separate(self):
        rows = [
            SimpleNamespace(account_id="c6-card", occurred_on="2026-10-03", transaction_type="DEBIT",
                            status="PENDING", amount=Decimal("99.00"), currency_code="BRL"),
            SimpleNamespace(account_id="c6-card", occurred_on="2026-10-04", transaction_type="DEBIT",
                            status="POSTED", amount=Decimal("300.00"), currency_code="BRL",
                            installment_number=1, total_installments=10),
            SimpleNamespace(account_id="c6-card", occurred_on="2026-10-20", transaction_type="DEBIT",
                            status="PENDING", amount=Decimal("351.68"), currency_code="BRL"),
            SimpleNamespace(account_id="c6-card", occurred_on="2026-10-10", transaction_type="CREDIT",
                            status="POSTED", amount=Decimal("-1048.64"), currency_code="BRL"),
            SimpleNamespace(account_id="c6-card", occurred_on="2026-10-10", transaction_type="CREDIT",
                            status="PENDING", amount=Decimal("-1048.64"), currency_code="BRL"),
            SimpleNamespace(account_id="other-card", occurred_on="2026-10-15", transaction_type="DEBIT",
                            status="POSTED", amount=Decimal("800.00"), currency_code="BRL"),
            SimpleNamespace(account_id="c6-card", occurred_on="2026-11-04", transaction_type="DEBIT",
                            status="POSTED", amount=Decimal("88.00"), currency_code="BRL"),
        ]

        result = project_card_cycle(rows, "c6-card", "2026-11-01", 3)

        self.assertEqual(result["cycle_start"], date(2026, 10, 4))
        self.assertEqual(result["cycle_end"], date(2026, 11, 3))
        self.assertEqual(result["posted_charges"], Decimal("300.00"))
        self.assertEqual(result["pending_charges"], Decimal("351.68"))
        self.assertEqual(result["projected_total"], Decimal("651.68"))
        self.assertEqual(result["credit_count"], 2)
        self.assertEqual(result["credit_count_posted"], 1)
        self.assertEqual(result["credit_count_pending"], 1)
        self.assertEqual(result["credit_adjustments_posted"], Decimal("-1048.64"))
        self.assertEqual(result["credit_adjustments_pending"], Decimal("-1048.64"))
        self.assertEqual(result["installment_count"], 1)
        self.assertTrue(result["review_required"])

    def test_limit_used_is_not_the_monthly_invoice(self):
        account = SimpleNamespace(
            balance=Decimal("2540.95"), credit_limit=Decimal("8860.00"),
            available_credit_limit=Decimal("6319.05"),
        )
        self.assertEqual(credit_used(account), Decimal("2540.95"))
        self.assertNotEqual(credit_used(account), Decimal("651.68"))

    def test_unlinked_account_does_not_use_suggested_cards_closing_day(self):
        cards = [{"name": "C6 Cartão", "closing_day": 18}]
        settings = {"card_closing_days": {"C6 Cartão": 18}}

        self.assertEqual(external_card_closing_day("c6-card", {}, cards, settings), (3, "default"))
        self.assertEqual(
            external_card_closing_day(
                "c6-card", {"c6-card": {"kind": "CREDIT", "name": "C6 Cartão"}}, cards, settings,
            ),
            (18, "configured"),
        )

    def test_latest_closed_provider_statement_sets_next_cycle_close_day(self):
        bills = [
            SimpleNamespace(account_id="itau-card", currency_code="BRL", closing_date="2026-09-02"),
            SimpleNamespace(account_id="itau-card", currency_code="BRL", closing_date="2026-10-02"),
            SimpleNamespace(account_id="itau-card", currency_code="BRL", closing_date="2026-11-02"),
            SimpleNamespace(account_id="other-card", currency_code="BRL", closing_date="2026-10-04"),
            SimpleNamespace(account_id="itau-card", currency_code="USD", closing_date="2026-10-01"),
        ]
        closing = latest_provider_closing_date(bills, "itau-card", "2026-11-01")

        self.assertEqual(closing, date(2026, 10, 2))
        self.assertEqual(
            external_card_closing_day(
                "itau-card", {"itau-card": {"kind": "CREDIT", "name": "Itaú Master"}},
                [{"name": "Itaú Master", "closing_day": 3}],
                {"card_closing_days": {"Itaú Master": 3}}, closing,
            ),
            (2, "provider_bill"),
        )

    def test_projected_invoice_detail_contains_only_brl_debits(self):
        rows = [
            SimpleNamespace(account_id="itau-card", occurred_on="2026-10-03", transaction_type="DEBIT",
                            status="PENDING", amount=Decimal("25.00"), currency_code="BRL",
                            description="Compra aberta", category="Casa", installment_number=1,
                            total_installments=10),
            SimpleNamespace(account_id="itau-card", occurred_on="2026-10-04", transaction_type="CREDIT",
                            status="POSTED", amount=Decimal("250.00"), currency_code="BRL",
                            description="Pagamento fatura"),
            SimpleNamespace(account_id="itau-card", occurred_on="2026-10-04", transaction_type="DEBIT",
                            status="PENDING", amount=Decimal("10.00"), currency_code="USD",
                            description="Compra estrangeira"),
        ]

        result = project_card_cycle(rows, "itau-card", "2026-11-01", 2)

        self.assertEqual(result["projected_total"], Decimal("25.00"))
        self.assertEqual(len(result["purchase_rows"]), 1)
        self.assertEqual(result["purchase_rows"][0]["description"], "Compra aberta")
        self.assertEqual(result["purchase_rows"][0]["installment_number"], 1)

    def test_foreign_currency_bill_stays_visible_and_is_not_compared_to_brl(self):
        account = SimpleNamespace(
            account_id="card-usd", name="Cartão internacional", owner="Hener", institution="Banco",
        )
        bill = SimpleNamespace(
            account_id="card-usd", currency_code="USD", due_date="2026-10-12",
            total_amount=Decimal("123.45"), payment_amount_observed=Decimal("10.00"),
            closing_date="2026-10-03",
        )
        groups = build_invoice_groups(
            [account], [bill], {"card-usd": {"kind": "CREDIT", "name": "Cartão internacional"}},
            [{"name": "Cartão internacional", "bank": "Banco"}], [], {},
            "2026-08-01", "2026-10-01",
        )

        row = next(
            row for group in groups for row in group["rows"]
            if row["month_key"] == "2026-10" and row["currency"] == "USD"
        )
        self.assertEqual(row["display_amount"], Decimal("123.45"))
        self.assertEqual(row["display_amount_label"], "Fatura Pluggy · USD")
        self.assertIsNone(row["finfam_amount"])
        self.assertIsNone(row["difference"])
        self.assertFalse(row["comparison_available"])

    def test_foreign_transaction_cannot_match_equal_brl_amount_and_date(self):
        account = SimpleNamespace(account_type="CREDIT")
        local_rows = [{
            "id": 7, "archived": False, "type": "Crédito à vista", "card_flag": 1,
            "account_card": "C6 Cartão", "transaction_date": "2026-10-27",
            "start_month": "2026-11-01", "end_month": "2026-11-01",
            "installment_count": 1, "amount": Decimal("77.77"), "description": "Gasto local em reais",
        }]
        mapping = {"kind": "CREDIT", "name": "C6 Cartão"}
        external = SimpleNamespace(
            currency_code="USD", occurred_on="2026-10-27", status="POSTED",
            transaction_type="DEBIT", installment_number=1, total_installments=1,
            amount=Decimal("77.77"),
        )
        cards = [{"name": "C6 Cartão"}]
        settings = {"card_closing_days": {"C6 Cartão": 3}}

        foreign = match_card_purchase(
            external, account, mapping, local_rows, cards, settings, today=date(2026, 10, 29),
        )
        self.assertEqual(foreign["reason"], "currency")

        external.currency_code = "BRL"
        candidate = match_card_purchase(
            external, account, mapping, local_rows, cards, settings, today=date(2026, 10, 29),
        )
        self.assertEqual(candidate["state"], "candidate")
        self.assertEqual(candidate["candidate"]["transaction_id"], 7)
        self.assertEqual(candidate["candidate"]["invoice_month"], "2026-11")
        self.assertFalse(candidate["can_confirm"])

    def test_projected_cycle_compares_by_due_month_and_preserves_one_cent(self):
        account = SimpleNamespace(
            account_id="c6-card", name="C6 PAR2", owner="Hener", institution="C6 Bank",
        )
        cycle = {
            "projected_total": Decimal("651.68"),
            "posted_charges": Decimal("0.00"), "pending_charges": Decimal("651.68"),
            "posted_count": 0, "pending_count": 8,
            "credit_adjustments_posted": Decimal("0.00"),
            "credit_adjustments_pending": Decimal("-1048.64"), "credit_count": 1,
            "unclassified_count": 0, "installment_count": 4,
            "cycle_start": date(2026, 10, 4), "cycle_end": date(2026, 11, 3),
            "closing_day": 3, "review_required": True,
        }
        card = {"name": "C6 Cartão", "bank": "C6", "closing_day": 3}
        with patch("app.services.open_finance_reconciliation.monthly_details",
                   return_value={"card_by_name": {"C6 Cartão": Decimal("651.69")}}):
            groups = build_invoice_groups(
                [account], [], {"c6-card": {"kind": "CREDIT", "name": "C6 Cartão"}},
                [card], [], {}, "2026-08-01", "2026-10-01",
                projected_cycles={("c6-card", "2026-11"): cycle},
            )

        row = next(row for group in groups for row in group["rows"] if row["month_key"] == "2026-11")
        self.assertEqual(row["display_amount_label"], "Projeção pelo ciclo de transações")
        self.assertEqual(row["provider_total"], None)
        self.assertEqual(row["finfam_amount"], Decimal("651.69"))
        self.assertEqual(row["display_amount"], Decimal("651.68"))
        self.assertEqual(row["difference"], Decimal("-0.01"))
        self.assertEqual(row["comparison_source"], "transactions")

    def test_provider_bill_stays_primary_and_cycle_details_remain_available(self):
        account = SimpleNamespace(
            account_id="itau-card", name="Cartão Itaú", owner="Hener", institution="Itaú",
        )
        bill = SimpleNamespace(
            account_id="itau-card", currency_code="BRL", due_date="2026-11-12",
            total_amount=Decimal("4981.39"), payment_amount_observed=Decimal("0.00"),
            closing_date="2026-10-02",
        )
        cycle = {
            "projected_total": Decimal("5231.39"),
            "posted_charges": Decimal("1209.58"), "pending_charges": Decimal("4021.81"),
            "posted_count": 5, "pending_count": 40,
            "purchase_rows": [{"description": "Compra do ciclo", "amount": Decimal("250.00")}],
        }
        groups = build_invoice_groups(
            [account], [bill], {"itau-card": {"kind": "CREDIT", "name": "Itaú Master"}},
            [{"name": "Itaú Master", "bank": "Itaú"}], [], {},
            "2026-08-01", "2026-10-01",
            projected_cycles={("itau-card", "2026-11"): cycle},
        )

        row = next(row for group in groups for row in group["rows"] if row["month_key"] == "2026-11")
        self.assertEqual(row["display_amount"], Decimal("4981.39"))
        self.assertEqual(row["display_amount_label"], "Fatura Pluggy")
        self.assertEqual(row["projected_total"], Decimal("5231.39"))
        self.assertEqual(row["purchase_rows"][0]["description"], "Compra do ciclo")
        self.assertEqual(row["comparison_source"], "bill")

    def test_unlinked_card_has_no_financial_comparison(self):
        account = SimpleNamespace(
            account_id="c6-card", name="C6 PAR2", owner="Hener", institution="C6 Bank",
        )
        cycle = {"projected_total": Decimal("651.68"), "posted_charges": Decimal("0.00"),
                 "pending_charges": Decimal("651.68"), "cycle_start": date(2026, 10, 4),
                 "cycle_end": date(2026, 11, 3), "closing_day": 3}
        with patch("app.services.open_finance_reconciliation.monthly_details") as details:
            groups = build_invoice_groups(
                [account], [], {}, [{"name": "C6 Cartão", "bank": "C6"}], [], {},
                "2026-08-01", "2026-10-01",
                projected_cycles={("c6-card", "2026-11"): cycle},
            )
        row = next(row for group in groups for row in group["rows"] if row["month_key"] == "2026-11")
        self.assertEqual(row["display_amount"], Decimal("651.68"))
        self.assertIsNone(row["finfam_amount"])
        self.assertIsNone(row["difference"])
        details.assert_not_called()


if __name__ == "__main__":
    unittest.main()

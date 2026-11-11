from __future__ import annotations

import unittest
from datetime import date
from app.services.daily_review import (
    annotate_review_actions,
    build_daily_review_context,
    build_return_url,
    filter_review_items,
    monthly_cash_items,
    order_review_items,
    review_state,
    review_summary,
    safe_batch_selection,
    status_change_block_reason,
    validate_item_confirmation,
    validate_paid_on,
)


class DailyReviewTests(unittest.TestCase):
    """Pure service tests using only local synthetic rows."""

    def test_states_distinguish_confirmed_pending_and_predicted(self):
        self.assertEqual(review_state({"paid": True, "date_precision": "recurrence"}), "confirmed")
        self.assertEqual(review_state({"paid": False, "date_precision": "exact"}), "pending")
        self.assertEqual(review_state({"paid": False, "date_precision": "recurrence"}), "predicted")

    def test_summary_is_not_changed_by_search_or_status_filter(self):
        rows = [
            {"item_key": "2026-10:tx:1", "description": "Salário", "account": "Conta", "member": "Pessoa A", "type": "Receita", "amount": 1000, "paid": True, "date_precision": "exact"},
            {"item_key": "2026-10:tx:2", "description": "Aluguel", "account": "Conta", "member": "Pessoa A", "type": "Fixo", "amount": 500, "paid": False, "date_precision": "exact"},
            {"item_key": "2026-10:tx:3", "description": "Reserva", "account": "Conta", "member": "Pessoa A", "type": "Reserva", "amount": 200, "paid": False, "date_precision": "recurrence"},
        ]
        summary = review_summary(rows)
        visible = filter_review_items(rows, query="aluguel", state="pending")
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["confirmed"], 1)
        self.assertEqual(summary["pending"], 1)
        self.assertEqual(summary["predicted"], 1)
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0]["description"], "Aluguel")
        self.assertEqual(summary["total"], 3)

    def test_context_contract_uses_current_month_cash_items(self):
        snapshot = (
            [
                {"id": 1, "member": "Pessoa A", "category": "Casa", "description": "Conta exata", "type": "Fixo", "account_card": "Conta", "amount": 50, "total_amount": 50, "installment_count": 1, "start_month": "2026-10-01", "end_month": "2026-10-01", "destination_account": "", "linked_pot": "", "card_flag": 0, "payment_bank": "Conta", "transaction_date": "2026-10-10", "date_precision": "exact", "archived": False},
            ],
            [{"name": "Conta", "owner": "Pessoa A", "initial_balance": 0}],
            [],
            {},
            {},
            {},
        )
        context = build_daily_review_context(snapshot, "2026-10", "Pessoa A", query="Conta")
        self.assertEqual(len(context["review_all_items"]), 1)
        self.assertEqual(context["review_summary"]["pending"], 1)
        self.assertEqual(context["pending_items"][0]["description"], "Conta exata")
        self.assertEqual(context["review_items"][0]["review_group_label"], "Pendentes · Débito/Pix e contas fixas")

    def test_review_items_are_grouped_faturas_transfers_bank_expenses_then_other(self):
        rows = [
            {"type": "Débito/Pix", "description": "Mercado"},
            {"type": "Transf. Env.", "description": "Entre contas"},
            {"type": "Fatura cartão", "description": "Fatura C6"},
            {"type": "Fixo", "description": "Luz"},
            {"type": "Receita", "description": "Salário"},
            {"type": "Fatura cartão", "description": "Fatura Itaú"},
        ]
        ordered = order_review_items(rows)
        self.assertEqual(
            [item["description"] for item in ordered],
            ["Fatura C6", "Fatura Itaú", "Entre contas", "Mercado", "Luz", "Salário"],
        )
        self.assertEqual([item["review_group_count"] for item in ordered[:2]], [2, 2])
        self.assertEqual(ordered[2]["review_group_label"], "Transferências")

    def test_pending_items_precede_confirmed_and_predicted_rows(self):
        rows = [
            {"type": "Fatura cartão", "description": "Fatura prevista", "paid": False, "date_precision": "recurrence"},
            {"type": "Débito/Pix", "description": "Pix pendente", "paid": False, "date_precision": "exact"},
            {"type": "Transf. Env.", "description": "Transferência pendente", "paid": False, "date_precision": "exact"},
            {"type": "Fatura cartão", "description": "Fatura pendente", "paid": False, "date_precision": "exact"},
            {"type": "Fatura cartão", "description": "Fatura confirmada", "paid": True, "date_precision": "exact"},
            {"type": "Fixo", "description": "Fixo previsto", "paid": False, "date_precision": "recurrence"},
        ]

        ordered = order_review_items(rows)

        self.assertEqual(
            [item["description"] for item in ordered],
            ["Fatura pendente", "Transferência pendente", "Pix pendente",
             "Fatura prevista", "Fatura confirmada", "Fixo previsto"],
        )
        self.assertEqual(ordered[0]["review_group_label"], "Pendentes · Faturas de cartão")
        self.assertEqual(ordered[1]["review_group_label"], "Pendentes · Transferências")
        self.assertEqual(ordered[3]["review_group_label"], "Faturas de cartão")

    def test_batch_selection_rejects_predicted_rows_but_allows_invoice(self):
        rows = [
            {"item_key": "exact", "type": "Fixo", "date_precision": "exact", "paid": False},
            {"item_key": "forecast", "type": "Fixo", "date_precision": "recurrence", "paid": False},
            {"item_key": "invoice", "type": "Fatura cartão", "date_precision": "recurrence", "paid": False},
        ]
        selection = safe_batch_selection(rows, ["exact", "forecast", "invoice"], action="confirm")
        self.assertEqual([item["item_key"] for item in selection["selected"]], ["exact", "forecast", "invoice"])
        self.assertEqual([item["item_key"] for item in selection["unsafe"]], ["forecast"])
        self.assertEqual(selection["missing"], [])
        self.assertEqual(safe_batch_selection(rows, ["forecast"], action="undo")["unsafe"], [])

    def test_only_iso_exact_dates_are_accepted_and_return_path_is_safe(self):
        self.assertEqual(validate_paid_on("2026-10-20"), "2026-10-20")
        self.assertIsNone(validate_paid_on(""))
        with self.assertRaises(ValueError):
            validate_paid_on("2026-10")
        with self.assertRaises(ValueError):
            validate_paid_on("20/10/2026")
        location = build_return_url("/today", month="2026-10-01", person="Família", horizon="all", q="café", state="pending")
        self.assertTrue(location.startswith("/today?"))
        self.assertIn("month=2026-10", location)
        self.assertIn("person=Fam%C3%ADlia", location)
        with self.assertRaises(ValueError):
            build_return_url("https://evil.example", month="2026-10")

    def test_status_writes_are_blocked_for_future_and_closed_months(self):
        today = date(2026, 10, 21)
        self.assertIsNone(status_change_block_reason("2026-09", False, today=today))
        self.assertIsNone(status_change_block_reason("2026-10", False, today=today))
        self.assertIn("futura", status_change_block_reason("2026-11", False, today=today))
        self.assertIn("fechada", status_change_block_reason("2026-10", True, today=today))

    def test_future_scheduled_item_needs_actual_paid_date(self):
        today = date(2026, 10, 21)
        item = {"transaction_date": "2026-10-22", "paid": False, "date_precision": "exact", "type": "Fixo"}
        with self.assertRaisesRegex(ValueError, "data prevista ainda não chegou"):
            validate_item_confirmation(item, None, today=today)
        validate_item_confirmation(item, "2026-10-21", today=today)
        with self.assertRaisesRegex(ValueError, "não pode estar no futuro"):
            validate_paid_on("2026-10-22", today=today)
        context = annotate_review_actions({"review_items": [item]}, "2026-10", False, today=today)
        self.assertTrue(context["review_items"][0]["requires_paid_on"])
        self.assertFalse(context["review_items"][0]["batch_confirmable"])


if __name__ == "__main__":
    unittest.main()

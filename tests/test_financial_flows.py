from __future__ import annotations

import unittest

from app.services.finance_core import (
    FAMILY, apply_payment_statuses, cashflow_items, cashflow_summary,
    opening_account_balances,
)


class CashflowClassificationTests(unittest.TestCase):
    def test_bank_assignment_matches_cashflow_and_opening_balances(self):
        transactions = [
            {
                "id": 1, "member": "Hener", "category": "Casa", "description": "Fixo C6",
                "type": "Fixo", "account_card": "C6", "amount": 30,
                "total_amount": 30, "installment_count": 1, "start_month": "2026-08-01",
                "end_month": "2030-12-01", "destination_account": "", "linked_pot": "",
                "card_flag": 0, "payment_bank": "Mercado Pago", "transaction_date": "2026-08-10",
                "date_precision": "exact", "archived": False,
            },
            {
                "id": 2, "member": "Hener", "category": "", "description": "Transferência",
                "type": "Transferência", "account_card": "Mercado Pago", "amount": 100,
                "total_amount": 100, "installment_count": 1, "start_month": "2026-10-01",
                "end_month": "2026-10-01", "destination_account": "C6", "linked_pot": "",
                "card_flag": 0, "payment_bank": "C6", "transaction_date": "2026-10-15",
                "date_precision": "exact", "archived": False,
            },
            {
                "id": 3, "member": "Hener", "category": "Lazer", "description": "Compra no C6",
                "type": "Crédito à vista", "account_card": "C6 cartão", "amount": 50,
                "total_amount": 50, "installment_count": 1, "start_month": "2026-10-01",
                "end_month": "2026-10-01", "destination_account": "", "linked_pot": "",
                "card_flag": 1, "payment_bank": "Mercado Pago", "transaction_date": "2026-10-16",
                "date_precision": "exact", "archived": False,
            },
        ]
        accounts = [
            {"name": "C6", "owner": "Hener", "initial_balance": 1000},
            {"name": "Mercado Pago", "owner": "Ju", "initial_balance": 500},
        ]
        cards = [{"name": "C6 cartão", "bank": "C6"}]

        october = cashflow_items(transactions, cards, accounts, "2026-10-01", FAMILY)
        self.assertEqual(
            {(item["type"], item["account"]): item["amount"] for item in october},
            {
                ("Fixo", "C6"): 30,
                ("Fatura cartão", "C6"): 50,
                ("Transf. Env.", "Mercado Pago"): 100,
                ("Transf. Receb.", "C6"): 100,
            },
        )

        opening = opening_account_balances(
            transactions, accounts, "2026-08-01", "2026-11-01", cards=cards,
        )
        # Ago/set/out: três saídas de R$30 no C6; a transferência é MP -> C6;
        # a compra no cartão também é liquidada pelo banco cadastrado do cartão.
        self.assertEqual(opening, {"C6": 960.0, "Mercado Pago": 400.0})

        november = cashflow_items(transactions, cards, accounts, "2026-11-01", FAMILY)
        paid = [dict(item, paid=True) for item in november]
        family = cashflow_summary(paid, accounts, opening, FAMILY, account_items=paid)
        hener = cashflow_summary(paid, accounts, opening, "Hener", account_items=paid)
        ju = cashflow_summary(paid, accounts, opening, "Ju", account_items=paid)
        self.assertEqual(family["live_total"], 1330.0)
        self.assertEqual(hener["live_total"] + ju["live_total"], family["live_total"])
        self.assertEqual(hener["account_balances"][0]["live"], 930.0)
        self.assertEqual(ju["account_balances"][0]["live"], 400.0)

        # Após o fim da recorrência em 2030, não há nova saída de R$30.
        post_2030 = opening_account_balances(
            transactions, accounts, "2026-08-01", "2031-01-01", cards=cards,
        )
        self.assertEqual(post_2030, {"C6": -540.0, "Mercado Pago": 400.0})
        self.assertEqual(cashflow_items(transactions, cards, accounts, "2031-01-01", FAMILY), [])

        # Chamadores antigos sem ``cards`` conservam o fallback histórico
        # payment_bank somente para despesas de cartão.
        legacy_opening = opening_account_balances(
            transactions, accounts, "2026-08-01", "2026-11-01",
        )
        self.assertEqual(legacy_opening, {"C6": 1010.0, "Mercado Pago": 350.0})

    def test_internal_transfer_is_not_external_income_or_expense(self):
        items = [
            {"item_key": "income", "account": "A", "type": "Receita", "amount": 1000, "signed_amount": 1000, "paid": True},
            {"item_key": "expense", "account": "A", "type": "Débito/Pix", "amount": 200, "signed_amount": -200, "paid": True},
            {"item_key": "transfer:out", "transaction_id": 7, "account": "A", "type": "Transf. Env.", "amount": 500, "signed_amount": -500, "paid": True},
            {"item_key": "transfer:in", "transaction_id": 7, "account": "B", "type": "Transf. Receb.", "amount": 500, "signed_amount": 500, "paid": True},
            {"item_key": "pot", "account": "A", "type": "Caixinha", "amount": 100, "signed_amount": -100, "paid": True},
            {"item_key": "withdraw", "account": "A", "type": "Saque Caixinha", "amount": 40, "signed_amount": 40, "paid": True},
        ]
        accounts = [{"name": "A", "owner": "Hener"}, {"name": "B", "owner": "Ju"}]
        summary = cashflow_summary(items, accounts, {"A": 1000, "B": 0}, FAMILY)
        self.assertEqual(summary["income_paid"], 1000)
        self.assertEqual(summary["expenses_paid"], 200)
        self.assertEqual(summary["internal_transfers"], 500)
        self.assertEqual(summary["transfer_net"], 0)
        self.assertEqual(summary["pot_contributions"], 100)
        self.assertEqual(summary["pot_withdrawals"], 40)
        self.assertEqual(summary["live_total"], 1740)

    def test_transfer_from_workbook_with_one_paid_side_is_synchronized(self):
        tx = {
            "id": 70, "member": "Hener", "category": "", "description": "Entre contas",
            "type": "Transferência", "account_card": "Conta A", "amount": 500,
            "total_amount": 500, "installment_count": 1, "start_month": "2026-10-01",
            "end_month": "2026-10-01", "destination_account": "Conta B", "linked_pot": "",
            "card_flag": 0, "payment_bank": "Conta A", "transaction_date": "2026-10-15",
            "date_precision": "exact", "archived": False,
        }
        accounts = [{"name": "Conta A", "owner": "Hener"}, {"name": "Conta B", "owner": "Ju"}]
        items = cashflow_items([tx], [], accounts, "2026-10-01", FAMILY)
        self.assertEqual({item["type"] for item in items}, {"Transf. Env.", "Transf. Receb."})
        legacy = [{"account": "Conta B", "type": "Transf. Receb.", "member": "Hener",
                   "description": "Entre contas", "amount": 500, "paid": True}]
        reconciled = apply_payment_statuses(items, legacy, {})
        self.assertTrue(all(item["paid"] for item in reconciled))
        summary = cashflow_summary(reconciled, accounts, {"Conta A": 1000, "Conta B": 0}, FAMILY)
        self.assertEqual(summary["transfer_net"], 0)
        self.assertEqual(summary["live_total"], 1000)

    def test_explicit_status_for_one_side_updates_the_whole_transfer_pair(self):
        items = [
            {"item_key": "2026-10:tx:9:out", "transaction_id": 9, "type": "Transf. Env.", "paid": False},
            {"item_key": "2026-10:tx:9:in", "transaction_id": 9, "type": "Transf. Receb.", "paid": False},
        ]
        explicit = {"2026-10:tx:9:out": {"paid": True, "paid_on": "2026-10-15"}}
        result = apply_payment_statuses(items, [], explicit)
        self.assertTrue(all(item["paid"] for item in result))
        self.assertEqual({item["paid_on"] for item in result}, {"2026-10-15"})

    def test_member_bank_balance_includes_shared_movements_on_owned_account(self):
        shared_paid = [{
            "item_key": "2026-10:tx:12", "account": "Conta Hener", "member": FAMILY,
            "type": "Débito/Pix", "amount": 100, "signed_amount": -100, "paid": True,
        }]
        personal_summary = cashflow_summary(
            [], [{"name": "Conta Hener", "owner": "Hener"}],
            {"Conta Hener": 1000}, "Hener", account_items=shared_paid,
        )
        self.assertEqual(personal_summary["account_balances"][0]["live"], 900)
        self.assertEqual(personal_summary["live_total"], 900)
        self.assertEqual(personal_summary["expenses_paid"], 0)


if __name__ == "__main__":
    unittest.main()

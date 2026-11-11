from __future__ import annotations

import copy
import unittest

from app.services.finance_core import cashflow_items
from app.services.member_balances import member_bank_series, member_bank_row


class MemberBankBalanceTests(unittest.TestCase):
    def setUp(self):
        self.accounts = [
            {"name": "Mercado Pago", "owner": "Hener", "initial_balance": 0},
            {"name": "Nubank", "owner": "Ju", "initial_balance": 482},
        ]
        self.cards = [
            {"name": "Nubank cartão", "bank": "Nubank", "limit_amount": 10000},
        ]
        self.settings = {
            "base_month": "2026-10-01",
            "reference_date": "2026-10-21",
            "forecast_end": "2026-11-01",
        }

    @staticmethod
    def tx(
        tx_id: int,
        member: str,
        kind: str,
        account: str,
        amount: float,
        *,
        description: str | None = None,
        destination: str = "",
        card: bool = False,
        start: str = "2026-10-01",
        end: str | None = "2026-10-01",
    ) -> dict:
        return {
            "id": tx_id,
            "member": member,
            "category": "Teste",
            "description": description or kind,
            "type": kind,
            "account_card": account,
            "amount": amount,
            "total_amount": amount,
            "installment_count": 1,
            "start_month": start,
            "end_month": end,
            "destination_account": destination,
            "linked_pot": "",
            "card_flag": 1 if card else 0,
            "payment_bank": "Nubank" if card else account,
            "transaction_date": "2026-10-15",
            "date_precision": "exact",
            "archived": False,
        }

    def series(self, transactions, *, statuses=None, legacy_rows=None, closed=None, person="Ju", **kwargs):
        return member_bank_series(
            transactions,
            self.accounts,
            self.cards,
            self.settings,
            statuses=statuses,
            legacy_rows=legacy_rows,
            closed_months=closed or {},
            person=person,
            observed_month="2026-10-01",
            **kwargs,
        )

    def confirm_all(self, transactions):
        items = cashflow_items(transactions, self.cards, self.accounts, "2026-10-01", "Família")
        return {item["item_key"]: {"paid": True, "paid_on": "2026-10-20"} for item in items}

    def test_current_ju_balance_matches_short_term_account_reconciliation(self):
        transactions = [
            self.tx(1, "Ju", "Receita", "Nubank", 6294.96, description="Receita"),
            self.tx(2, "Ju", "Transferência", "Nubank", 5700, destination="Mercado Pago", description="Envio"),
            self.tx(3, "Hener", "Transferência", "Mercado Pago", 68.80, destination="Nubank", description="Recebimento"),
            self.tx(4, "Ju", "Fixo", "Nubank", 80, description="Telefone"),
            self.tx(5, "Família", "Crédito à vista", "Nubank cartão", 616.70, card=True, description="Compra do mês"),
        ]
        before = copy.deepcopy(transactions)
        rows = self.series(transactions, statuses=self.confirm_all(transactions), closed={"2026-10": True})
        current = member_bank_row(rows, "2026-10")

        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current["opening_balance"], 482.0)
        self.assertEqual(current["confirmed_balance"], 449.06)
        self.assertEqual(current["projected_balance"], 449.06)
        self.assertEqual(current["planned_change"], -32.94)
        self.assertEqual(current["pending_count"], 0)
        self.assertTrue(current["closed"])
        self.assertEqual(transactions, before)
        self.assertEqual(current["account_balances"][0]["account"], "Nubank")
        self.assertEqual(current["account_balances"][0]["confirmed"], 449.06)

    def test_items_are_built_for_family_then_filtered_by_account_owner(self):
        accounts = [
            {"name": "Conta Hener", "owner": "Hener", "initial_balance": 0},
            {"name": "Conta Ju", "owner": "Ju", "initial_balance": 1000},
        ]
        transactions = [
            self.tx(10, "Ju", "Fixo", "Conta Hener", 200, description="Ju atribuído em conta Hener"),
            self.tx(11, "Hener", "Fixo", "Conta Ju", 50, description="Hener atribuído em conta Ju"),
        ]
        statuses = {
            item["item_key"]: {"paid": True}
            for item in cashflow_items(transactions, self.cards, accounts, "2026-10-01", "Família")
        }
        rows = member_bank_series(
            transactions, accounts, self.cards, self.settings, statuses=statuses,
            person="Ju", observed_month="2026-10-01", end="2026-10-01",
        )
        current = rows[0]
        self.assertEqual(current["opening_balance"], 1000.0)
        self.assertEqual(current["projected_balance"], 950.0)
        self.assertEqual(current["confirmed_balance"], 950.0)

    def test_confirmed_and_projected_keep_pending_amount_separate_without_a_settlement_date(self):
        transactions = [self.tx(20, "Ju", "Débito/Pix", "Nubank", 100, description="Pendente")]
        rows = self.series(transactions, closed={"2026-10": True})
        current = rows[0]

        self.assertEqual(current["confirmed_balance"], 482.0)
        self.assertEqual(current["projected_balance"], 382.0)
        self.assertEqual(current["pending_count"], 1)
        self.assertEqual(current["pending_amount"], 100.0)
        self.assertEqual(current["pending_change"], -100.0)
        self.assertEqual(current["confirmed_change"], 0.0)
        self.assertTrue(current["closed"])

    def test_legacy_paid_status_is_used_only_for_observed_month(self):
        transactions = [self.tx(30, "Ju", "Fixo", "Nubank", 100, description="Conta")]
        legacy = [{"account": "Nubank", "type": "Fixo", "member": "Ju", "description": "Conta", "amount": 100, "paid": True}]
        rows = self.series(transactions, legacy_rows=legacy)

        self.assertEqual(rows[0]["confirmed_balance"], 382.0)
        self.assertEqual(rows[0]["projected_balance"], 382.0)
        self.assertIsNone(rows[1]["confirmed_balance"])
        self.assertEqual(rows[1]["pending_count"], None)

    def test_future_opening_chains_full_planned_flow_not_confirmed_current_flow(self):
        transactions = [
            self.tx(40, "Ju", "Receita", "Nubank", 100, description="Receita recorrente", end="2026-11-01"),
            self.tx(41, "Ju", "Fixo", "Nubank", 50, description="Despesa observada"),
        ]
        rows = self.series(transactions, end="2026-11-01")

        current, future = rows
        self.assertEqual(current["confirmed_balance"], 482.0)
        self.assertEqual(current["projected_balance"], 532.0)
        self.assertEqual(future["opening_balance"], 532.0)
        self.assertEqual(future["projected_balance"], 632.0)
        self.assertIsNone(future["confirmed_balance"])
        self.assertEqual(future["status_scope"], "future")


if __name__ == "__main__":
    unittest.main()

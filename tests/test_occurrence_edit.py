from __future__ import annotations

import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import Account, ActivityEvent, Base, Card, CashflowStatus, MonthClosure, Transaction
from app.repository import read_snapshot
from app.services.occurrence_edit import (
    ClosedOccurrenceError,
    ConcurrentOccurrenceError,
    PaidOccurrenceConfirmationRequired,
    edit_occurrence,
    occurrence_target_is_paid,
)


class OccurrenceEditServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.db.add_all([
            Account(name="Conta A", owner="Pessoa A", initial_balance=1000),
            Account(name="Conta B", owner="Pessoa B", initial_balance=1000),
        ])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def add_tx(self, **changes):
        values = dict(
            member="Pessoa A", category="Casa", description="Luz", type="Fixo",
            account_card="Conta A", amount=100, total_amount=100, installment_count=1,
            start_month="2029-12-01", end_month="2030-03-01", destination_account="",
            linked_pot="", card_flag=0, payment_bank="Conta A", transaction_date="2029-12-10",
            date_precision="recurrence", archived=False,
        )
        values.update(changes)
        row = Transaction(**values)
        self.db.add(row)
        self.db.commit()
        return row.id

    def expected(self, tx_id):
        return next(row for row in read_snapshot(self.db)[0] if row["id"] == tx_id)

    def test_finite_boundary_split_migrates_status_and_keeps_dates(self):
        tx_id = self.add_tx()
        self.db.add(CashflowStatus(month="2030-02", item_key=f"2030-02:tx:{tx_id}", paid=True, paid_on="2030-02-10"))
        self.db.commit()
        result = edit_occurrence(self.db, tx_id, "2030-01", "150", expected_before=self.expected(tx_id))
        self.db.commit()

        rows = list(self.db.scalars(select(Transaction).order_by(Transaction.start_month)))
        self.assertEqual([(r.start_month, r.end_month, r.amount, r.transaction_date) for r in rows], [
            ("2029-12-01", "2029-12-01", 100, "2029-12-10"),
            ("2030-01-01", "2030-01-01", 150, "2030-01-10"),
            ("2030-02-01", "2030-03-01", 100, "2030-02-10"),
        ])
        status = list(self.db.scalars(select(CashflowStatus)))
        self.assertEqual(len(status), 1)
        self.assertEqual(status[0].item_key, f"2030-02:tx:{rows[2].id}")
        self.assertTrue(status[0].paid)
        self.assertEqual(result.target_month, "2030-01")
        self.assertTrue(result.changed)
        event = self.db.scalar(select(ActivityEvent).order_by(ActivityEvent.id.desc()))
        self.assertIsNotNone(event)
        assert event is not None
        assert event.before is not None
        assert event.after is not None
        self.assertEqual(event.entity_type, "occurrence_edit")
        self.assertIsNone(event.undo_expires_at)
        self.assertIn("before_segments", event.before)
        self.assertIn("after_segments", event.after)

    def test_open_occurrence_after_2030_advances_date_and_keeps_open_tail(self):
        tx_id = self.add_tx(start_month="2030-12-01", end_month=None, transaction_date="2030-12-21")
        edit_occurrence(self.db, tx_id, "2031-01", 125, expected_before=self.expected(tx_id))
        self.db.commit()
        rows = list(self.db.scalars(select(Transaction).order_by(Transaction.start_month)))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0].end_month, "2030-12-01")
        self.assertEqual(rows[1].start_month, "2031-01-01")
        self.assertEqual(rows[1].end_month, "2031-01-01")
        self.assertEqual(rows[2].start_month, "2031-02-01")
        self.assertIsNone(rows[2].end_month)
        self.assertEqual([row.transaction_date for row in rows], ["2030-12-21", "2031-01-21", "2031-02-21"])

    def test_month_end_occurrence_clamps_february_without_shifting_march(self):
        tx_id = self.add_tx(start_month="2029-01-01", end_month="2029-03-01", transaction_date="2029-01-31")
        edit_occurrence(self.db, tx_id, "2029-02", 125, expected_before=self.expected(tx_id))
        self.db.commit()
        rows = list(self.db.scalars(select(Transaction).order_by(Transaction.start_month)))
        self.assertEqual([row.transaction_date for row in rows], ["2029-01-31", "2029-02-28", "2029-03-31"])

    def test_closed_target_has_no_db_mutation(self):
        tx_id = self.add_tx()
        self.db.add(MonthClosure(month="2030-01", closed=True))
        self.db.commit()
        before = [(r.id, r.start_month, r.end_month, r.amount) for r in self.db.scalars(select(Transaction))]
        with self.assertRaises(ClosedOccurrenceError):
            edit_occurrence(self.db, tx_id, "2030-01", 150, expected_before=self.expected(tx_id))
        self.db.rollback()
        after = [(r.id, r.start_month, r.end_month, r.amount) for r in self.db.scalars(select(Transaction))]
        self.assertEqual(before, after)
        self.assertEqual(self.db.scalar(select(ActivityEvent).count()) if False else self.db.query(ActivityEvent).count(), 0)

    def test_transfer_moves_both_status_sides_atomically(self):
        tx_id = self.add_tx(
            type="Transferência", description="Envio mensal", member="Pessoa A",
            destination_account="Conta B", end_month="2030-02-01",
        )
        self.db.add(CashflowStatus(month="2030-01", item_key=f"2030-01:tx:{tx_id}:out", paid=True, paid_on="2030-01-08"))
        self.db.commit()
        edit_occurrence(self.db, tx_id, "2030-01", 175, expected_before=self.expected(tx_id), confirm_paid_change=True)
        self.db.commit()
        target_id = next(row.id for row in self.db.scalars(select(Transaction)) if row.start_month == "2030-01-01")
        row_ids = {row.id for row in self.db.scalars(select(Transaction))}
        statuses = list(self.db.scalars(select(CashflowStatus)))
        self.assertEqual({status.item_key for status in statuses}, {
            f"2030-01:tx:{target_id}:out", f"2030-01:tx:{target_id}:in",
        })
        self.assertTrue(all(status.paid for status in statuses))
        self.assertTrue(all(int(status.item_key.split(":tx:", 1)[1].split(":", 1)[0]) in row_ids for status in statuses))

    def test_paid_card_invoice_requires_explicit_confirmation_and_is_preserved(self):
        self.db.add(Card(name="Visa", bank="Conta A", limit_amount=2345))
        tx_id = self.add_tx(account_card="Visa", card_flag=1)
        self.db.add(CashflowStatus(month="2030-01", item_key="2030-01:invoice:Visa", paid=True, paid_on="2030-01-10"))
        self.db.commit()
        snapshot = read_snapshot(self.db)
        self.assertTrue(occurrence_target_is_paid(self.db, tx_id, "2030-01", snapshot=snapshot))
        with self.assertRaises(PaidOccurrenceConfirmationRequired):
            edit_occurrence(self.db, tx_id, "2030-01", 150, expected_before=self.expected(tx_id))
        self.db.rollback()
        edit_occurrence(self.db, tx_id, "2030-01", 150, expected_before=self.expected(tx_id), confirm_paid_change=True)
        self.db.commit()
        status = self.db.get(CashflowStatus, {"month": "2030-01", "item_key": "2030-01:invoice:Visa"})
        self.assertIsNotNone(status)
        assert status is not None
        self.assertTrue(status.paid)
        self.assertEqual(status.paid_on, "2030-01-10")

    def test_expected_before_rejects_concurrent_change_and_keeps_rows(self):
        tx_id = self.add_tx()
        stale = self.expected(tx_id)
        row = self.db.get(Transaction, tx_id)
        assert row is not None
        row.amount = 101
        row.total_amount = 101
        self.db.commit()
        with self.assertRaises(ConcurrentOccurrenceError):
            edit_occurrence(self.db, tx_id, "2030-01", 150, expected_before=stale)
        self.db.rollback()
        self.assertEqual(self.db.query(Transaction).count(), 1)
        unchanged = self.db.get(Transaction, tx_id)
        assert unchanged is not None
        self.assertEqual(unchanged.amount, 101)


if __name__ == "__main__":
    unittest.main()

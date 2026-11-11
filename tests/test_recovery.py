from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.database import get_engine, get_session_factory
from app.models import ActivityEvent, Base, BudgetAdjustment, MonthClosure, RestoreSnapshot, Transaction
from app.recovery_routes import router as recovery_router
from app.repository import (
    MAX_BACKUP_BYTES,
    BackupValidationError,
    UndoConflictError,
    UndoExpiredError,
    backup_payload,
    backup_counts,
    delete_transaction,
    insert_transaction,
    parse_backup_json,
    record_activity_event,
    restore_backup,
    rollback_restore,
    undo_activity_event,
    update_transaction,
)


class RecoverySQLiteTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "recovery.sqlite"
        self.previous_env = {key: os.environ.get(key) for key in ("DATABASE_URL", "FINANCE_APP_PASSWORD", "APP_COOKIE_SECURE")}
        os.environ["DATABASE_URL"] = f"sqlite:///{self.db_path.as_posix()}"
        os.environ["FINANCE_APP_PASSWORD"] = "recovery-test-password"
        os.environ["APP_COOKIE_SECURE"] = "0"
        get_engine.cache_clear()
        get_session_factory.cache_clear()
        Base.metadata.create_all(get_engine())
        self.app = FastAPI()

        @self.app.middleware("http")
        async def family_session(request: Request, call_next):
            request.state.family_session = {"csrf": "test-csrf"}
            return await call_next(request)

        self.app.include_router(recovery_router)
        self.client = TestClient(self.app)
        with get_session_factory()() as db:
            from app.models import Account, Card
            db.add_all([Account(name="Conta", owner="Família", initial_balance=1000), Card(name="Cartão", bank="Conta", limit_amount=2345)])
            db.commit()

    def tearDown(self):
        get_engine.cache_clear()
        get_session_factory.cache_clear()
        for key, value in self.previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp_dir.cleanup()

    def tx_values(self, description="Teste"):
        return {
            "member": "Família", "category": "Casa", "description": description, "type": "Débito/Pix",
            "account_card": "Conta", "amount": 100, "total_amount": 100, "installment_count": 1,
            "start_month": "2026-10-01", "end_month": "2026-10-01", "destination_account": "",
            "linked_pot": "", "transaction_date": "2026-10-10", "date_precision": "exact",
        }

    def test_preview_restore_and_rollback_are_transactional(self):
        with get_session_factory()() as db:
            first_id = insert_transaction(db, self.tx_values("Original"))
            db.add(BudgetAdjustment(created_at="2026-10-20T12:00:00-03:00", start_month="2026-11-01", end_month="2026-12-01",
                target_pot="Reserva - Gasolina Auto", source_pot="Reserva - Lazer", target_before=500, target_after=600,
                source_before=500, source_after=400, sample_months=6))
            db.commit()
            source = backup_payload(db)
            insert_transaction(db, self.tx_values("Extra"))
            db.add(BudgetAdjustment(created_at="2026-10-20T12:01:00-03:00", start_month="2026-11-01", end_month="2026-12-01",
                target_pot="Reserva - Gasolina Auto", source_pot="Reserva - Lazer", target_before=600, target_after=700,
                source_before=400, source_after=300, sample_months=6))
            db.commit()
        encoded = json.dumps(source, ensure_ascii=False).encode()
        preview = self.client.post("/recovery/preview", data={"_csrf": "test-csrf"}, files={"backup_file": ("backup.json", encoded, "application/json")})
        self.assertEqual(preview.status_code, 200, preview.text[:500])
        self.assertIn("1 lançamento(s)", preview.text)
        self.assertIn("ajuste(s) de orçamento", preview.text)
        token = re.search(r'name="preview_token" value="([^"]+)"', preview.text)
        self.assertIsNotNone(token)
        assert token is not None
        restored = self.client.post(
            "/recovery/restore",
            data={"_csrf": "test-csrf", "preview_token": token.group(1), "confirmation": "RESTAURAR BASE"},
            files={"backup_file": ("backup.json", encoded, "application/json")},
            follow_redirects=False,
        )
        self.assertEqual(restored.status_code, 303, restored.text[:500])
        with get_session_factory()() as db:
            self.assertIsNotNone(db.get(Transaction, first_id))
            self.assertEqual(db.query(Transaction).count(), 1)
            restored_adjustments = db.query(BudgetAdjustment).all()
            self.assertEqual(len(restored_adjustments), 1)
            self.assertEqual(restored_adjustments[0].target_after, 600)
            snapshot = db.query(RestoreSnapshot).one()
            snapshot_id = snapshot.id
        rolled = self.client.post(
            f"/recovery/snapshots/{snapshot_id}/rollback",
            data={"_csrf": "test-csrf", "confirmation": "DESFAZER RESTAURAÇÃO"},
            follow_redirects=False,
        )
        self.assertEqual(rolled.status_code, 303)
        with get_session_factory()() as db:
            self.assertEqual(db.query(Transaction).count(), 2)
            self.assertEqual(db.query(BudgetAdjustment).count(), 2)
            self.assertEqual(db.query(RestoreSnapshot).count(), 2)

    def test_update_and_delete_can_be_undone(self):
        with get_session_factory()() as db:
            tx_id = insert_transaction(db, self.tx_values("Antes"))
            db.commit()
            original = db.get(Transaction, tx_id)
            assert original is not None
            before = {"id": tx_id, "member": original.member, "category": original.category, "description": original.description,
                      "type": original.type, "account_card": original.account_card, "amount": 100.0, "total_amount": 100.0,
                      "installment_count": 1, "start_month": original.start_month, "end_month": original.end_month,
                      "destination_account": "", "linked_pot": "", "card_flag": 0, "payment_bank": "Conta",
                      "transaction_date": original.transaction_date, "date_precision": "exact", "archived": False}
            update_transaction(db, tx_id, {**self.tx_values("Depois"), "amount": 150, "total_amount": 150})
            db.commit()
            event = db.query(ActivityEvent).filter_by(action="update", entity_id=str(tx_id)).order_by(ActivityEvent.id.desc()).first()
            assert event is not None
            undo_activity_event(db, event.id)
            db.commit()
            restored = db.get(Transaction, tx_id)
            assert restored is not None
            self.assertEqual(restored.description, "Antes")
            self.assertEqual(restored.amount, 100)
            delete_transaction(db, tx_id)
            db.commit()
            event = db.query(ActivityEvent).filter_by(action="delete", entity_id=str(tx_id)).order_by(ActivityEvent.id.desc()).first()
            assert event is not None
            undo_activity_event(db, event.id)
            db.commit()
            restored = db.get(Transaction, tx_id)
            assert restored is not None
            self.assertEqual(restored.description, "Antes")

    def test_concurrent_change_is_not_overwritten_and_expiry_is_enforced(self):
        with get_session_factory()() as db:
            tx_id = insert_transaction(db, self.tx_values("Original"))
            db.commit()
            update_transaction(db, tx_id, {**self.tx_values("Edited"), "amount": 125, "total_amount": 125})
            db.commit()
            event = db.query(ActivityEvent).filter_by(action="update", entity_id=str(tx_id)).order_by(ActivityEvent.id.desc()).first()
            assert event is not None
            row = db.get(Transaction, tx_id)
            assert row is not None
            row.description = "Concorrente"
            db.commit()
            with self.assertRaises(UndoConflictError):
                undo_activity_event(db, event.id)
            db.rollback()
            unchanged = db.get(Transaction, tx_id)
            assert unchanged is not None
            self.assertEqual(unchanged.description, "Concorrente")

            expired = record_activity_event(db, "update", "transaction", tx_id, {"description": "old"}, {"description": "new"}, created_at=datetime.now(timezone.utc) - timedelta(minutes=16))
            db.commit()
            with self.assertRaises(UndoExpiredError):
                undo_activity_event(db, expired.id)

    def test_undo_requires_reopening_a_month_closed_after_the_change(self):
        for action in ("create", "update", "delete"):
            with self.subTest(action=action), get_session_factory()() as db:
                closure = db.get(MonthClosure, "2026-10")
                if closure is None:
                    closure = MonthClosure(month="2026-10", closed=False)
                    db.add(closure)
                closure.closed = False
                tx_id = insert_transaction(db, self.tx_values(action))
                if action == "update":
                    update_transaction(db, tx_id, {**self.tx_values(action), "amount": 150, "total_amount": 150})
                elif action == "delete":
                    delete_transaction(db, tx_id)
                db.commit()
                event = db.query(ActivityEvent).filter_by(action=action, entity_id=str(tx_id)).order_by(ActivityEvent.id.desc()).first()
                assert event is not None
                event_id = event.id
                closure.closed = True
                db.commit()
                with self.assertRaisesRegex(UndoConflictError, "mês fechado"):
                    undo_activity_event(db, event_id)
                db.rollback()
                unchanged_event = db.get(ActivityEvent, event_id)
                assert unchanged_event is not None
                self.assertIsNone(unchanged_event.undone_at)
                self.assertEqual(db.get(Transaction, tx_id) is not None, action != "delete")
                closure = db.get(MonthClosure, "2026-10")
                assert closure is not None
                closure.closed = False
                db.commit()
                undo_activity_event(db, event_id)
                db.commit()
                undone_event = db.get(ActivityEvent, event_id)
                assert undone_event is not None
                self.assertIsNotNone(undone_event.undone_at)

    def test_backup_schema_relationship_and_size_limits(self):
        with self.assertRaises(BackupValidationError):
            parse_backup_json(b"{}")
        with self.assertRaises(BackupValidationError):
            parse_backup_json(b"{}" * (MAX_BACKUP_BYTES // 2 + 1))
        with get_session_factory()() as db:
            payload = backup_payload(db)
        older_v1_payload = dict(payload)
        older_v1_payload.pop("budget_adjustments", None)
        self.assertEqual(backup_counts(older_v1_payload)["budget_adjustments"], 0)
        payload["transactions"] = [{**self.tx_values(), "id": 1, "archived": False}]
        payload["transactions"][0]["account_card"] = "Conta que nao existe"
        with self.assertRaises(BackupValidationError):
            backup_counts(payload)

    def test_backup_rejects_negative_override_on_installment_purchase(self):
        with get_session_factory()() as db:
            payload = backup_payload(db)
        payload["transactions"] = [{
            **self.tx_values("Compra parcelada"), "id": 1, "type": "Parcelado", "account_card": "Cartão",
            "amount": 100, "total_amount": 300, "installment_count": 3,
            "start_month": "2026-10-01", "end_month": "2026-12-01", "transaction_date": "2026-10-10",
            "card_flag": 1, "payment_bank": "Conta", "installment_overrides": {"2026-11": "-0.01"},
            "archived": False,
        }]
        with self.assertRaisesRegex(BackupValidationError, "Ajustes negativos só são permitidos"):
            backup_counts(payload)


if __name__ == "__main__":
    unittest.main()

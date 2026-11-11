from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import SESSION_COOKIE
from app.database import get_engine, get_session_factory
from app.main import app
from app.models import (
    Account, ActivityEvent, AppSetting, Card, CashflowStatus, HeartbeatTask, LookupValue,
    OpenFinanceAccountSnapshot, OpenFinanceItem, OpenFinanceTransaction, Transaction,
)


class OpenFinanceReviewRouteTests(unittest.TestCase):
    password = "review-only-test-password"

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "isolated-review.sqlite"
        self.previous_env = {key: os.environ.get(key) for key in ("DATABASE_URL", "FINANCE_APP_PASSWORD", "APP_COOKIE_SECURE")}
        os.environ["DATABASE_URL"] = f"sqlite:///{self.db_path.as_posix()}"
        os.environ["FINANCE_APP_PASSWORD"] = self.password
        os.environ["APP_COOKIE_SECURE"] = "0"
        get_engine.cache_clear()
        get_session_factory.cache_clear()
        self.client = TestClient(app)
        self.client.__enter__()
        with get_session_factory()() as db:
            db.add_all([
                Account(name="Conta Hener", owner="Hener", initial_balance=2345),
                LookupValue(kind="categoria", value="Casa", position=0),
                AppSetting(key="base_month", value=json.dumps("2026-08-01")),
                AppSetting(key="forecast_end", value=json.dumps("2030-12-01")),
                AppSetting(key="minimum_balance", value=json.dumps(2345)),
                Transaction(
                    id=1, member="Hener", category="Casa", description="Aluguel",
                    type="Fixo", account_card="Conta Hener", amount=3200,
                    total_amount=3200, installment_count=1, start_month="2026-09-01",
                    end_month="2026-12-01", destination_account="", linked_pot="",
                    card_flag=0, payment_bank="Conta Hener", transaction_date="2026-09-15",
                    date_precision="exact", archived=False,
                ),
                OpenFinanceAccountSnapshot(
                    account_id="bank-hener", snapshot_date="2026-10-29", item_key="itau_hener",
                    owner="Hener", institution="Itaú", name="Conta Corrente", account_type="BANK",
                    balance=2345, currency_code="BRL", synced_at="2026-10-29T10:00:00+00:00",
                ),
                OpenFinanceTransaction(
                    transaction_id="bank-pix-1", item_key="itau_hener", account_id="bank-hener",
                    occurred_on="2026-10-15", description="PIX ALUGUEL", amount=3250,
                    currency_code="BRL", transaction_type="DEBIT", status="POSTED",
                    last_seen_at="2026-10-29T10:00:00+00:00", is_current=True,
                ),
            ])
            db.commit()
        self.login()
        self._save_mapping()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        get_engine.cache_clear()
        get_session_factory.cache_clear()
        for key, value in self.previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp_dir.cleanup()

    def login(self):
        response = self.client.get("/login")
        token = re.search(r'name="_csrf" value="([^"]+)"', response.text)
        assert token is not None
        result = self.client.post("/login", data={"_csrf": token.group(1), "password": self.password}, follow_redirects=False)
        self.assertEqual(result.status_code, 303)

    def _token(self, page: str) -> str:
        token = re.search(r'name="_csrf" value="([^"]+)"', page)
        assert token is not None
        return token.group(1)

    def _save_mapping(self):
        page = self.client.get("/open-finance?month=2026-10-01")
        result = self.client.post("/open-finance/map", data={
            "_csrf": self._token(page.text), "account_id": "bank-hener", "internal_name": "Conta Hener",
        }, follow_redirects=False)
        self.assertEqual(result.status_code, 303)

    def test_manual_open_finance_sync_imports_available_snapshot_without_forcing_patch(self):
        page = self.client.get("/open-finance?month=2026-10-01")
        summary = {
            "configured": 1, "errors": 0, "pending": 0, "items": [],
            "accounts": 1, "transactions": 1, "bills": 0,
        }
        with patch("app.open_finance_routes.sync_configured_items", return_value=summary) as sync:
            response = self.client.post("/open-finance/sync", data={"_csrf": self._token(page.text)}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertIn("sync=complete", response.headers["location"])
        self.assertIs(sync.call_args.kwargs.get("refresh"), False)
        with get_session_factory()() as db:
            self.assertEqual(db.query(Transaction).count(), 1)

    def test_scheduled_pluggy_sync_only_reads_cache_and_never_changes_ledger(self):
        with get_session_factory()() as db:
            db.add(HeartbeatTask(
                task_uid="pluggy-test-task", task_name="pluggy-daily-read-sync",
                enabled=True, created_at="2026-11-03T00:00:00+00:00",
            ))
            db.commit()
        summary = {
            "configured": 1, "errors": 0, "pending": 0,
            "items": [{"key": "ITAU", "ok": True}],
            "accounts": 1, "transactions": 1, "bills": 0,
        }
        with (
            patch("app.open_finance_routes.verify_platform_jwt", return_value={"openId": "cron-open"}),
            patch("app.open_finance_routes.resolve_platform_task", return_value={"openId": "cron-open", "taskUid": "pluggy-test-task"}),
            patch("app.open_finance_routes.sync_configured_items", return_value=summary) as sync,
        ):
            self.client.cookies.set(SESSION_COOKIE, "test-heartbeat-token")
            response = self.client.post("/api/scheduled/pluggy-sync")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertIs(sync.call_args.kwargs.get("refresh"), False)
        with get_session_factory()() as db:
            self.assertEqual(db.query(Transaction).count(), 1)
            task = db.get(HeartbeatTask, "pluggy-test-task")
            self.assertIsNotNone(task)
            assert task is not None
            self.assertIsNotNone(task.last_run_at)

    def test_open_finance_connection_shows_provider_times_in_brasilia_timezone(self):
        with get_session_factory()() as db:
            db.add(OpenFinanceItem(
                item_key="ITAU", owner="Hener", institution="Itaú", status="UPDATED",
                execution_status="SUCCESS", provider_updated_at="2026-11-03T02:26:57.218Z",
                next_auto_sync_at="2026-11-04T02:26:57.218Z",
                synced_at="2026-11-03T11:39:00+00:00",
            ))
            db.commit()
        page = self.client.get("/open-finance?month=2026-11-01")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Última coleta na instituição (informada pela Pluggy): 02/11/2026 às 23:26", page.text)
        self.assertIn("Próxima coleta automática indicada pela Pluggy: 03/11/2026 às 23:26", page.text)
        self.assertIn("Última leitura FinFam: 03/11/2026 às 08:39", page.text)

    def _review_form(self, action: str, transaction_id: str = "bank-pix-1") -> dict[str, str]:
        page = self.client.get("/open-finance?month=2026-10-01")
        return {
            "_csrf": self._token(page.text), "transaction_id": transaction_id, "action": action,
            "_context_month": "2026-10", "_context_person": "Família", "_context_horizon": "12",
        }

    def test_approved_adjustment_changes_only_one_competence_and_is_idempotent(self):
        page = self.client.get("/open-finance?month=2026-10-01")
        self.assertIn("Aluguel", page.text)
        self.assertIn('action="/open-finance/review"', page.text)
        self.assertIn("Ajustar só esta competência para R$ 3.250,00 e confirmar", page.text)
        daily_page = self.client.get("/today?month=2026-10-01")
        self.assertNotIn("Saldo Pluggy", daily_page.text)
        self.assertNotIn("diferença (banco − FinFam)", daily_page.text)

        form = self._review_form("adjust_confirm")
        saved = self.client.post("/open-finance/review", data=form, follow_redirects=False)
        self.assertEqual(saved.status_code, 303, saved.text)
        self.assertTrue(saved.headers["location"].startswith("/open-finance?"))
        self.assertIn("review=adjusted", saved.headers["location"])

        with get_session_factory()() as db:
            rows = list(db.scalars(select(Transaction).where(Transaction.description == "Aluguel").order_by(Transaction.start_month)))
            self.assertEqual([(row.start_month, row.end_month, round(float(row.amount), 2)) for row in rows], [
                ("2026-09-01", "2026-09-01", 3200.0),
                ("2026-10-01", "2026-10-01", 3250.0),
                ("2026-11-01", "2026-12-01", 3200.0),
            ])
            paid = list(db.scalars(select(CashflowStatus)))
            self.assertEqual(len(paid), 1)
            self.assertEqual(paid[0].month, "2026-10")
            self.assertEqual(paid[0].paid_on, "2026-10-15")
            self.assertTrue(paid[0].paid)
            review_events = list(db.scalars(select(ActivityEvent).where(
                ActivityEvent.entity_type == "open_finance_review",
            )))
            self.assertEqual(len(review_events), 1)
            assert review_events[0].after is not None
            self.assertEqual(review_events[0].after["decision"], "adjusted_and_confirmed")

        duplicate = self.client.post("/open-finance/review", data=form, follow_redirects=False)
        self.assertEqual(duplicate.status_code, 303)
        self.assertIn("review=already", duplicate.headers["location"])
        with get_session_factory()() as db:
            self.assertEqual(db.query(CashflowStatus).count(), 1)
            self.assertEqual(db.query(ActivityEvent).filter_by(entity_type="open_finance_review").count(), 1)

    def test_pending_can_be_ignored_but_server_rejects_confirmation(self):
        with get_session_factory()() as db:
            row = db.get(OpenFinanceTransaction, "bank-pix-1")
            assert row is not None
            row.status = "PENDING"
            db.commit()
        page = self.client.get("/open-finance?month=2026-10-01")
        self.assertIn("aguarde POSTED", page.text)
        rejected = self.client.post("/open-finance/review", data=self._review_form("confirm"), follow_redirects=False)
        self.assertEqual(rejected.status_code, 409)
        with get_session_factory()() as db:
            self.assertEqual(db.query(CashflowStatus).count(), 0)
            self.assertEqual(db.query(ActivityEvent).filter_by(entity_type="open_finance_review").count(), 0)

        ignored = self.client.post("/open-finance/review", data=self._review_form("ignore"), follow_redirects=False)
        self.assertEqual(ignored.status_code, 303)
        self.assertIn("review=ignored", ignored.headers["location"])
        with get_session_factory()() as db:
            self.assertEqual(db.query(CashflowStatus).count(), 0)
            self.assertEqual(db.query(ActivityEvent).filter_by(entity_type="open_finance_review").count(), 1)

    def test_competing_bank_transactions_are_ambiguous_and_cannot_be_confirmed(self):
        with get_session_factory()() as db:
            db.add(OpenFinanceTransaction(
                transaction_id="bank-pix-2", item_key="itau_hener", account_id="bank-hener",
                occurred_on="2026-10-15", description="PIX OUTRO", amount=3249,
                currency_code="BRL", transaction_type="DEBIT", status="POSTED",
                last_seen_at="2026-10-29T10:01:00+00:00", is_current=True,
            ))
            db.commit()
        page = self.client.get("/open-finance?month=2026-10-01")
        self.assertIn("Correspondência ambígua.", page.text)
        rejected = self.client.post("/open-finance/review", data=self._review_form("confirm"), follow_redirects=False)
        self.assertEqual(rejected.status_code, 409)
        with get_session_factory()() as db:
            self.assertEqual(db.query(CashflowStatus).count(), 0)
            self.assertEqual(db.query(ActivityEvent).filter_by(entity_type="open_finance_review").count(), 0)

    def test_offline_financial_snapshot_and_view_are_removed(self):
        self.assertEqual(self.client.get("/api/offline/snapshot?month=2026-10").status_code, 404)
        self.assertEqual(self.client.get("/static/offline.html").status_code, 404)
        self.assertEqual(self.client.get("/static/js/offline-vault.js").status_code, 404)

    def test_bank_registration_prefills_normal_form_without_creating_a_row(self):
        response = self.client.get(
            "/transactions/new?month=2026-10-01&person=Hener&horizon=12"
            "&return_page=open-finance&source_type=Débito%2FPix&source_date=2026-10-15"
            "&source_amount=3250&source_description=PIX%20ALUGUEL"
            "&source_account=Conta%20Hener&source_member=Hener"
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('value="PIX ALUGUEL"', response.text)
        self.assertIn('value="2026-10-15"', response.text)
        self.assertIn('value="3250.0"', response.text)
        self.assertIn('value="open-finance"', response.text)
        with get_session_factory()() as db:
            self.assertEqual(db.query(Transaction).count(), 1)

    def test_saving_a_bank_suggestion_returns_to_open_finance(self):
        page = self.client.get(
            "/transactions/new?month=2026-10&person=Hener&horizon=12"
            "&return_page=open-finance&source_type=Débito%2FPix"
        )
        self.assertEqual(page.status_code, 200, page.text[:500])
        csrf = page.text.split('name="_csrf" value="', 1)[1].split('"', 1)[0]
        response = self.client.post("/transactions/new", data={
            "_csrf": csrf, "type": "Débito/Pix", "transaction_date": "2026-10-15",
            "description": "PIX ALUGUEL conciliado manualmente", "category": "Casa",
            "member": "Hener", "account_card": "Conta Hener", "total_amount": "3250",
            "first_bill_month": "2026-10", "installment_count": "1",
            "destination_account": "", "linked_pot": "", "return_page": "open-finance",
            "_context_month": "2026-10", "_context_person": "Hener", "_context_horizon": "12",
        }, follow_redirects=False)
        self.assertEqual(response.status_code, 303, response.text[:500])
        self.assertTrue(response.headers["location"].startswith("/open-finance?"), response.headers["location"])
        self.assertIn("saved=1", response.headers["location"])
        with get_session_factory()() as db:
            self.assertEqual(db.query(Transaction).count(), 2)

    def test_card_cycle_match_is_visible_but_cannot_confirm_the_individual_purchase(self):
        with get_session_factory()() as db:
            db.add_all([
                Card(name="Visa Hener", bank="Itaú", limit_amount=10000),
                AppSetting(key="card_closing_days", value=json.dumps({"Visa Hener": 3})),
                Transaction(
                    id=2, member="Hener", category="Casa", description="Geladeira",
                    type="Crédito à vista", account_card="Visa Hener", amount=100,
                    total_amount=100, installment_count=1, start_month="2026-11-01",
                    end_month="2026-11-01", destination_account="", linked_pot="",
                    card_flag=1, payment_bank="Conta Hener", transaction_date="2026-10-15",
                    date_precision="exact", archived=False,
                ),
                OpenFinanceAccountSnapshot(
                    account_id="card-hener", snapshot_date="2026-10-29", item_key="itau_hener",
                    owner="Hener", institution="Itaú", name="Visa Hener", account_type="CREDIT",
                    balance=100, currency_code="BRL", credit_limit=10000,
                    available_credit_limit=9900, synced_at="2026-10-29T10:00:00+00:00",
                ),
                OpenFinanceTransaction(
                    transaction_id="card-purchase-1", item_key="itau_hener", account_id="card-hener",
                    occurred_on="2026-10-15", description="LOJA ELETRO", amount=100,
                    currency_code="BRL", transaction_type="DEBIT", status="POSTED",
                    installment_number=1, total_installments=1,
                    last_seen_at="2026-10-29T10:00:00+00:00", is_current=True,
                ),
            ])
            db.commit()
        page = self.client.get("/open-finance?month=2026-10-01")
        mapped = self.client.post("/open-finance/map", data={
            "_csrf": self._token(page.text), "account_id": "card-hener", "internal_name": "Visa Hener",
        }, follow_redirects=False)
        self.assertEqual(mapped.status_code, 303)

        page = self.client.get("/open-finance?month=2026-10-01")
        self.assertIn("Possível compra na fatura 2026-11", page.text)
        self.assertIn("fechamento dia 3", page.text)
        self.assertIn("parcela 1/1", page.text)
        rejected = self.client.post(
            "/open-finance/review", data=self._review_form("confirm", "card-purchase-1"),
            follow_redirects=False,
        )
        self.assertEqual(rejected.status_code, 409)
        with get_session_factory()() as db:
            self.assertEqual(db.query(CashflowStatus).count(), 0)

        ignored = self.client.post(
            "/open-finance/review", data=self._review_form("ignore", "card-purchase-1"),
            follow_redirects=False,
        )
        self.assertEqual(ignored.status_code, 303)
        page = self.client.get("/open-finance?month=2026-10-01")
        self.assertIn("sugestão ignorada", page.text)


    def test_unmatched_bank_transaction_opens_prefilled_form_without_writing(self):
        with get_session_factory()() as db:
            db.add(OpenFinanceTransaction(
                transaction_id="bank-unmatched-1", item_key="itau_hener", account_id="bank-hener",
                occurred_on="2026-10-10", description="PADARIA", amount=29.90,
                currency_code="BRL", transaction_type="DEBIT", status="POSTED",
                last_seen_at="2026-10-29T10:02:00+00:00", is_current=True,
            ))
            db.commit()
        page = self.client.get("/open-finance?month=2026-10-01")
        self.assertIn("Revisar e lançar manualmente no FinFam", page.text)
        match = re.search(r'href="([^"]*source_type[^"]*)">Revisar e lançar manualmente no FinFam', page.text)
        self.assertIsNotNone(match)
        assert match is not None
        form = self.client.get(match.group(1).replace("&amp;", "&"))
        self.assertEqual(form.status_code, 200, form.text)
        self.assertIn('value="PADARIA"', form.text)
        self.assertIn('value="2026-10-10"', form.text)
        self.assertIn('value="29.9"', form.text)
        self.assertIn('value="open-finance"', form.text)
        with get_session_factory()() as db:
            self.assertEqual(db.query(Transaction).count(), 1)

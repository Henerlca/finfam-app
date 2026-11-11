from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.database import get_engine, get_session_factory
from app.main import app
from app.models import Account, Transaction


class ReserveSelectorTests(unittest.TestCase):
    password = "test-only-reserve-selector-password"

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "reserve-selector.sqlite"
        self.previous_env = {
            key: os.environ.get(key)
            for key in ("DATABASE_URL", "FINANCE_APP_PASSWORD", "APP_COOKIE_SECURE")
        }
        os.environ["DATABASE_URL"] = f"sqlite:///{self.db_path.as_posix()}"
        os.environ["FINANCE_APP_PASSWORD"] = self.password
        os.environ["APP_COOKIE_SECURE"] = "0"
        get_engine.cache_clear()
        get_session_factory.cache_clear()
        self.client = TestClient(app)
        self.client.__enter__()
        with get_session_factory()() as db:
            db.add(Account(name="Conta teste", owner="Família", initial_balance=1000))
            db.add_all([
                Transaction(
                    id=1, member="Família", category="", description="Reserva Z",
                    type="Reserva", account_card="Conta teste", amount=100,
                    total_amount=100, installment_count=1, start_month="2026-10-01",
                    end_month="2026-12-01", destination_account="", linked_pot="",
                    card_flag=0, payment_bank="Conta teste", transaction_date="2026-10-01",
                    date_precision="recurrence", archived=False,
                ),
                Transaction(
                    id=2, member="Família", category="", description="Reserva A",
                    type="Reserva", account_card="Conta teste", amount=50,
                    total_amount=50, installment_count=1, start_month="2026-10-01",
                    end_month="2026-12-01", destination_account="", linked_pot="",
                    card_flag=0, payment_bank="Conta teste", transaction_date="2026-10-02",
                    date_precision="recurrence", archived=False,
                ),
                Transaction(
                    id=3, member="Família", category="", description="Reserva arquivada",
                    type="Reserva", account_card="Conta teste", amount=25,
                    total_amount=25, installment_count=1, start_month="2026-10-01",
                    end_month="2026-12-01", destination_account="", linked_pot="",
                    card_flag=0, payment_bank="Conta teste", transaction_date="2026-10-03",
                    date_precision="recurrence", archived=True,
                ),
                Transaction(
                    id=4, member="Família", category="Casa", description="Compra existente",
                    type="Débito/Pix", account_card="Conta teste", amount=20,
                    total_amount=20, installment_count=1, start_month="2026-10-01",
                    end_month="2026-10-01", destination_account="", linked_pot="Reserva antiga",
                    card_flag=0, payment_bank="Conta teste", transaction_date="2026-10-04",
                    date_precision="exact", archived=False,
                ),
            ])
            db.commit()

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
        login_page = self.client.get("/login")
        csrf = re.search(r'name="_csrf" value="([^"]+)"', login_page.text)
        self.assertIsNotNone(csrf)
        assert csrf is not None
        response = self.client.post(
            "/login",
            data={"_csrf": csrf.group(1), "password": self.password},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)

    @staticmethod
    def option_pairs(html: str) -> list[tuple[str, str]]:
        select = re.search(r'<select\b[^>]*\bname="linked_pot"[^>]*>(.*?)</select>', html, re.S)
        assert select is not None
        return [
            (value, re.sub(r"\s+", " ", label).strip())
            for value, label in re.findall(r'<option value="([^"]*)"[^>]*>(.*?)</option>', select.group(1), re.S)
            if value in {"", "Reserva A", "Reserva antiga", "Reserva Z", "Reserva arquivada"}
        ]

    def test_selector_lists_active_reserves_sorts_and_preserves_old_edit_value(self):
        self.login()

        new_page = self.client.get("/transactions/new")
        self.assertEqual(new_page.status_code, 200)
        self.assertIn('<label for="linked-pot-select">Descontar de uma reserva (opcional)</label>', new_page.text)
        self.assertIn("As opções acompanham a competência da data do lançamento.", new_page.text)
        self.assertIn('<select id="linked-pot-select" name="linked_pot"', new_page.text)
        self.assertNotRegex(new_page.text, r'<input[^>]+name="linked_pot"')
        self.assertEqual(
            self.option_pairs(new_page.text),
            [("", "Sem reserva vinculada"), ("Reserva A", "Reserva A"), ("Reserva Z", "Reserva Z")],
        )
        self.assertRegex(new_page.text, r'<option value="" selected>Sem reserva vinculada</option>')
        for field in ("transaction_date", "type", "description", "category", "member", "account_card", "total_amount", "destination_account", "end_month"):
            self.assertIn(f'name="{field}"', new_page.text)

        edit_page = self.client.get("/transactions/4/edit")
        self.assertEqual(edit_page.status_code, 200)
        self.assertEqual(
            self.option_pairs(edit_page.text),
            [("", "Sem reserva vinculada"), ("Reserva A", "Reserva A"), ("Reserva Z", "Reserva Z")],
        )
        self.assertIn("O vínculo atual “Reserva antiga” não está planejado nesta competência.", edit_page.text)
        self.assertNotRegex(edit_page.text, r'<option value="Reserva antiga"')
        self.assertNotIn("Reserva arquivada", edit_page.text)


if __name__ == "__main__":
    unittest.main()

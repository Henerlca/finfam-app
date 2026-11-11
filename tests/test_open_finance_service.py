from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import Base, OpenFinanceAccountSnapshot, OpenFinanceBill, OpenFinanceItem, OpenFinanceTransaction
from app.services.open_finance import (
    PluggyClient, _local_date, configured_items, has_run_today_in_brasilia,
    pluggy_ready, sync_configured_items,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        import json
        return json.dumps(self.payload).encode()


def test_configured_items_are_fixed_aliases_and_optional():
    assert configured_items({}) == []
    env = {
        "PLUGGY_ITEM_ID_ITAU": "itau-id",
        "PLUGGY_ITEM_ID_MERCADO_PAGO": "mp-id",
        "PLUGGY_ITEM_ID_SANTANDER": "santander-id",
        "PLUGGY_ITEM_ID_C6": "c6-id",
        "PLUGGY_ITEM_ID_NUBANK_JU": "nubank-id",
        "PLUGGY_CLIENT_ID_HENER": "h-id", "PLUGGY_CLIENT_SECRET_HENER": "h-secret",
        "PLUGGY_CLIENT_ID_JU": "j-id", "PLUGGY_CLIENT_SECRET_JU": "j-secret",
    }
    assert [row["key"] for row in configured_items(env)] == ["ITAU", "MERCADO_PAGO", "SANTANDER", "C6", "NUBANK_JU"]
    assert pluggy_ready(env)


def test_local_date_converts_utc_to_brasilia_calendar_day():
    assert _local_date("2026-10-26T01:30:00.000Z") == "2026-10-25"
    assert _local_date("2026-10-26") == "2026-10-26"


def test_heartbeat_daily_guard_uses_brasilia_day_boundary():
    last_run = "2026-10-26T11:00:00+00:00"
    assert has_run_today_in_brasilia(last_run, datetime(2026, 10, 27, 1, 59, tzinfo=timezone.utc))
    assert not has_run_today_in_brasilia(last_run, datetime(2026, 10, 27, 3, 0, tzinfo=timezone.utc))
    assert not has_run_today_in_brasilia(None, datetime(2026, 10, 27, 3, 0, tzinfo=timezone.utc))


def test_client_reads_auth_and_uses_cursor_only_on_fixed_api_host():
    calls = []
    def transport(request, timeout):
        calls.append((request.method, request.full_url, request.headers))
        if request.full_url.endswith("/auth"):
            return FakeResponse({"apiKey": "test-key"})
        if "/v2/transactions" in request.full_url:
            query = parse_qs(urlsplit(request.full_url).query)
            if "after" not in query:
                return FakeResponse({"results": [{"id": "one"}], "next": "?accountId=a&after=cursor%2B1"})
            return FakeResponse({"results": [{"id": "two"}], "next": None})
        return FakeResponse({})
    client = PluggyClient("id", "secret", transport=transport)
    rows = client.list_transactions("a", "2025-10-26", "2026-10-26")
    assert [row["id"] for row in rows] == ["one", "two"]
    assert calls[0][0] == "POST" and calls[0][1].endswith("/auth")
    assert calls[1][1].startswith("https://api.pluggy.ai/v2/transactions?")
    assert calls[2][1].startswith("https://api.pluggy.ai/v2/transactions?")
    assert "X-api-key" in calls[1][2]


def test_sync_persists_only_external_tables_and_is_idempotent():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    env = {
        "PLUGGY_ITEM_ID_ITAU": "item-1",
        "PLUGGY_CLIENT_ID_HENER": "client-h", "PLUGGY_CLIENT_SECRET_HENER": "secret-h",
    }
    calls = []
    def transport(request, timeout):
        import json
        calls.append((request.method, request.full_url))
        if request.full_url.endswith("/auth"):
            return FakeResponse({"apiKey": "test-key"})
        if "/items/item-1" in request.full_url:
            return FakeResponse({"id": "item-1", "status": "UPDATED", "executionStatus": "SUCCESS"})
        if "/accounts?" in request.full_url:
            return FakeResponse({"results": [{"id": "account-1", "name": "Conta teste", "type": "BANK", "balance": 123.45, "currencyCode": "BRL", "bankData": {"closingBalance": 123.45}}]})
        if "/v2/transactions?" in request.full_url:
            return FakeResponse({"results": [{"id": "provider-tx-1", "accountId": "account-1", "date": "2026-10-26T01:30:00Z", "description": "Compra", "amount": -21.30, "currencyCode": "BRL", "status": "POSTED", "type": "DEBIT"}], "next": None})
        return FakeResponse({"results": []})
    with Session(engine) as db:
        with patch.dict("os.environ", env, clear=True):
            result = sync_configured_items(db, now=datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc), transport=transport)
            assert result["errors"] == 0 and result["transactions"] == 1
            assert not any(method == "PATCH" for method, _ in calls)
            transaction = db.get(OpenFinanceTransaction, "provider-tx-1")
            account = db.get(OpenFinanceAccountSnapshot, ("account-1", "2026-10-26"))
            assert transaction is not None and transaction.amount == Decimal("-21.30")
            assert transaction.occurred_on == "2026-10-25"
            assert account is not None and account.balance == Decimal("123.45")
            saved_item = db.scalar(select(OpenFinanceItem).where(OpenFinanceItem.item_key == "ITAU"))
            assert saved_item is not None and saved_item.status == "UPDATED"
            # nenhuma escrita ocorre em qualquer tabela de lançamento do FinFam
            assert db.query(OpenFinanceBill).count() == 0
            again = sync_configured_items(db, now=datetime(2026, 10, 26, 13, 0, tzinfo=timezone.utc), transport=transport)
            assert again["transactions"] == 1
            assert not any(method == "PATCH" for method, _ in calls)
            assert db.query(OpenFinanceTransaction).count() == 1
            assert db.query(OpenFinanceAccountSnapshot).count() == 1

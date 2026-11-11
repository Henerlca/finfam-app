import io
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from email.message import Message
from urllib.error import HTTPError
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import Base, CashflowStatus, OpenFinanceAccountSnapshot, OpenFinanceItem, OpenFinanceTransaction, Transaction
from app.open_finance_routes import _status_label
from app.services.open_finance import sync_configured_items


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def test_failed_refresh_reads_provider_cache_and_preserves_source_time():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    env = {
        "PLUGGY_ITEM_ID_ITAU": "item-1",
        "PLUGGY_CLIENT_ID_HENER": "client-h",
        "PLUGGY_CLIENT_SECRET_HENER": "secret-h",
    }
    calls = []
    patch_bodies = []

    def transport(request, timeout):
        calls.append((request.method, request.full_url))
        if request.full_url.endswith("/auth"):
            return FakeResponse({"apiKey": "test-key"})
        if request.method == "PATCH":
            patch_bodies.append(json.loads(request.data.decode("utf-8")))
            body = json.dumps({"error": {"code": 400, "message": "Cannot update item"}}).encode()
            raise HTTPError(request.full_url, 400, "Bad Request", Message(), io.BytesIO(body))
        if "/items/item-1" in request.full_url:
            return FakeResponse({
                "id": "item-1", "status": "UPDATED", "executionStatus": "SUCCESS",
                "lastUpdatedAt": "2026-10-25T14:00:00.000Z",
            })
        if "/accounts?" in request.full_url:
            return FakeResponse({"results": [{
                "id": "account-1", "name": "Conta teste", "type": "BANK",
                "balance": 123.45, "currencyCode": "BRL",
            }]})
        if "/v2/transactions?" in request.full_url:
            return FakeResponse({"results": [{
                "id": "provider-tx-1", "accountId": "account-1",
                "date": "2026-10-26T01:30:00Z", "description": "Compra de teste",
                "amount": -21.30, "currencyCode": "BRL", "status": "POSTED", "type": "DEBIT",
            }], "next": None})
        raise AssertionError("Endpoint não esperado no transporte falso")

    with Session(engine) as db:
        with patch.dict(os.environ, env, clear=True):
            local_tx = Transaction(
                member="Hener", category="Mercado", description="Lançamento local",
                type="Débito/Pix", amount=10.0, total_amount=10.0,
                start_month="2026-10", transaction_date="2026-10-26",
            )
            local_status = CashflowStatus(month="2026-10", item_key="local-test", paid=True, paid_on="2026-10-26")
            db.add_all([local_tx, local_status])
            db.commit()
            local_tx_id = local_tx.id
            local_before = (local_tx.member, local_tx.description, local_tx.type, local_tx.amount, local_tx.transaction_date)
            result = sync_configured_items(
                db, now=datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc), transport=transport,
                refresh=True,
            )
            assert result["errors"] == 1
            assert result["accounts"] == 1 and result["transactions"] == 1
            assert result["items"] == [{"key": "ITAU", "status": "UPDATED", "ok": False, "data_available": True}]
            assert patch_bodies == [{}]

            item = db.get(OpenFinanceItem, "ITAU")
            account = db.get(OpenFinanceAccountSnapshot, ("account-1", "2026-10-26"))
            transaction = db.get(OpenFinanceTransaction, "provider-tx-1")
            assert item is not None and item.last_error_code == "400"
            assert item.provider_updated_at == "2026-10-25T14:00:00.000Z"
            assert item.last_error_message and "coleta anterior" in item.last_error_message
            assert account is not None and account.balance == Decimal("123.45")
            assert transaction is not None and transaction.is_current
            local_after = db.get(Transaction, local_tx_id)
            paid_after = db.get(CashflowStatus, ("2026-10", "local-test"))
            assert local_after is not None
            assert (local_after.member, local_after.description, local_after.type, local_after.amount, local_after.transaction_date) == local_before
            assert paid_after is not None and paid_after.paid and paid_after.paid_on == "2026-10-26"

            label, message = _status_label(item.status, True, item.last_error_message)
            assert "coleta não renovada" in label
            assert "confira a data" in message

            # Leitura de cache não repete o PATCH; GET bem-sucedido limpa a falha local anterior.
            before_patch_calls = len(patch_bodies)
            before_read_calls = len(calls)
            read_only_result = sync_configured_items(
                db, now=datetime(2026, 10, 26, 13, 0, tzinfo=timezone.utc),
                transport=transport, refresh=False,
            )
            assert len(patch_bodies) == before_patch_calls
            second_cycle_calls = calls[before_read_calls:]
            assert second_cycle_calls
            assert all(method == "GET" or (method == "POST" and url.endswith("/auth")) for method, url in second_cycle_calls)
            assert read_only_result["errors"] == 0
            assert db.query(OpenFinanceTransaction).count() == 1
            cached_item = db.get(OpenFinanceItem, "ITAU")
            assert cached_item is not None and cached_item.last_error_code == ""
            cached_label, cached_message = _status_label(cached_item.status, True, cached_item.last_error_message)
            assert cached_label == "Atualizado" and cached_message == ""
            local_after_cache_read = db.get(Transaction, local_tx_id)
            paid_after_cache_read = db.get(CashflowStatus, ("2026-10", "local-test"))
            assert local_after_cache_read is not None and local_after_cache_read.description == "Lançamento local"
            assert paid_after_cache_read is not None and paid_after_cache_read.paid

"""Integração Pluggy opcional, somente leitura de dados financeiros."""
from __future__ import annotations

import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OpenFinanceAccountSnapshot, OpenFinanceBill, OpenFinanceItem, OpenFinanceTransaction

API_BASE = "https://api.pluggy.ai"
SAO_PAULO = ZoneInfo("America/Sao_Paulo")
MAX_PAGES = 80
MAX_BILLS_PAGES = 20
MAX_ITEM_REFRESH_POLLS = 4
ITEM_REFRESH_POLL_SECONDS = 5
ITEM_DEFINITIONS = (
    ("ITAU", "Hener", "Itaú", "PLUGGY_ITEM_ID_ITAU"),
    ("MERCADO_PAGO", "Hener", "Mercado Pago", "PLUGGY_ITEM_ID_MERCADO_PAGO"),
    ("SANTANDER", "Hener", "Santander", "PLUGGY_ITEM_ID_SANTANDER"),
    ("C6", "Hener", "C6 Bank", "PLUGGY_ITEM_ID_C6"),
    ("NUBANK_JU", "Ju", "Nubank", "PLUGGY_ITEM_ID_NUBANK_JU"),
)


class PluggyError(RuntimeError):
    def __init__(self, code: str, message: str = ""):
        self.code = (str(code or "PLUGGY_ERROR")[:64])
        self.message = (str(message or "")[:240])
        super().__init__(self.code)


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _local_date(value: Any) -> str | None:
    if not value:
        return None
    text = str(value)
    if len(text) == 10:
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(SAO_PAULO).date().isoformat()
    except (ValueError, TypeError):
        try:
            return date.fromisoformat(str(value)[:10]).isoformat()
        except ValueError:
            return None


def configured_items(environ: Mapping[str, str] | None = None) -> list[dict[str, str]]:
    source = environ if environ is not None else os.environ
    rows = []
    for key, owner, institution, env_name in ITEM_DEFINITIONS:
        item_id = str(source.get(env_name, "")).strip()
        if item_id:
            rows.append({"key": key, "owner": owner, "institution": institution, "item_id": item_id})
    return rows


def pluggy_ready(environ: Mapping[str, str] | None = None) -> bool:
    source = environ if environ is not None else os.environ
    configured = {item["key"] for item in configured_items(source)}
    return configured == {row[0] for row in ITEM_DEFINITIONS} and all(
        source.get(f"PLUGGY_CLIENT_ID_{owner.upper()}") and source.get(f"PLUGGY_CLIENT_SECRET_{owner.upper()}")
        for owner in {"Hener", "Ju"}
    )


def has_run_today_in_brasilia(last_run_at: str | None, now: datetime) -> bool:
    """Make scheduled retries idempotent by local calendar day, not UTC day."""
    if not last_run_at:
        return False
    try:
        previous = datetime.fromisoformat(last_run_at)
    except (TypeError, ValueError):
        return False
    moment = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    if previous.tzinfo is None:
        previous = previous.replace(tzinfo=timezone.utc)
    return previous.astimezone(SAO_PAULO).date() == moment.astimezone(SAO_PAULO).date()


class PluggyClient:
    """Small API client with injectable transport for isolated tests."""

    def __init__(self, client_id: str, client_secret: str, transport: Callable = urlopen, timeout: int = 25):
        self._transport = transport
        self._timeout = timeout
        result = self._request("POST", "/auth", body={"clientId": client_id, "clientSecret": client_secret}, authenticated=False)
        self._api_key = str(result.get("apiKey") or "")
        if not self._api_key:
            raise PluggyError("AUTH_RESPONSE_INVALID")

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None, authenticated: bool = True) -> dict[str, Any]:
        url = API_BASE + path
        if params:
            url += "?" + urlencode({key: value for key, value in params.items() if value is not None})
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if authenticated and getattr(self, "_api_key", ""):
            headers["X-API-KEY"] = self._api_key
        request = Request(url, data=json.dumps(body).encode("utf-8") if body is not None else None, headers=headers, method=method)
        try:
            response = self._transport(request, timeout=self._timeout)
            with response:
                content = response.read()
            decoded = json.loads(content.decode("utf-8")) if content else {}
        except HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except Exception:
                payload = {}
            payload_object = payload if isinstance(payload, dict) else {}
            nested_error = payload_object.get("error")
            error = nested_error if isinstance(nested_error, dict) else payload_object
            code = error.get("code") or error.get("statusCode") or f"HTTP_{exc.code}"
            message = error.get("message") or ""
            raise PluggyError(str(code), str(message)) from None
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            # Nunca incluir URL, request, token ou corpo externo na exceção/log.
            raise PluggyError("NETWORK_OR_RESPONSE_ERROR", type(exc).__name__) from None
        if not isinstance(decoded, dict):
            raise PluggyError("INVALID_JSON_RESPONSE")
        return decoded

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("GET", path, params=params)

    def refresh_item(self, item_id: str) -> dict[str, Any]:
        """Request a fresh provider read for a configured Item without changing credentials."""
        if not re.fullmatch(r"[A-Za-z0-9-]{1,128}", item_id):
            raise PluggyError("INVALID_CONFIGURED_ITEM_ID")
        item_path = "/items/" + item_id
        response = self._request("PATCH", item_path, body={})
        status = str(response.get("status") or "")
        for _ in range(MAX_ITEM_REFRESH_POLLS):
            if status != "UPDATING":
                return response
            time.sleep(ITEM_REFRESH_POLL_SECONDS)
            response = self.get(item_path)
            status = str(response.get("status") or "")
        return response

    def list_transactions(self, account_id: str, date_from: str, date_to: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        after = None
        for _ in range(MAX_PAGES):
            params = {"accountId": account_id, "dateFrom": date_from, "dateTo": date_to, "after": after}
            page = self.get("/v2/transactions", params=params)
            records.extend(page.get("results") or [])
            next_value = page.get("next")
            if not next_value:
                return records
            parsed = urlsplit(str(next_value))
            if parsed.scheme or parsed.netloc or parsed.path not in ("", "/"):
                raise PluggyError("INVALID_PAGINATION_CURSOR")
            next_after = parse_qs(parsed.query).get("after", [None])[0]
            if not next_after or next_after == after:
                raise PluggyError("INVALID_PAGINATION_CURSOR")
            after = next_after
        raise PluggyError("TRANSACTION_PAGE_LIMIT")

    def list_bills(self, account_id: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for page_number in range(1, MAX_BILLS_PAGES + 1):
            page = self.get("/bills", params={"accountId": account_id, "pageSize": 500, "page": page_number})
            records.extend(page.get("results") or [])
            if page_number >= int(page.get("totalPages") or 1):
                return records
        raise PluggyError("BILL_PAGE_LIMIT")


def _item_error_text(code: str | None, status: str | None) -> str:
    if status == "WAITING_USER_INPUT":
        return "A conexão precisa de uma ação no banco. Os dados anteriores continuam apenas como referência."
    if status == "LOGIN_ERROR":
        return "O banco recusou a atualização. Revise a conexão Pluggy; não alteramos lançamentos."
    if code:
        return "Não foi possível solicitar uma coleta nova. Os dados podem ser da coleta anterior; confira a data informada pela Pluggy."
    if status in {"OUTDATED", "ERROR"}:
        return "Não foi possível atualizar esta conexão agora. O restante do FinFam continua disponível."
    return ""


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _upsert_item(db: Session, item: dict[str, Any], response: dict[str, Any] | None, *, synced_at: datetime, error_code: str | None = None) -> OpenFinanceItem:
    row = db.get(OpenFinanceItem, item["key"])
    if row is None:
        row = OpenFinanceItem(item_key=item["key"], owner=item["owner"], institution=item["institution"])
        db.add(row)
    status = str((response or {}).get("status") or ("ERROR" if error_code else "UNKNOWN"))[:40]
    row.owner = item["owner"]
    row.institution = item["institution"]
    row.status = status
    row.execution_status = str((response or {}).get("executionStatus") or "")[:64]
    row.provider_updated_at = str((response or {}).get("lastUpdatedAt") or "")[:40] or None
    row.next_auto_sync_at = str((response or {}).get("nextAutoSyncAt") or "")[:40] or None
    row.synced_at = synced_at.astimezone(timezone.utc).isoformat(timespec="seconds")
    row.last_error_code = (error_code or "")[:64]
    row.last_error_message = _item_error_text(error_code, status)
    return row


def _store_account(db: Session, item: dict[str, str], account: dict[str, Any], snapshot_date: str, synced_at: datetime) -> None:
    account_id = str(account.get("id") or "")
    if not account_id:
        return
    credit = account.get("creditData") or {}
    bank = account.get("bankData") or {}
    digits = "".join(ch for ch in str(account.get("number") or "") if ch.isdigit())
    row = db.get(OpenFinanceAccountSnapshot, (account_id, snapshot_date))
    if row is None:
        row = OpenFinanceAccountSnapshot(account_id=account_id, snapshot_date=snapshot_date)
        db.add(row)
    row.item_key = item["key"]
    row.owner = item["owner"]
    row.institution = item["institution"]
    row.name = str(account.get("marketingName") or account.get("name") or item["institution"])[:191]
    row.account_type = str(account.get("type") or "")[:32]
    row.subtype = str(account.get("subtype") or "")[:64]
    row.card_last4 = digits[-4:] if str(account.get("type")) == "CREDIT" and digits else ""
    row.balance = _decimal(account.get("balance"))
    row.closing_balance = _decimal(bank.get("closingBalance"))
    row.currency_code = str(account.get("currencyCode") or "BRL")[:8]
    row.balance_close_date = _local_date(credit.get("balanceCloseDate"))
    row.balance_due_date = _local_date(credit.get("balanceDueDate"))
    row.credit_limit = _decimal(credit.get("creditLimit"))
    row.available_credit_limit = _decimal(credit.get("availableCreditLimit"))
    row.minimum_payment = _decimal(credit.get("minimumPayment"))
    row.synced_at = synced_at.astimezone(timezone.utc).isoformat(timespec="seconds")


def _store_transaction(db: Session, item: dict[str, str], account_id: str, data: dict[str, Any], synced_at: datetime) -> None:
    transaction_id = str(data.get("id") or "")
    if not transaction_id:
        return
    row = db.get(OpenFinanceTransaction, transaction_id)
    if row is None:
        row = OpenFinanceTransaction(transaction_id=transaction_id)
        db.add(row)
    metadata = data.get("creditCardMetadata") or {}
    row.item_key = item["key"]
    row.account_id = account_id
    row.occurred_on = _local_date(data.get("date")) or str(data.get("date") or "")[:10]
    row.description = str(data.get("description") or "Movimentação bancária")[:255]
    row.amount = _decimal(data.get("amount")) or Decimal("0.00")
    row.currency_code = str(data.get("currencyCode") or "BRL")[:8]
    row.transaction_type = str(data.get("type") or "")[:32]
    row.status = str(data.get("status") or "")[:32]
    row.category = str(data.get("category") or "")[:128]
    row.provider_id = str(data.get("providerId") or "")[:128]
    row.installment_number = _optional_int(metadata.get("installmentNumber"))
    row.total_installments = _optional_int(metadata.get("totalInstallments"))
    row.last_seen_at = synced_at.astimezone(timezone.utc).isoformat(timespec="seconds")
    row.is_current = True


def _store_bill(db: Session, item: dict[str, str], account_id: str, data: dict[str, Any], synced_at: datetime) -> None:
    bill_id = str(data.get("id") or "")
    if not bill_id:
        return
    row = db.get(OpenFinanceBill, bill_id)
    if row is None:
        row = OpenFinanceBill(bill_id=bill_id)
        db.add(row)
    payments = data.get("payments") or []
    payment_amount = sum((_decimal(payment.get("amount")) or Decimal("0.00") for payment in payments if isinstance(payment, dict)), Decimal("0.00"))
    payment_dates = [date_value for payment in payments if isinstance(payment, dict) for date_value in [_local_date(payment.get("paymentDate"))] if date_value]
    row.item_key = item["key"]
    row.account_id = account_id
    row.due_date = _local_date(data.get("dueDate"))
    row.closing_date = _local_date(data.get("billClosingDate"))
    row.total_amount = _decimal(data.get("totalAmount")) or Decimal("0.00")
    row.minimum_payment = _decimal(data.get("minimumPaymentAmount"))
    row.payment_amount_observed = payment_amount
    row.last_payment_date = max(payment_dates) if payment_dates else None
    row.currency_code = str(data.get("totalAmountCurrencyCode") or "BRL")[:8]
    row.synced_at = synced_at.astimezone(timezone.utc).isoformat(timespec="seconds")


def sync_configured_items(db: Session, *, now: datetime | None = None, transport: Callable = urlopen, refresh: bool = False) -> dict[str, Any]:
    """Read pre-authorized Items and store isolated provider snapshots; never writes finance records."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local_today = moment.astimezone(SAO_PAULO).date()
    date_from = (local_today - timedelta(days=365)).isoformat()
    date_to = local_today.isoformat()
    env = os.environ
    configured = configured_items(env)
    clients: dict[str, PluggyClient | PluggyError] = {}
    result = {"items": [], "accounts": 0, "transactions": 0, "bills": 0, "errors": 0, "pending": 0, "configured": len(configured)}

    for item in configured:
        owner_key = item["owner"].upper()
        refresh_error: PluggyError | None = None
        try:
            if owner_key not in clients:
                client_id = str(env.get(f"PLUGGY_CLIENT_ID_{owner_key}") or "").strip()
                client_secret = str(env.get(f"PLUGGY_CLIENT_SECRET_{owner_key}") or "").strip()
                if not client_id or not client_secret:
                    raise PluggyError("MISSING_OWNER_CREDENTIALS")
                clients[owner_key] = PluggyClient(client_id, client_secret, transport=transport)
            client = clients[owner_key]
            if isinstance(client, PluggyError):
                raise client
            if refresh:
                # PATCH vazio pede nova coleta do Item existente, sem enviar credenciais.
                try:
                    item_data = client.refresh_item(item["item_id"])
                except PluggyError as exc:
                    refresh_error = exc
                    # Mesmo sem refresh, tenta ler o cache da Pluggy e mostra sua data real.
                    item_data = client.get("/items/" + item["item_id"])
            else:
                item_data = client.get("/items/" + item["item_id"])
                # Uma leitura do snapshot não herda a falha de um PATCH anterior.
                # Só um erro atual informado pelo Item deve manter o estado de atenção.
                provider_error = item_data.get("error")
                if provider_error:
                    if isinstance(provider_error, dict):
                        provider_error_code = provider_error.get("code") or provider_error.get("type")
                    else:
                        provider_error_code = None
                    refresh_error = PluggyError(str(provider_error_code or "PROVIDER_ITEM_ERROR"))
            item_status = str(item_data.get("status") or "UNKNOWN")
            if item_status == "UPDATING":
                _upsert_item(db, item, item_data, synced_at=moment, error_code=refresh_error.code if refresh_error else None)
                db.commit()
                result["pending"] += 1
                result["items"].append({"key": item["key"], "status": item_status, "ok": False, "data_available": False})
                continue
            if item_status in {"WAITING_USER_INPUT", "LOGIN_ERROR", "ERROR", "OUTDATED"} and refresh_error is None:
                refresh_error = PluggyError("ITEM_STATUS_" + item_status)
            _upsert_item(db, item, item_data, synced_at=moment, error_code=refresh_error.code if refresh_error else None)
            accounts_response = client.get("/accounts", params={"itemId": item["item_id"]})
            accounts = list(accounts_response.get("results") or [])
            transactions: list[tuple[str, dict[str, Any]]] = []
            bills: list[tuple[str, dict[str, Any]]] = []
            for account in accounts:
                account_id = str(account.get("id") or "")
                if not account_id:
                    continue
                transactions.extend((account_id, tx) for tx in client.list_transactions(account_id, date_from, date_to))
                if str(account.get("type") or "") == "CREDIT":
                    bills.extend((account_id, bill) for bill in client.list_bills(account_id))
            # A atualização do banco acontece somente após todas as páginas do Item terem sido lidas.
            for account in accounts:
                _store_account(db, item, account, local_today.isoformat(), moment)
            account_ids = {str(account.get("id") or "") for account in accounts}
            for account_id in account_ids:
                if account_id:
                    for old in db.scalars(select(OpenFinanceTransaction).where(OpenFinanceTransaction.account_id == account_id)):
                        old.is_current = False
            for account_id, transaction in transactions:
                _store_transaction(db, item, account_id, transaction, moment)
            for account_id, bill in bills:
                _store_bill(db, item, account_id, bill, moment)
            db.commit()
            result["accounts"] += len(accounts)
            result["transactions"] += len(transactions)
            result["bills"] += len(bills)
            if refresh_error:
                result["errors"] += 1
            result["items"].append({"key": item["key"], "status": item_status, "ok": refresh_error is None, "data_available": True})
        except PluggyError as exc:
            db.rollback()
            _upsert_item(db, item, None, synced_at=moment, error_code=exc.code)
            db.commit()
            if owner_key not in clients:
                clients[owner_key] = exc
            result["errors"] += 1
            result["items"].append({"key": item["key"], "status": "ERROR", "ok": False})
        except Exception:
            db.rollback()
            _upsert_item(db, item, None, synced_at=moment, error_code="UNEXPECTED_SYNC_ERROR")
            db.commit()
            result["errors"] += 1
            result["items"].append({"key": item["key"], "status": "ERROR", "ok": False})
    return result

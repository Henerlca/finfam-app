"""Revisões explícitas de entradas; não escreve dados."""
from __future__ import annotations
import hashlib
import json
import unicodedata
from typing import Any
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from app.auth import family_password


def _serializer():
    password = family_password()
    if not password:
        raise RuntimeError("A senha familiar não foi configurada.")
    secret = hashlib.sha256(b"finance-review-v1\0" + password.encode()).digest()
    return URLSafeTimedSerializer(secret, salt="finance-entry-review-v1")


def sign_review(payload: dict[str, Any]) -> str:
    return _serializer().dumps(payload)


def read_review(token: str) -> dict[str, Any] | None:
    try:
        payload = _serializer().loads(token, max_age=1800)
        return payload if isinstance(payload, dict) else None
    except (BadSignature, SignatureExpired, TypeError):
        return None


def values_digest(values: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(values, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _description(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).split())


def probable_duplicates(txs, values, excluding=None):
    matches = []
    for row in txs:
        if row.get("archived") or row.get("id") == excluding:
            continue
        if (str(row.get("transaction_date")) == str(values.get("transaction_date"))
            and str(row.get("account_card")) == str(values.get("account_card"))
            and row.get("type") == values.get("type")
            and _description(row.get("description")) == _description(values.get("description"))
            and abs(float(row.get("total_amount") or row.get("amount") or 0)
                    - float(values.get("total_amount") or values.get("amount") or 0)) < 0.005):
            matches.append(row)
    return matches

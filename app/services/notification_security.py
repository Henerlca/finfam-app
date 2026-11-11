"""Credenciais de dispositivo só dão acesso à criação de rascunhos, nunca ao livro financeiro."""
from __future__ import annotations
import hashlib
import hmac
import time
from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app.auth import family_password
from app.models import NotificationDevice, NotificationRateLimit


def secret_hash(value: str, purpose: str) -> str:
    key = hashlib.sha256(b"finfam-notification-v1\0" + family_password().encode()).digest()
    return hmac.new(key, (purpose + "\0" + value).encode(), hashlib.sha256).hexdigest()


def authenticated_device(db, request: Request) -> NotificationDevice:
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer ") or not 32 <= len(header[7:]) <= 128:
        raise HTTPException(401, "Dispositivo não autorizado.")
    device = db.scalar(select(NotificationDevice).where(
        NotificationDevice.token_hash == secret_hash(header[7:], "device"),
        NotificationDevice.active.is_(True),
    ).with_for_update())
    if device is None:
        raise HTTPException(401, "Dispositivo revogado ou não autorizado.")
    return device


def enforce_pairing_rate(db, request: Request) -> None:
    # Não usar X-Forwarded-For fornecido pelo cliente. A janela fica no banco
    # compartilhado, funcionando também com mais de um processo/container.
    client = request.client.host if request.client else "unknown"
    key = secret_hash(client, "pairing-rate")
    window = int(time.time()) // 600
    row = db.scalar(select(NotificationRateLimit).where(NotificationRateLimit.key == key).with_for_update())
    if row is None:
        row = NotificationRateLimit(key=key, window=window, count=0)
        db.add(row)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            raise HTTPException(429, "Aguarde antes de tentar parear novamente.") from None
    if row.window != window:
        row.window, row.count = window, 0
    row.count += 1
    blocked = row.count > 20
    db.commit()  # Tentativas inválidas também contam.
    if blocked:
        raise HTTPException(429, "Muitas tentativas. Aguarde dez minutos.")

"""Listener Android → rascunho privado → formulário normal → aprovação humana."""
from __future__ import annotations
import json
import math
import re
import secrets
import time
import unicodedata
from datetime import datetime, timezone, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from app.database import get_session_factory
from app.models import NotificationPairing, NotificationDevice, NotificationDraft, NotificationRateLimit
from app.routes import checked_form, render, _nav_context, _return_url, _form_context
from app.services.entry_review import read_review, sign_review
from app.services.notification_security import authenticated_device, enforce_pairing_rate, secret_hash

router = APIRouter()
NATIVE_PATHS = {"/api/notificacoes/parear", "/api/notificacoes/receber", "/api/notificacoes/status"}

_GENERIC_APPROVED = re.compile(r"^(?:compra\s+)?aprovada(?:\s+com\s+sucesso)?[.!]?$", re.IGNORECASE)
_GENERIC_CARD = re.compile(
    r"^(?:seu\s+)?cart[aã]o(?:\s+(?P<institution>[\wÀ-ÿ][\wÀ-ÿ .&-]{0,39}))?[.!]?$",
    re.IGNORECASE,
)

_GENERIC_LABELS = {"cartao uniclass", "seu cartao", "cartao", "seu cartao de credito",
                   "cartao de credito", "seu cartao de debito", "cartao de debito"}

def _source_bank(source_package: str | None) -> str | None:
    package = (source_package or "").casefold()
    if "itau" in package or "itaubank" in package:
        return "Itaú"
    if "mercadopago" in package or "mercado_pago" in package:
        return "Mercado Pago"
    if "santander" in package:
        return "Santander"
    if "c6bank" in package:
        return "C6"
    return None

def display_merchant(value: str | None, source_package: str | None = None) -> str:
    """Replace only unmistakable notification templates; never invent a merchant."""
    text = (value or "").strip()
    normalized = re.sub(r"\s+", " ", text)
    plain = "".join(c for c in unicodedata.normalize("NFKD", normalized.casefold())
                    if not unicodedata.combining(c))
    folded = re.sub(r"[^a-z0-9]+", " ", plain).strip()
    if folded in _GENERIC_LABELS:
        bank = _source_bank(source_package)
        return f"Compra no cartão · {bank}" if bank else "Compra no cartão"
    if not normalized:
        return "Compra no cartão"
    if _GENERIC_APPROVED.fullmatch(normalized):
        return "Compra no cartão"
    match = _GENERIC_CARD.fullmatch(normalized)
    if match:
        institution = (match.group("institution") or "").strip(" .&-")
        if institution.casefold() in {"uniclass", "de crédito", "de débito", "credito", "debito"}:
            bank = _source_bank(source_package)
            return f"Compra no cartão · {bank}" if bank else "Compra no cartão"
        if institution and institution.casefold() not in {"de", "do", "da", "de crédito", "de débito"}:
            return f"Compra no cartão · {institution}"
        return "Compra no cartão"
    return text


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def bounded_json(request: Request) -> dict[str, Any]:
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > 4096:
            raise HTTPException(413, "Conteúdo muito extenso.")
    try:
        value = json.loads(content)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "JSON inválido.") from None
    if not isinstance(value, dict):
        raise HTTPException(400, "Objeto JSON necessário.")
    return value


@router.get("/notificacoes")
def inbox(request: Request):
    with get_session_factory()() as db:
        devices = list(db.scalars(select(NotificationDevice).order_by(NotificationDevice.id.desc())))
        rows = list(db.scalars(select(NotificationDraft).where(NotificationDraft.status == "pending")
                     .order_by(NotificationDraft.id.desc()).limit(100)))
        counts = dict(db.execute(select(NotificationDraft.device_id, func.count())
                      .where(NotificationDraft.status == "pending")
                      .group_by(NotificationDraft.device_id)).all())
        # Não passar hashes/tokens ou objetos ORM à interface.
        drafts = [{"id": r.id, "source": r.source_package, "date": r.occurred_at[:10],
                   "amount": r.amount, "merchant": display_merchant(r.merchant, r.source_package),
                   "type_hint": r.type_hint, "installment_count": r.installment_count} for r in rows]
        safe_devices = [{"id": r.id, "name": r.name, "active": r.active,
                         "last_seen": r.last_seen_at, "pending_count": counts.get(r.id, 0)} for r in devices]
    return render(request, "notifications.html", "Lançamentos detectados", drafts=drafts, devices=safe_devices)


@router.post("/notificacoes/parear")
async def create_pairing_code(request: Request):
    await checked_form(request)
    code = f"{secrets.randbelow(100000000):08d}"
    with get_session_factory()() as db:
        # Só o último código permanece válido. Expira em dez minutos.
        db.execute(delete(NotificationPairing))
        db.add(NotificationPairing(code_hash=secret_hash(code, "pairing"), expires_at=int(time.time()) + 600, used=False))
        db.commit()
    return render(request, "notification_pairing.html", "Conectar celular", pairing_code=code)


@router.post("/api/notificacoes/parear")
async def exchange_pairing_code(request: Request):
    data = await bounded_json(request)
    with get_session_factory()() as db:
        enforce_pairing_rate(db, request)
        if set(data) - {"code", "device_name"} or not re.fullmatch(r"\d{8}", str(data.get("code", ""))):
            raise HTTPException(400, "Código inválido.")
        name = str(data.get("device_name", "Celular da família")).strip()
        if not name or len(name) > 80 or any(ord(c) < 32 for c in name):
            raise HTTPException(400, "Nome de dispositivo inválido.")
        changed = db.execute(update(NotificationPairing).where(
            NotificationPairing.code_hash == secret_hash(str(data["code"]), "pairing"),
            NotificationPairing.used.is_(False), NotificationPairing.expires_at > int(time.time()),
        ).values(used=True))
        if cast(CursorResult, changed).rowcount != 1:
            raise HTTPException(401, "Código expirado ou já utilizado. Gere outro no app familiar.")
        token = secrets.token_urlsafe(32)
        device = NotificationDevice(name=name, token_hash=secret_hash(token, "device"), active=True, created_at=now_iso())
        db.add(device)
        db.flush()
        device_id = device.id
        db.commit()
    return {"token": token, "device_id": device_id}


@router.get("/api/notificacoes/status")
def native_status(request: Request):
    with get_session_factory()() as db:
        device = authenticated_device(db, request)
        return {"active": True, "device_id": device.id}


@router.post("/api/notificacoes/receber")
async def receive_draft(request: Request):
    data = await bounded_json(request)
    if set(data) - {"event_id", "source_package", "occurred_at", "amount", "merchant", "type_hint", "installment_count"}:
        raise HTTPException(400, "Enviar somente os campos normalizados; texto bruto não é aceito.")
    event_id = data.get("event_id", "")
    source = data.get("source_package", "")
    if not isinstance(event_id, str) or not re.fullmatch(r"[a-f0-9]{64}", event_id):
        raise HTTPException(400, "Identificador do evento inválido.")
    if not isinstance(source, str) or len(source) > 191 or not re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+", source):
        raise HTTPException(400, "App-fonte inválido.")
    try:
        occurred = datetime.fromisoformat(str(data.get("occurred_at", "")).replace("Z", "+00:00"))
        if occurred.tzinfo is None:
            raise ValueError()
        current = datetime.now(timezone.utc)
        if occurred > current + timedelta(days=1) or occurred < current - timedelta(days=120):
            raise ValueError()
        if isinstance(data.get("amount"), bool):
            raise ValueError()
        amount = float(str(data.get("amount", "")))
        if not math.isfinite(amount) or not 0 < amount <= 1e9 or abs(amount - round(amount, 2)) > 0.000001:
            raise ValueError()
    except (ValueError, TypeError, OverflowError):
        raise HTTPException(400, "Data ou valor da transação inválidos.") from None
    merchant = data.get("merchant", "")
    if not isinstance(merchant, str) or len(merchant) > 120 or any(ord(c) < 32 for c in merchant):
        raise HTTPException(400, "Descrição normalizada inválida.")
    hint = data.get("type_hint")
    if hint not in {"Crédito à vista", "Débito/Pix", "Receita"}:
        raise HTTPException(400, "Tipo de sugestão inválido.")
    count = data.get("installment_count")
    if count is not None and (isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 120):
        raise HTTPException(400, "Quantidade de parcelas inválida.")
    if count is not None and count > 1 and hint != "Crédito à vista":
        raise HTTPException(400, "Parcelas só se aplicam a compras com cartão.")
    with get_session_factory()() as db:
        device = authenticated_device(db, request)
        guard = db.scalar(select(NotificationRateLimit).where(NotificationRateLimit.key == "draft-quota").with_for_update())
        if guard is None:
            db.add(NotificationRateLimit(key="draft-quota", window=0, count=0))
            try:
                db.flush()
            except IntegrityError:
                db.rollback()
                raise HTTPException(429, "Aguarde antes de reenviar este evento.") from None
        previous = db.scalar(select(NotificationDraft).where(NotificationDraft.device_id == device.id, NotificationDraft.event_id == event_id))
        if previous:
            return {"draft_id": previous.id, "status": previous.status}
        pending = db.scalar(select(func.count()).select_from(NotificationDraft).where(NotificationDraft.status == "pending")) or 0
        recent = db.scalar(select(func.count()).select_from(NotificationDraft).where(NotificationDraft.device_id == device.id,
                    NotificationDraft.received_at >= (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds"))) or 0
        if pending >= 1000 or recent >= 500:
            raise HTTPException(429, "Revise os rascunhos pendentes antes de receber novos eventos.")
        row = NotificationDraft(device_id=device.id, event_id=event_id, source_package=source,
               occurred_at=occurred.isoformat(timespec="seconds"), received_at=now_iso(), amount=amount,
               merchant=display_merchant(merchant, source), type_hint=hint, installment_count=count, status="pending")
        device.last_seen_at = now_iso()
        db.add(row)
        try:
            db.flush()
            draft_id = row.id
            db.commit()
        except IntegrityError:
            db.rollback()
            duplicate = db.scalar(select(NotificationDraft).where(NotificationDraft.device_id == device.id, NotificationDraft.event_id == event_id))
            if duplicate is None:
                raise HTTPException(409, "Atualize a caixa de revisão.") from None
            return {"draft_id": duplicate.id, "status": duplicate.status}
    return {"draft_id": draft_id, "status": "pending"}


@router.post("/notificacoes/dispositivos/{device_id}/revogar")
async def revoke_device(request: Request, device_id: int):
    form = await checked_form(request)
    if form.get("confirmation") != "revoke":
        raise HTTPException(400, "Confirme a revogação.")
    with get_session_factory()() as db:
        device = db.get(NotificationDevice, device_id)
        if device is None:
            raise HTTPException(404, "Dispositivo não encontrado.")
        device.active = False
        db.commit()
    return RedirectResponse(_return_url(request, "/notificacoes", form), 303)


@router.post("/notificacoes/{draft_id}/descartar")
async def discard_draft(request: Request, draft_id: int):
    form = await checked_form(request)
    with get_session_factory()() as db:
        row = db.get(NotificationDraft, draft_id)
        if row is None or row.status != "pending":
            raise HTTPException(409, "Rascunho já revisado ou inexistente.")
        row.status = "discarded"
        db.commit()
    return RedirectResponse(_return_url(request, "/notificacoes", form), 303)


@router.get("/notificacoes/{draft_id}/revisar")
def review_draft(request: Request, draft_id: int):
    with get_session_factory()() as db:
        row = db.get(NotificationDraft, draft_id)
        if row is None or row.status != "pending":
            raise HTTPException(404, "Rascunho pendente não encontrado.")
        nav = _nav_context(request)
        count = row.installment_count or 1
        token = sign_review({"kind": "notification-draft", "id": row.id,
                            "csrf": request.state.family_session["csrf"]})
        local_date = datetime.fromisoformat(row.occurred_at).astimezone(ZoneInfo("America/Sao_Paulo")).date().isoformat()
        initial = {"transaction_date": local_date, "type": "Parcelado" if count > 1 else row.type_hint,
                   "description": display_merchant(row.merchant, row.source_package), "total_amount": row.amount,
                   "amount": row.amount / count, "installment_count": count, "start_month": local_date[:7] + "-01",
                   "member": nav["person"], "account_card": "", "category": "", "_notification_token": token}
    return _form_context(request, "Revisar lançamento detectado", initial,
             notification_review=True)


def claim_draft(db, token: str, csrf: str) -> int:
    review = read_review(token)
    if not review or review.get("kind") != "notification-draft" or review.get("csrf") != csrf:
        raise ValueError("A revisão expirou. Abra novamente o rascunho na caixa de notificações.")
    draft_id = review.get("id")
    if not isinstance(draft_id, int):
        raise ValueError("Rascunho inválido.")
    result = db.execute(update(NotificationDraft).where(NotificationDraft.id == draft_id,
              NotificationDraft.status == "pending").values(status="processing"))
    if cast(CursorResult, result).rowcount != 1:
        raise ValueError("Este rascunho já foi aprovado ou descartado; nenhum novo lançamento foi criado.")
    return draft_id


def complete_draft(db, draft_id: int, tx_id: int):
    db.execute(update(NotificationDraft).where(NotificationDraft.id == draft_id, NotificationDraft.status == "processing")
               .values(status="approved", transaction_id=tx_id))

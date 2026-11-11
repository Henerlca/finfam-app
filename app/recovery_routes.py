from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.database import get_session_factory
from app.models import RestoreSnapshot
from app.repository import (
    MAX_BACKUP_BYTES,
    BackupValidationError,
    RecoveryError,
    UndoExpiredError,
    backup_counts,
    backup_digest,
    parse_backup_json,
    latest_family_activity,
    record_activity_event,
    restore_backup,
    rollback_restore,
    undo_activity_event,
)
from app.routes import checked_form, family_sync_state, render
from app.services.entry_review import read_review, sign_review

ROOT = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(ROOT / "app" / "templates"))
router = APIRouter()

RESTORE_CONFIRMATION = "RESTAURAR BASE"
ROLLBACK_CONFIRMATION = "DESFAZER RESTAURAÇÃO"
MAX_ACTIVITY_ROWS = 50


def _is_undoable(event: Any) -> bool:
    if event.undone_at or event.entity_type != "transaction" or event.action not in {"create", "update", "delete"}:
        return False
    try:
        expires = datetime.fromisoformat(str(event.undo_expires_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) <= expires.astimezone(timezone.utc)


def _local_stamp(value: Any) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(ZoneInfo("America/Sao_Paulo")).strftime("%d/%m/%Y às %H:%M")
    except (TypeError, ValueError):
        return ""


def _activity_view(event: Any) -> dict[str, Any]:
    data = event.after or event.before or {}
    entity = str(event.entity_type or "")
    action = str(event.action or "")
    title, subject, detail = "Alteração registrada", "Família", ""
    if entity == "transaction":
        title = {"create": "Lançamento adicionado", "update": "Lançamento atualizado", "delete": "Lançamento excluído", "undo": "Alteração desfeita"}.get(action, title)
        subject = str(data.get("description") or "Lançamento")
        detail = str(data.get("type") or "")
    elif entity == "cashflow_status":
        title = "Baixa confirmada" if bool(data.get("paid")) else "Baixa desfeita"
        subject = str(data.get("description") or "Movimentação do mês")
        detail = f"Data da baixa: {data['paid_on']}" if data.get("paid_on") else "Status atualizado"
    elif entity == "month_closure":
        title = "Mês fechado" if bool(data.get("closed")) else "Mês reaberto"
        subject = f"Competência {str(event.entity_id)[:7]}"
    elif entity == "settings":
        title, subject = "Preferências atualizadas", "Configurações da família"
    elif entity == "accounts":
        title, subject = "Contas atualizadas", "Contas da família"
        detail = f"{len(data.get('names', []))} conta(s)"
    elif entity == "cards":
        title, subject = "Cartões atualizados", "Cartões da família"
        detail = f"{len(data.get('names', []))} cartão(ões)"
    elif entity == "lookup":
        title, subject = "Listas atualizadas", "Categorias e opções"
    elif entity == "budget_adjustment":
        title = "Ajuste de reservas aplicado"
        subject = f"{data.get('target_pot') or 'Reserva'} · {data.get('source_pot') or 'outra reserva'}"
        detail = f"A partir de {str(data.get('start_month') or '')[:7]}"
    elif entity == "backup":
        title = "Backup revertido" if data.get("rolled_back_snapshot") else "Backup restaurado"
        subject = "Base financeira da família"
    elif entity == "recovery":
        title, subject = "Alteração desfeita", "Lançamento restaurado ao estado anterior"
    return {
        "id": event.id,
        "when": _local_stamp(event.created_at),
        "action_label": title,
        "subject": subject,
        "detail": detail,
        "undoable": _is_undoable(event),
        "undone_at": _local_stamp(event.undone_at),
    }


def _activity_page(request: Request, *, error: str = "", notice: str = "", preview: dict[str, Any] | None = None, preview_token: str = ""):
    with get_session_factory()() as db:
        events = [_activity_view(event) for event in latest_family_activity(db, MAX_ACTIVITY_ROWS)]
        snapshots = list(db.scalars(select(RestoreSnapshot).order_by(RestoreSnapshot.created_at.desc(), RestoreSnapshot.id.desc()).limit(20)))
    return render(
        request,
        "recovery.html",
        "Atividade e recuperação",
        events=events,
        snapshots=snapshots,
        error=error,
        notice=notice,
        preview=preview,
        preview_token=preview_token,
        restore_confirmation=RESTORE_CONFIRMATION,
        rollback_confirmation=ROLLBACK_CONFIRMATION,
        max_backup_bytes=MAX_BACKUP_BYTES,
    )


async def _uploaded_backup(form) -> dict[str, Any]:
    upload = form.get("backup_file") or form.get("backup")
    # FastAPI and Starlette can expose distinct UploadFile re-export classes;
    # the contract needed here is only an async read method.
    if upload is None or not hasattr(upload, "read"):
        raise BackupValidationError("Selecione um arquivo JSON de backup.")
    raw = await upload.read(MAX_BACKUP_BYTES + 1)
    # parse_backup_json enforces the same limit even if the client omits a size.
    return parse_backup_json(raw)


@router.get("/api/family-sync")
def api_family_sync():
    return JSONResponse(family_sync_state(), headers={"Cache-Control": "private, no-store"})


@router.get("/activity", response_class=HTMLResponse)
def activity_page(request: Request):
    notice = "A alteração foi desfeita." if request.query_params.get("undone") == "1" else ""
    return _activity_page(request, notice=notice)


@router.get("/recovery", response_class=HTMLResponse)
def recovery_page(request: Request):
    notice = "Restauração concluída; o snapshot anterior está disponível para reversão." if request.query_params.get("restored") == "1" else ""
    return _activity_page(request, notice=notice)


@router.post("/activity/{event_id}/undo")
@router.post("/recovery/events/{event_id}/undo")
async def undo_activity(request: Request, event_id: int):
    await checked_form(request)
    with get_session_factory()() as db:
        try:
            original = undo_activity_event(db, event_id)
            record_activity_event(db, "undo", "recovery", str(event_id), None,
                                  {"entity_type": original.entity_type}, undo_window=timedelta(0))
            db.commit()
        except UndoExpiredError as exc:
            db.rollback()
            return _activity_page(request, error=str(exc))
        except RecoveryError as exc:
            db.rollback()
            return _activity_page(request, error=str(exc))
    return RedirectResponse("/activity?undone=1", status_code=303)


@router.post("/recovery/preview", response_class=HTMLResponse)
async def recovery_preview(request: Request):
    form = await checked_form(request)
    try:
        payload = await _uploaded_backup(form)
        counts = backup_counts(payload)
        digest = backup_digest(payload)
        session = getattr(request.state, "family_session", None) or {}
        token = sign_review({"kind": "recovery-preview", "digest": digest, "csrf": session.get("csrf", "")})
        return _activity_page(request, preview=counts, preview_token=token)
    except BackupValidationError as exc:
        return _activity_page(request, error=str(exc))


@router.post("/recovery/restore")
async def recovery_restore(request: Request):
    form = await checked_form(request)
    if str(form.get("confirmation", "")).strip() != RESTORE_CONFIRMATION:
        raise HTTPException(status_code=400, detail=f"Digite exatamente: {RESTORE_CONFIRMATION}.")
    try:
        payload = await _uploaded_backup(form)
        digest = backup_digest(payload)
    except BackupValidationError as exc:
        return _activity_page(request, error=str(exc))
    session = getattr(request.state, "family_session", None) or {}
    review = read_review(str(form.get("preview_token", "")))
    if not review or review.get("kind") != "recovery-preview" or review.get("digest") != digest or review.get("csrf") != session.get("csrf", ""):
        raise HTTPException(status_code=400, detail="A prévia expirou ou não corresponde ao arquivo enviado.")
    with get_session_factory()() as db:
        try:
            restore_backup(db, payload)
            record_activity_event(db, "restore", "backup", "family", None,
                                  {"transactions": backup_counts(payload)["transactions"]}, undo_window=timedelta(0))
            db.commit()
        except (BackupValidationError, RecoveryError) as exc:
            db.rollback()
            return _activity_page(request, error=str(exc))
        except Exception:
            db.rollback()
            # Do not expose database/driver details, payloads, or credentials.
            return _activity_page(request, error="Não foi possível concluir a restauração; nenhum dado foi substituído.")
    return RedirectResponse("/recovery?restored=1", status_code=303)


@router.post("/recovery/snapshots/{snapshot_id}/rollback")
@router.post("/recovery/rollback/{snapshot_id}")
async def recovery_rollback(request: Request, snapshot_id: int):
    form = await checked_form(request)
    if str(form.get("confirmation", "")).strip() != ROLLBACK_CONFIRMATION:
        raise HTTPException(status_code=400, detail=f"Digite exatamente: {ROLLBACK_CONFIRMATION}.")
    with get_session_factory()() as db:
        try:
            rollback_restore(db, snapshot_id)
            record_activity_event(db, "restore", "backup", "family", None,
                                  {"rolled_back_snapshot": snapshot_id}, undo_window=timedelta(0))
            db.commit()
        except RecoveryError as exc:
            db.rollback()
            return _activity_page(request, error=str(exc))
        except Exception:
            db.rollback()
            return _activity_page(request, error="Não foi possível reverter a restauração; nenhum dado foi substituído.")
    return RedirectResponse("/recovery?restored=1", status_code=303)

"""Rotas privadas para ajustar o valor de uma única competência."""
from __future__ import annotations

from urllib.parse import urlencode
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.database import get_session_factory
from app.repository import read_snapshot
from app.routes import _nav_context, _return_url, _transaction_return_target, checked_form, render
from app.services.entry_review import read_review, sign_review
from app.services.finance_core import CARD_TYPES, month_key, month_start
from app.services.occurrence_edit import (
    ClosedOccurrenceError,
    ConcurrentOccurrenceError,
    OCCURRENCE_TYPES,
    OccurrenceEditError,
    PaidOccurrenceConfirmationRequired,
    edit_occurrence,
    occurrence_target_is_paid,
    occurrence_fingerprint,
    _card_invoice_line_amount,
    parse_occurrence_amount,
    plan_occurrence_split,
)

router = APIRouter()
occurrence_router = router
occurrence_edit_router = router


def _target_from_request(request: Request) -> tuple[str, Any]:
    raw = request.query_params.get("month") or _nav_context(request).get("month")
    try:
        target = month_start(raw)
    except (TypeError, ValueError):
        target = None
    if target is None:
        raise HTTPException(status_code=400, detail="Informe uma competência válida.")
    return month_key(target), target


def _find_row(txs: list[dict[str, Any]], tx_id: int) -> dict[str, Any]:
    row = next((item for item in txs if int(item.get("id") or 0) == int(tx_id) and not item.get("archived")), None)
    if row is None:
        raise HTTPException(status_code=404, detail="Lançamento não encontrado.")
    return row


def _assert_supported_and_active(row: dict[str, Any], target: Any) -> None:
    if row.get("type") not in OCCURRENCE_TYPES:
        raise HTTPException(status_code=400, detail="Este tipo não aceita ajuste por competência.")
    if row.get("type") in CARD_TYPES and int(row.get("card_flag") or 0) != 1:
        raise HTTPException(status_code=400, detail="Ajuste parcela a parcela exige uma compra vinculada a cartão.")
    start = month_start(row.get("start_month"))
    end = month_start(row.get("end_month")) if row.get("end_month") else None
    if start is None or target < start or (end is not None and target > end):
        raise HTTPException(status_code=400, detail="A competência escolhida não pertence a esta recorrência.")


def _version(request: Request, row: dict[str, Any], target_month: str) -> str:
    session = getattr(request.state, "family_session", None) or {}
    return sign_review({
        "kind": "occurrence-version", "tx_id": int(row["id"]), "month": target_month,
        "fingerprint": occurrence_fingerprint(row), "csrf": session.get("csrf"),
    })


def _version_payload(request: Request, form: Any, tx_id: int) -> tuple[dict[str, Any], str]:
    token = read_review(str(form.get("_entry_version", "")))
    session = getattr(request.state, "family_session", None) or {}
    if (
        not token or token.get("kind") != "occurrence-version"
        or token.get("tx_id") != int(tx_id)
        or token.get("csrf") != session.get("csrf")
        or not token.get("month")
        or not token.get("fingerprint")
    ):
        raise HTTPException(status_code=409, detail="Esta competência foi alterada ou a edição expirou. Reabra a tela para conferir a versão mais recente.")
    return token, str(token["month"])


def _old_plan(row: dict[str, Any]) -> dict[str, Any]:
    start = month_start(row.get("start_month"))
    end = month_start(row.get("end_month")) if row.get("end_month") else None
    return {"start": start, "end": end, "open": end is None}


def _render_occurrence(
    request: Request,
    row: dict[str, Any],
    target: Any,
    *,
    entry_version: str,
    error: str = "",
    amount_value: Any = None,
    base_amount: Any = None,
    status_code: int = 200,
    target_closed: bool = False,
    target_paid: bool = False,
    form_confirm_paid: bool = False,
):
    if amount_value in (None, ""):
        amount_value = row.get("amount", "")
    card_adjustment = row.get("type") in CARD_TYPES and int(row.get("card_flag") or 0) == 1
    allow_negative_amount = row.get("type") == "Crédito à vista" and int(row.get("card_flag") or 0) == 1
    if card_adjustment:
        plan = ()
        if base_amount is None:
            current_snapshot = read_snapshot_from_request()
            base_amount = _card_invoice_line_amount(current_snapshot, int(row["id"]), target, ignore_target_override=True)
    else:
        plan = plan_occurrence_split(row, target, row.get("amount", 0))
        base_amount = row.get("amount", 0)
    target_key = month_key(target)
    override_exists = target_key in (row.get("installment_overrides") or {})
    return_path, return_filters = _transaction_return_target(request.query_params)
    cancel_url = _return_url(request, return_path, **return_filters)
    response = render(
        request, "occurrence_edit.html", "Ajustar competência",
        row=row, target_month=target, target_month_key=month_key(target),
        entry_version=entry_version, error=error, amount_value=amount_value,
        old_plan=_old_plan(row), split_preview=plan, target_closed=target_closed,
        target_paid=target_paid, form_confirm_paid=form_confirm_paid,
        card_adjustment=card_adjustment, allow_negative_amount=allow_negative_amount,
        override_exists=override_exists, base_amount=base_amount,
        cancel_url=cancel_url, return_page=request.query_params.get("return_page", ""),
        return_card=request.query_params.get("return_card", ""), return_month=request.query_params.get("return_month", ""),
        occurrence_context_query=urlencode(dict(request.query_params)),
    )
    response.status_code = status_code
    return response


@router.get("/transactions/{tx_id}/occurrence", response_class=HTMLResponse)
def occurrence_edit_page(request: Request, tx_id: int):
    target_key, target = _target_from_request(request)
    txs, accounts, cards, lookups, settings, closed = read_snapshot_from_request()
    row = _find_row(txs, tx_id)
    _assert_supported_and_active(row, target)
    with get_session_factory()() as db:
        target_paid = occurrence_target_is_paid(
            db, tx_id, target, snapshot=(txs, accounts, cards, lookups, settings, closed),
        )
    return _render_occurrence(
        request, row, target, entry_version=_version(request, row, target_key),
        amount_value=_card_invoice_line_amount((txs, accounts, cards, lookups, settings, closed), tx_id, target)
        if row.get("type") in CARD_TYPES and int(row.get("card_flag") or 0) == 1 else row.get("amount", ""),
        base_amount=_card_invoice_line_amount((txs, accounts, cards, lookups, settings, closed), tx_id, target, ignore_target_override=True)
        if row.get("type") in CARD_TYPES and int(row.get("card_flag") or 0) == 1 else row.get("amount", ""),
        target_closed=bool(closed.get(target_key)), target_paid=target_paid,
    )


def read_snapshot_from_request():
    # Kept as a tiny seam for route tests and to make the GET path use the same
    # fresh snapshot as the existing transaction pages.
    with get_session_factory()() as db:
        return read_snapshot(db)


@router.post("/transactions/{tx_id}/occurrence")
async def occurrence_edit_submit(request: Request, tx_id: int):
    form = await checked_form(request)
    token, target_key = _version_payload(request, form, tx_id)
    try:
        target = month_start(target_key)
    except (TypeError, ValueError):
        target = None
    if target is None:
        raise HTTPException(status_code=409, detail="A competência assinada não é válida. Reabra a edição.")

    # The signed token defines the occurrence. The global context remains useful
    # for navigation, but cannot be used to retarget a submission.
    with get_session_factory()() as db:
        snapshot = read_snapshot(db)
        row = _find_row(snapshot[0], tx_id)
        _assert_supported_and_active(row, target)
        if occurrence_fingerprint(row) != token.get("fingerprint"):
            raise HTTPException(status_code=409, detail="O lançamento mudou durante a edição; atualize a página antes de salvar.")
        amount_raw = form.get("amount", form.get("total_amount", ""))
        reset_override = str(form.get("reset_override", "")) == "yes"
        allow_negative_amount = row.get("type") == "Crédito à vista" and int(row.get("card_flag") or 0) == 1
        try:
            new_amount = None if reset_override else parse_occurrence_amount(amount_raw, allow_signed=allow_negative_amount)
            result = edit_occurrence(
                db, tx_id, target, new_amount, expected_before=row,
                confirm_paid_change=str(form.get("confirm_paid_change", "")) in {"yes", "on", "1"},
                reset_override=reset_override,
            )
            db.commit()
        except ConcurrentOccurrenceError as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except ClosedOccurrenceError as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except PaidOccurrenceConfirmationRequired as exc:
            db.rollback()
            page = _render_occurrence(
                request, row, target, entry_version=str(form.get("_entry_version", "")),
                error=str(exc), amount_value=amount_raw,
                target_closed=False,
                target_paid=occurrence_target_is_paid(db, tx_id, target, snapshot=snapshot),
                form_confirm_paid=str(form.get("confirm_paid_change", "")) in {"yes", "on", "1"},
            )
            return page
        except (OccurrenceEditError, TypeError, ValueError) as exc:
            db.rollback()
            page = _render_occurrence(
                request, row, target, entry_version=str(form.get("_entry_version", "")),
                error=str(exc), amount_value=amount_raw,
                target_closed=False,
                target_paid=occurrence_target_is_paid(db, tx_id, target, snapshot=snapshot),
                form_confirm_paid=str(form.get("confirm_paid_change", "")) in {"yes", "on", "1"},
            )
            return page

    # Occurrence edits are audit-only; retain the source screen after save.
    return_path, return_filters = _transaction_return_target(form)
    return RedirectResponse(_return_url(request, return_path, form, saved="1", **return_filters), status_code=303)

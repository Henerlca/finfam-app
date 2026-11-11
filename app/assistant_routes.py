from __future__ import annotations

import asyncio
import html
import json
from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app.auth import verify_csrf
from app.database import get_session_factory
from app.repository import read_snapshot
from app.services.assistant_agent import (
    AIAssistantError,
    MAX_QUESTION_CHARS,
    ask_finance_proposals,
    assistant_agent_available,
    sanitize_history,
)
from app.services.assistant_proposals import validate_transaction_payload
from app.services.entry_review import read_review, sign_review, values_digest
from app.services.finance_ai_data import build_financial_summary
from app.services.horizon import effective_end
from app.services.finance_core import FAMILY, MEMBERS, month_from_id, month_id, month_start

# These helpers are deliberately reused from the normal transaction flow. They render
# and validate only; this router never calls insert/update/delete repository functions.
from app.routes import _forecast, _form_context, _nav_context, _transaction_from_form, checked_form

router = APIRouter()
MAX_CHAT_BODY_BYTES = 20 * 1024
_RECURRING_TYPES = {"Receita", "Fixo", "Reserva", "Caixinha", "Saque Caixinha"}
_CARD_TYPES = {"Crédito à vista", "Parcelado"}


def _json_error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"detail": message}, status_code=status, headers={"Cache-Control": "no-store"})


def _session(request: Request) -> dict[str, Any]:
    return getattr(request.state, "family_session", None) or {}


def _csrf_from_json(request: Request, payload: dict[str, Any]) -> str:
    return str(payload.get("_csrf") or payload.get("csrf") or request.headers.get("X-CSRF-Token") or "")


def _safe_payload(request: Request, payload: Any) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    if not isinstance(payload, dict):
        return None, _json_error("Envie um objeto JSON válido.")
    session = _session(request)
    if not verify_csrf(session.get("csrf"), _csrf_from_json(request, payload)):
        return None, _json_error("Sessão ou CSRF expirado. Atualize a página.", 403)
    return payload, None


def _reference_maps(txs: list[dict[str, Any]], accounts: list[dict[str, Any]], cards: list[dict[str, Any]], lookups: dict[str, list[str]]) -> dict[str, dict[str, str]]:
    def pseudonyms(values: list[str], prefix: str) -> dict[str, str]:
        return {value: f"{prefix}:{index}" for index, value in enumerate(sorted({str(value).strip() for value in values if str(value).strip()}, key=str.casefold), 1)}
    pot_names = [str(tx.get("linked_pot") or "") for tx in txs]
    pot_names += [str(tx.get("description") or "") for tx in txs if tx.get("type") == "Reserva"]
    return {
        "accounts": pseudonyms([str(row.get("name", "")) for row in accounts], "account"),
        "cards": pseudonyms([str(row.get("name", "")) for row in cards], "card"),
        "members": pseudonyms([*MEMBERS, FAMILY, *(str(value) for value in lookups.get("membro", []))], "member"),
        "pots": pseudonyms([value for value in pot_names if value], "pot"),
    }


def _closed_origin(original: dict[str, Any] | None, closed: dict[str, bool]) -> bool:
    """Return whether an edit would touch at least one reconciled competence."""
    if not original or not closed:
        return False
    start = month_id(month_start(original.get("start_month") or original.get("transaction_date")))
    if start is None:
        return False
    end = month_id(month_start(original.get("end_month"))) if original.get("end_month") else None
    for key, is_closed in closed.items():
        if not is_closed:
            continue
        index = month_id(month_start(key))
        if index is not None and index >= start and (end is None or index <= end):
            return True
    return False


def _closed_edit_message() -> str:
    return (
        "Este lançamento afeta uma competência fechada. Reabra o mês antes de incluir, editar ou excluir; "
        "os valores conciliados não foram alterados."
    )


def _reverse(ref_map: dict[str, str], ref: Any, *, nullable: bool = True) -> str:
    if ref is None and nullable:
        return ""
    return next((real for real, pseudonym in ref_map.items() if pseudonym == ref), "")


def _form_values(action: dict[str, Any], maps: dict[str, dict[str, str]], original: dict[str, Any] | None = None) -> dict[str, Any] | None:
    transaction = action.get("transaction")
    if not isinstance(transaction, dict):
        return None
    account_ref = str(transaction.get("account_ref") or "")
    account = _reverse(maps["accounts"], account_ref) or _reverse(maps["cards"], account_ref)
    member_ref = transaction.get("member_ref")
    member = _reverse(maps["members"], member_ref, nullable=False) if member_ref is not None else ""
    linked_pot_ref = transaction.get("linked_pot_ref")
    linked_pot = _reverse(maps["pots"], linked_pot_ref) if linked_pot_ref is not None else ""
    destination_ref = transaction.get("destination_ref")
    destination = _reverse(maps["accounts"], destination_ref) if destination_ref is not None else ""
    if (
        not account
        or not member
        or (member_ref is not None and not member)
        or (linked_pot_ref is not None and not linked_pot)
        or (destination_ref is not None and not destination)
    ):
        return None
    tx_type = transaction.get("type")
    repeat_end = transaction.get("repeat_end_month") or ""
    first_bill = transaction.get("first_bill_month") or ""
    if not first_bill and original and tx_type in _CARD_TYPES and original.get("type") in _CARD_TYPES:
        first_bill = original.get("start_month") or ""
    preserve_open_repeat = bool(
        original
        and original.get("type") in _RECURRING_TYPES
        and original.get("end_month") is None
        and tx_type in _RECURRING_TYPES
    )
    form_values: dict[str, Any] = {
        "transaction_date": transaction.get("transaction_date") or "",
        "type": tx_type or "",
        "description": transaction.get("description") or "",
        "category": transaction.get("category") or "",
        "member": member,
        "account_card": account,
        "total_amount": transaction.get("total_amount"),
        "installment_count": transaction.get("installments") or 1,
        "first_bill_month": first_bill,
        "destination_account": destination,
        "linked_pot": linked_pot,
        "repeat_monthly": "on" if tx_type in _RECURRING_TYPES and (repeat_end or preserve_open_repeat) else "",
        "end_month": repeat_end,
    }
    if original:
        if not transaction.get("transaction_date") and not original.get("transaction_date"):
            return None
        merged = dict(original)
        merged.update(form_values)
        merged["id"] = original.get("id")
        if not transaction.get("transaction_date"):
            merged["transaction_date"] = original["transaction_date"]
        form_values = merged
    try:
        normalized = _transaction_from_form(form_values)
    except (TypeError, ValueError):
        return None
    normalized["card_flag"] = int(account in maps["cards"])
    # The review form must show the exact normalized competence, not an empty
    # card field or a date-derived default.  An open series keeps its original
    # origin and remains open; merely opening the review cannot move it.
    if preserve_open_repeat and original and original.get("start_month"):
        normalized["start_month"] = original.get("start_month")
        normalized["end_month"] = None
    form_values["start_month"] = normalized.get("start_month") or form_values.get("start_month", "")
    form_values["end_month"] = normalized.get("end_month")
    form_values["first_bill_month"] = str(normalized.get("start_month") or form_values.get("first_bill_month") or "")[:7]
    form_values["_normalized"] = normalized
    return form_values


def _transaction_by_ref(action: dict[str, Any], txs: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, int | None]:
    ref = action.get("transaction_ref")
    if not isinstance(ref, str) or not ref.startswith("tx:"):
        return None, None
    try:
        tx_id = int(ref.split(":", 1)[1])
    except (TypeError, ValueError):
        return None, None
    return next((row for row in txs if int(row.get("id") or 0) == tx_id and not row.get("archived")), None), tx_id


def _forecast_preview(action: dict[str, Any], txs: list[dict[str, Any]], accounts: list[dict[str, Any]], settings: dict[str, Any], closed: dict[str, bool], form_values: dict[str, Any], original: dict[str, Any] | None) -> list[dict[str, Any]]:
    normalized = dict(form_values.get("_normalized") or {})
    target_id = int(original["id"]) if original and original.get("id") is not None else -1
    normalized["id"] = target_id
    normalized["archived"] = False
    draft_txs = []
    for row in txs:
        if original and int(row.get("id") or 0) == target_id:
            draft_txs.append(normalized)
        else:
            draft_txs.append(row)
    if not original:
        draft_txs.append(normalized)
    try:
        # Resolve one horizon from the draft before calculating either side.
        # Otherwise a new transaction in (for example) 2035 makes ``changed``
        # longer than ``base`` and the old positional zip silently displayed
        # zero as the base balance.
        same_end = effective_end(draft_txs, settings)
        base = _forecast(txs, accounts, settings, closed, requested=same_end)
        changed = _forecast(draft_txs, accounts, settings, closed, requested=same_end)
    except (TypeError, ValueError, OverflowError):
        return []
    start = month_start(normalized.get("start_month"))
    start_idx = month_id(start)
    if start_idx is None:
        return []
    # The preview explains downstream impact, not only the installments or
    # repeat end.  Keep it bounded to 24 competencies from the start.
    end_idx = start_idx + 23
    base_by_month = {
        month_id(row.get("month")): row
        for row in base
        if month_id(row.get("month")) is not None
    }
    rows: list[dict[str, Any]] = []
    for changed_row in changed:
        month_idx = month_id(changed_row.get("month"))
        if month_idx is None or month_idx < start_idx or month_idx > end_idx:
            continue
        base_row = base_by_month.get(month_idx)
        if base_row is None:
            # Never manufacture a base value when horizons unexpectedly differ.
            continue
        rows.append({"month": str(changed_row.get("month"))[:7], "base_balance": round(float(base_row.get("projected_ending_balance", 0) or 0), 2), "changed_balance": round(float(changed_row.get("projected_ending_balance", 0) or 0), 2), "base_budget": round(float(base_row.get("monthly_balance_with_prior_impact", 0) or 0), 2), "changed_budget": round(float(changed_row.get("monthly_balance_with_prior_impact", 0) or 0), 2)})
        if len(rows) >= 24:
            break
    return rows


def _attach_reviews(result: dict[str, Any], request: Request, summary: dict[str, Any], txs: list[dict[str, Any]], accounts: list[dict[str, Any]], cards: list[dict[str, Any]], lookups: dict[str, list[str]], settings: dict[str, Any], closed: dict[str, bool]) -> dict[str, Any]:
    maps = _reference_maps(txs, accounts, cards, lookups)
    response = {"answer": str(result.get("answer") or "")[:7000], "proposals": []}
    session_csrf = _session(request).get("csrf")
    catalog_refs = {str(item.get("ref")) for item in (summary.get("transaction_catalog") or []) if isinstance(item, dict)}
    for proposal in result.get("proposals") or []:
        if not isinstance(proposal, dict) or not isinstance(proposal.get("action"), dict):
            continue
        action = proposal["action"]
        kind = action.get("kind")
        original, tx_id = _transaction_by_ref(action, txs)
        if kind == "edit_transaction" and (original is None or f"tx:{tx_id}" not in catalog_refs):
            continue
        form_values = None
        preview: list[dict[str, Any]] = []
        if kind in {"create_transaction", "edit_transaction"}:
            form_values = _form_values(action, maps, original)
            if form_values is None:
                continue
            preview = _forecast_preview(action, txs, accounts, settings, closed, form_values, original)
        safe = {key: proposal.get(key) for key in ("title", "evidence", "period", "months", "uncertainty", "limitations", "before", "after", "impact")}
        safe.update({"action": action, "preview": preview})
        if form_values:
            safe["entry_summary"] = {key: form_values.get(key) for key in
                ("type", "description", "category", "member", "account_card", "total_amount", "transaction_date", "first_bill_month", "installment_count")}
        if kind in {"create_transaction", "edit_transaction"}:
            if kind == "edit_transaction" and _closed_origin(original, closed):
                # Do not offer a dead CTA: reconciled history is immutable from
                # this workflow; the safe alternative is a future complement.
                safe["review_note"] = _closed_edit_message()
            else:
                fingerprint = values_digest(original) if original else ""
                token_payload = {
                    "kind": "general-ai-review", "purpose": "general-ai-review", "action": action,
                    "fingerprint": fingerprint, "original_fingerprint": fingerprint,
                    "tx_id": tx_id, "csrf": session_csrf,
                }
                signed = sign_review(token_payload)
                safe.update({"signed_token": signed, "review_token": signed})
                safe["review_note"] = "Nada foi gravado. A revisão abrirá o formulário normal, com todos os campos editáveis."
        else:
            # General scenario/reallocation proposals have no compatible form
            # here; keeping them informational avoids emitting an unusable CTA.
            safe["review_note"] = "Simulação informativa — nada foi gravado neste fluxo."
        response["proposals"].append(safe)
    return response


def _append_review_preview(response: Response, note: str, preview: list[dict[str, Any]]) -> Response:
    """Keep the normal transaction form while visibly attaching the read-only impact."""
    try:
        raw_body = response.body
        body = raw_body.tobytes().decode("utf-8") if isinstance(raw_body, memoryview) else bytes(raw_body).decode("utf-8")
    except (AttributeError, TypeError, UnicodeDecodeError):
        return response
    rows = "".join(
        "<tr><th scope=\"row\">{month}</th><td>{base:.2f}</td><td>{changed:.2f}</td><td>{delta:+.2f}</td></tr>".format(
            month=html.escape(str(row.get("month", ""))),
            base=float(row.get("base_balance", 0) or 0),
            changed=float(row.get("changed_balance", 0) or 0),
            delta=float(row.get("changed_balance", 0) or 0) - float(row.get("base_balance", 0) or 0),
        ) for row in preview
    )
    extra = "<section class=\"panel assistant-review-preview\"><p class=\"eyebrow\">Revisão da proposta</p><p>" + html.escape(note) + "</p>"
    if rows:
        extra += "<p><strong>Impacto mensal simulado (sem gravação):</strong></p><div class=\"table-scroll\"><table><thead><tr><th>Mês</th><th>Saldo base</th><th>Saldo com proposta</th><th>Variação</th></tr></thead><tbody>" + rows + "</tbody></table></div>"
    extra += "</section>"
    marker = "</main>"
    if marker in body:
        body = body.replace(marker, extra + marker, 1)
    response.body = body.encode("utf-8")
    response.headers["content-length"] = str(len(response.body))
    return response


@router.post("/api/assistente/chat")
async def assistant_chat(request: Request):
    # Check the declared size before touching ``request.json`` and verify the
    # actual bytes as well for clients that omit Content-Length.
    raw_length = request.headers.get("content-length")
    try:
        if raw_length is not None and int(raw_length) > MAX_CHAT_BODY_BYTES:
            return _json_error("A mensagem excede o limite seguro de 20 KB.", 413)
    except (TypeError, ValueError):
        return _json_error("Tamanho de requisição inválido.", 400)
    try:
        raw_body = await request.body()
    except (ValueError, TypeError, UnicodeError):
        return _json_error("Envie JSON válido.")
    if len(raw_body) > MAX_CHAT_BODY_BYTES:
        return _json_error("A mensagem excede o limite seguro de 20 KB.", 413)
    try:
        body = json.loads(raw_body.decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError):
        return _json_error("Envie JSON válido.")
    payload, error = _safe_payload(request, body)
    if error is not None:
        return error
    assert payload is not None
    question = str(payload.get("question") or "").strip()
    if not question or len(question) > MAX_QUESTION_CHARS:
        return _json_error(f"Escreva uma pergunta com até {MAX_QUESTION_CHARS} caracteres.")
    if payload.get("ai_consent") is not True and payload.get("consent") is not True:
        return _json_error("Autorize o processamento da pergunta e do resumo financeiro.")
    if not assistant_agent_available():
        return _json_error("A análise por IA não está habilitada neste ambiente.", 503)
    with get_session_factory()() as db:
        txs, accounts, cards, lookups, settings, closed = read_snapshot(db)
        protected = [*MEMBERS, FAMILY, *(str(row.get("name", "")) for row in accounts), *(str(row.get("name", "")) for row in cards), *(str(value) for value in lookups.get("membro", []))]
    # The browser may ask about the screen's selected competence/person without
    # exposing human names to the model.  _nav_context applies the same bounds
    # as the regular pages (invalid values safely fall back to defaults).
    nav = _nav_context(request, {
        "_context_month": payload.get("month") or payload.get("_context_month"),
        "_context_person": payload.get("person") or payload.get("_context_person"),
        "_context_horizon": payload.get("horizon") or payload.get("_context_horizon"),
    })
    member_ref = "family"
    if nav["person"] != FAMILY:
        member_ref = _reference_maps([], [], [], lookups)["members"].get(nav["person"], "family")
    view_context = {"month": nav["month"], "member_ref": member_ref, "chart_horizon": nav["horizon"]}
    preferences = payload.get("preferences") if "preferences" in payload else settings.get("assistant_preferences")
    if not isinstance(preferences, dict):
        preferences = {}
    history = sanitize_history(payload.get("history"), protected)
    context_prefix = f"Contexto da tela: competência {nav['month']}; pergunta: "
    contextual_question = context_prefix + question
    if len(contextual_question) > MAX_QUESTION_CHARS:
        contextual_question = context_prefix + question[: max(0, MAX_QUESTION_CHARS - len(context_prefix))]

    def summary_provider(months: int, *, include_descriptions: bool = False):
        with get_session_factory()() as db:
            summary = build_financial_summary(db, months, include_descriptions=include_descriptions)
            summary["view_context"] = view_context
            return summary

    try:
        result = await asyncio.to_thread(ask_finance_proposals, contextual_question, summary_provider, protected_identifiers=protected, preferences=preferences, history=history, include_descriptions=True)
        # Build a fresh read-only snapshot after the model call for reference mapping and previews.
        with get_session_factory()() as db:
            txs, accounts, cards, lookups, settings, closed = read_snapshot(db)
            summary = build_financial_summary(db, 6, include_descriptions=True)
        return JSONResponse(_attach_reviews(result, request, summary, txs, accounts, cards, lookups, settings, closed), headers={"Cache-Control": "no-store"})
    except AIAssistantError as exc:
        return _json_error(str(exc), 503)
    except Exception:
        return _json_error("A análise não pôde ser concluída agora. Nenhuma alteração foi feita.", 503)


async def _review_action(request: Request):
    form = await checked_form(request)
    token = read_review(str(form.get("token") or form.get("review_token") or ""))
    if not token or token.get("csrf") != _session(request).get("csrf"):
        raise HTTPException(status_code=400, detail="Proposta expirada ou inválida. Gere uma nova análise.")
    if token.get("kind") == "ai-proposal":
        action = token.get("proposal", {}).get("action", {}) if isinstance(token.get("proposal"), dict) else {}
        if action.get("kind") == "scenario":
            params = {"amount": action.get("amount", 0), "scenario_month": action.get("start_month", ""), "months": action.get("duration", 1)}
            return RedirectResponse("/scenarios?" + "&".join(f"{key}={value}" for key, value in params.items()), status_code=303)
        raise HTTPException(status_code=400, detail="A proposta legada não é revisável neste fluxo.")
    if token.get("kind") != "general-ai-review":
        raise HTTPException(status_code=400, detail="Proposta inválida.")
    action = token.get("action")
    if not isinstance(action, dict) or action.get("kind") not in {"create_transaction", "edit_transaction"}:
        raise HTTPException(status_code=400, detail="Esta ação não abre um lançamento.")
    with get_session_factory()() as db:
        txs, accounts, cards, lookups, settings, closed = read_snapshot(db)
    original, tx_id = _transaction_by_ref(action, txs)
    if action.get("kind") == "edit_transaction":
        expected = str(token.get("original_fingerprint") or token.get("fingerprint") or "")
        if original is None or not expected or values_digest(original) != expected:
            raise HTTPException(status_code=409, detail="O lançamento mudou ou foi arquivado. Gere uma nova análise.")
        if _closed_origin(original, closed):
            raise HTTPException(status_code=409, detail=_closed_edit_message())
    maps = _reference_maps(txs, accounts, cards, lookups)
    summary: dict[str, Any] = {"transaction_catalog": [{"ref": f"tx:{row.get('id')}"} for row in txs if not row.get("archived")]}
    # validate_transaction_payload only needs the local reference maps; the route then maps
    # refs to current names and sends the result through the normal form validator.
    summary["reference_maps"] = maps
    if action.get("kind") == "edit_transaction":
        action["transaction_ref"] = f"tx:{tx_id}"
    if validate_transaction_payload(action.get("transaction"), summary, kind=action.get("kind")) is None:
        raise HTTPException(status_code=400, detail="As referências da proposta não estão mais disponíveis.")
    values = _form_values(action, maps, original)
    if values is None:
        raise HTTPException(status_code=400, detail="Não foi possível montar a prévia editável.")
    preview = _forecast_preview(action, txs, accounts, settings, closed, values, original)
    note = "Proposta da IA — nada foi gravado. Revise e ajuste os campos; o salvamento usa o fluxo normal."
    initial = dict(values)
    initial.pop("_normalized", None)
    # The regular POST handler can verify this original signed review token at
    # save time, closing the race between opening the form and submitting it.
    initial["_ai_review_token"] = str(form.get("token") or form.get("review_token") or "")
    initial["assistant_review_note"] = note
    if original:
        initial["id"] = original.get("id")
    context = _form_context(
        request,
        "Editar lançamento" if original else "Novo lançamento",
        initial,
        "",
        assistant_review=True,
        assistant_info=note,
        proposal_preview=preview,
        editing_original=original,
    )
    return _append_review_preview(context, note, preview)


@router.post("/assistente/acao/revisar")
async def assistant_review(request: Request):
    return await _review_action(request)


# Compatibility endpoint for signed proposals generated by the previous assistant page.
@router.post("/assistente/revisar")
async def assistant_review_legacy_path(request: Request):
    return await _review_action(request)

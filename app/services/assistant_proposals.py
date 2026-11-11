"""Pure validation for read-only assistant proposals.

The assistant may suggest actions, but this module never writes data and never trusts
human-readable account/member names from a model. References must be present in the
locally-built pseudonymized catalog and are resolved by the route at review time.
"""
from __future__ import annotations

import json
import math
import re
from datetime import date, datetime
from typing import Any

from app.services.finance_core import month_id, month_start

MAX_PROPOSALS = 2
MAX_SCENARIO_DURATION = 60
MAX_INSTALLMENTS = 120
MAX_EVIDENCE_MONTHS = 240
MAX_TEXT = 1200
_ACTION_KINDS = {"scenario", "reallocate", "create_transaction", "edit_transaction"}
_SCENARIO_KINDS = {"income", "expense", "savings"}
_TRANSACTION_TYPES = {"Receita", "Fixo", "Débito/Pix", "Crédito à vista", "Parcelado", "Reserva", "Caixinha", "Saque Caixinha", "Transferência"}
_REJECTED_ACTION_WORDS = ("investimento", "investir", "ações", "acoes", "cripto", "criptomoeda", "fundos", "renda fixa", "tesouro", "ação", "acao")
_PROTECTED_CUT_WORDS = ("cortar", "reduzir", "diminuir", "retirar", "sacrificar", "cancelar", "zerar", "eliminar", "corte", "redução", "reducao")
_MONTH_RE = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})(?!\d)")
_REF_RE = re.compile(r"^(?:account|card|member|pot|category|tx):[1-9]\d*$")


def _nullable_string() -> dict[str, Any]:
    return {"type": ["string", "null"]}


def _nullable_number() -> dict[str, Any]:
    return {"type": ["number", "null"]}


def proposal_json_schema() -> dict[str, Any]:
    """Strict schema accepted by the configured LLM when response_format is available."""
    transaction = {
        "type": "object",
        "properties": {
            "account_ref": _nullable_string(), "member_ref": _nullable_string(), "category": _nullable_string(),
            "description": _nullable_string(), "type": _nullable_string(), "transaction_date": _nullable_string(),
            "first_bill_month": _nullable_string(), "total_amount": _nullable_number(), "installments": {"type": ["integer", "null"]},
            "repeat_end_month": _nullable_string(), "linked_pot_ref": _nullable_string(), "destination_ref": _nullable_string(),
        },
        "required": ["account_ref", "member_ref", "category", "description", "type", "transaction_date", "first_bill_month", "total_amount", "installments", "repeat_end_month", "linked_pot_ref", "destination_ref"],
        "additionalProperties": False,
    }
    action = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["scenario", "reallocate", "create_transaction", "edit_transaction"]},
            "scenario_kind": {"type": "string", "enum": ["income", "expense", "savings", "none"]},
            "amount": {"type": "number"}, "start_month": _nullable_string(), "duration": {"type": "integer"},
            "target_group": {"type": "string", "enum": ["Gasolina", "none"]}, "source_group": {"type": "string", "enum": ["Lazer", "Lazer e escolhas", "none"]},
            "transaction_ref": _nullable_string(), "transaction": {"anyOf": [transaction, {"type": "null"}]},
        },
        "required": ["kind", "scenario_kind", "amount", "start_month", "duration", "target_group", "source_group", "transaction_ref", "transaction"],
        "additionalProperties": False,
    }
    proposal = {"type": "object", "properties": {"title": {"type": "string"}, "evidence": {"type": "string"}, "period": {"type": "string"}, "months": {"type": "integer"}, "uncertainty": {"type": "string"}, "limitations": {"type": "string"}, "before": {"type": "number"}, "after": {"type": "number"}, "impact": {"type": "string"}, "action": action}, "required": ["title", "evidence", "period", "months", "uncertainty", "limitations", "before", "after", "impact", "action"], "additionalProperties": False}
    return {"type": "object", "properties": {"answer": {"type": "string"}, "proposals": {"type": "array", "items": proposal, "maxItems": MAX_PROPOSALS}}, "required": ["answer", "proposals"], "additionalProperties": False}


def _clean_text(value: Any, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.replace("\x00", "").split())[:limit].strip()


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _month(value: Any) -> date | None:
    try:
        return month_start(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _month_pair(period: Any) -> tuple[date, date] | None:
    parsed: list[date] = []
    for year, month in _MONTH_RE.findall(str(period or ""))[:2]:
        try:
            parsed.append(date(int(year), int(month), 1))
        except ValueError:
            return None
    if not parsed:
        return None
    if len(parsed) == 1:
        return parsed[0], parsed[0]
    return (parsed[0], parsed[1]) if parsed[0] <= parsed[1] else None


def _summary_bounds(summary: dict[str, Any]) -> tuple[date, date] | None:
    ranges: list[tuple[date, date]] = []
    for name in ("confirmed_sample", "history", "full_future_plan", "planning"):
        container = summary.get(name)
        if isinstance(container, dict):
            start, end = _month(container.get("from")), _month(container.get("to"))
            if start and end and start <= end:
                ranges.append((start, end))
    for key in ("projection_next_12_months", "planned_reserves_next_12_months"):
        rows = summary.get(key)
        if isinstance(rows, list):
            months = [_month(row.get("month")) for row in rows if isinstance(row, dict)]
            months = [item for item in months if item]
            if months:
                ranges.append((min(months), max(months)))
    if not ranges:
        return None
    return min(start for start, _ in ranges), max(end for _, end in ranges)


def _summary_has_data(summary: dict[str, Any]) -> bool:
    sample = summary.get("confirmed_sample")
    if isinstance(sample, dict) and (int(sample.get("months_available") or 0) > 0 or bool(sample.get("monthly")) or int(sample.get("confirmed_item_count") or 0) > 0):
        return True
    return any(isinstance(summary.get(key), (list, dict)) and bool(summary.get(key)) for key in ("history", "planning", "projection_next_12_months", "planned_reserves_next_12_months", "full_future_plan", "transaction_catalog"))


def _preference_fragments(preferences: dict[str, Any] | None) -> list[str]:
    value = preferences.get("protected_goals", "") if isinstance(preferences, dict) else ""
    return [part.casefold().strip() for part in re.split(r"[,;\n]", value) if isinstance(value, str) and part.strip()]


def _contains_unsupported(text: str) -> bool:
    lowered = text.casefold()
    # Match complete words/phrases only.  A substring check incorrectly rejects
    # harmless evidence such as ``simulação`` because it contains ``ação``.
    return any(re.search(rf"(?<!\w){re.escape(word)}(?!\w)", lowered) for word in _REJECTED_ACTION_WORDS)


def _is_protected_cut(text: str, preferences: dict[str, Any] | None) -> bool:
    lowered = text.casefold()
    return any(word in lowered for word in _PROTECTED_CUT_WORDS) and any(goal and goal in lowered for goal in _preference_fragments(preferences))


def _reference_sets(summary: dict[str, Any]) -> dict[str, set[str]]:
    maps = summary.get("reference_maps") if isinstance(summary.get("reference_maps"), dict) else {}
    result: dict[str, set[str]] = {}
    for name in ("accounts", "cards", "members", "pots", "categories"):
        values = maps.get(name) if isinstance(maps, dict) else None
        if isinstance(values, dict):
            result[name] = {str(value) for value in values.values()}
        elif isinstance(values, list):
            result[name] = {str(value) for value in values}
        else:
            result[name] = set()
    catalog = summary.get("transaction_catalog") or summary.get("catalog") or []
    for item in catalog if isinstance(catalog, list) else []:
        if not isinstance(item, dict):
            continue
        ref = str(item.get("ref") or "")
        if ref.startswith("tx:"):
            result.setdefault("transactions", set()).add(ref)
        for field, key in (("account_ref", "accounts"), ("card_ref", "cards"), ("member_ref", "members"), ("pot_ref", "pots"), ("category_ref", "categories")):
            if item.get(field):
                result.setdefault(key, set()).add(str(item[field]))
    return result


def validate_transaction_payload(raw: Any, summary: dict[str, Any], *, kind: str) -> dict[str, Any] | None:
    """Validate the strict transaction object and chosen references against the local catalog."""
    if not isinstance(raw, dict) or kind not in {"create_transaction", "edit_transaction"}:
        return None
    required = ("account_ref", "member_ref", "category", "description", "type", "transaction_date", "first_bill_month", "total_amount", "installments", "repeat_end_month", "linked_pot_ref", "destination_ref")
    if any(key not in raw for key in required):
        return None
    refs = _reference_sets(summary)
    account_ref, member_ref = raw.get("account_ref"), raw.get("member_ref")
    if not isinstance(account_ref, str) or account_ref not in refs.get("accounts", set()) | refs.get("cards", set()):
        return None
    if not isinstance(member_ref, str) or member_ref not in refs.get("members", set()):
        return None
    for field, group in (("linked_pot_ref", "pots"), ("destination_ref", "accounts")):
        value = raw.get(field)
        if value is not None and (not isinstance(value, str) or value not in refs.get(group, set())):
            return None
    tx_type, description, category = raw.get("type"), _clean_text(raw.get("description"), 255), _clean_text(raw.get("category"), 128)
    if tx_type not in _TRANSACTION_TYPES or not description:
        return None
    if any(ch in description for ch in ("\n", "\r", "\x00")):
        return None
    transaction_date = _month(raw.get("transaction_date"))
    try:
        exact = datetime.strptime(str(raw.get("transaction_date")), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    first_bill = _month(raw.get("first_bill_month")) if raw.get("first_bill_month") is not None else None
    end = _month(raw.get("repeat_end_month")) if raw.get("repeat_end_month") is not None else None
    if transaction_date is None or exact is None or (end and first_bill and end < first_bill):
        return None
    amount = _finite_number(raw.get("total_amount"))
    if amount is None or amount <= 0:
        return None
    installments = raw.get("installments")
    if installments is None:
        installments = 1
    if isinstance(installments, bool) or not isinstance(installments, int) or installments < 1 or installments > MAX_INSTALLMENTS or (tx_type == "Parcelado" and installments < 2):
        return None
    if tx_type == "Transferência" and raw.get("destination_ref") is None:
        return None
    if tx_type in {"Crédito à vista", "Parcelado"} and first_bill is None:
        return None
    if kind == "edit_transaction":
        tx_ref = raw.get("transaction_ref")
        if tx_ref is not None and (not isinstance(tx_ref, str) or tx_ref not in refs.get("transactions", set())):
            return None
    return {"account_ref": account_ref, "member_ref": member_ref, "category": category, "description": description, "type": tx_type, "transaction_date": exact.isoformat(), "first_bill_month": first_bill.strftime("%Y-%m") if first_bill else None, "total_amount": round(amount, 2), "installments": installments, "repeat_end_month": end.strftime("%Y-%m") if end else None, "linked_pot_ref": raw.get("linked_pot_ref"), "destination_ref": raw.get("destination_ref")}


def _normalize_action(raw: Any, summary: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    action_value = raw.get("action")
    action = action_value if isinstance(action_value, dict) else raw
    kind = action.get("kind")
    if kind not in _ACTION_KINDS:
        return None
    amount = _finite_number(action.get("amount"))
    if amount is None or amount <= 0:
        return None
    if kind == "scenario":
        start = _month(action.get("start_month"))
        duration_raw = action.get("duration")
        try:
            duration = int(duration_raw)
        except (TypeError, ValueError, OverflowError):
            return None
        if start is None or start <= date.today().replace(day=1) or isinstance(duration_raw, bool) or not 1 <= duration <= MAX_SCENARIO_DURATION or action.get("scenario_kind") not in _SCENARIO_KINDS:
            return None
        return {"kind": kind, "scenario_kind": action["scenario_kind"], "amount": round(amount, 2), "start_month": start.strftime("%Y-%m"), "duration": duration, "target_group": "none", "source_group": "none", "transaction_ref": None, "transaction": None}
    if kind == "reallocate":
        if action.get("target_group") != "Gasolina" or action.get("source_group") not in ("Lazer", "Lazer e escolhas"):
            return None
        start = _month(action.get("start_month"))
        if start is not None and start <= date.today().replace(day=1):
            return None
        duration_raw = action.get("duration") or 0
        try:
            duration = int(duration_raw)
        except (TypeError, ValueError, OverflowError):
            return None
        if isinstance(duration_raw, bool) or duration < 0 or duration > MAX_SCENARIO_DURATION:
            return None
        return {"kind": kind, "scenario_kind": "none", "amount": round(amount, 2), "start_month": start.strftime("%Y-%m") if start else "", "duration": duration, "target_group": "Gasolina", "source_group": "Lazer", "transaction_ref": None, "transaction": None}
    tx_ref = action.get("transaction_ref")
    if kind == "edit_transaction" and not isinstance(tx_ref, str):
        return None
    transaction = validate_transaction_payload(action.get("transaction"), summary, kind=kind)
    if transaction is None:
        return None
    if kind == "edit_transaction" and tx_ref not in _reference_sets(summary).get("transactions", set()):
        return None
    return {"kind": kind, "scenario_kind": "none", "amount": round(amount, 2), "start_month": _month(action.get("start_month")).strftime("%Y-%m") if _month(action.get("start_month")) else "", "duration": int(action.get("duration") or 0), "target_group": "none", "source_group": "none", "transaction_ref": tx_ref if kind == "edit_transaction" else None, "transaction": transaction}


def validate_proposal(raw: Any, summary: dict[str, Any] | None, preferences: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not isinstance(summary, dict) or not _summary_has_data(summary):
        return None
    bounds = _summary_bounds(summary)
    if bounds is None:
        return None
    period = _month_pair(raw.get("period"))
    if period is None or period[0] < bounds[0] or period[1] > bounds[1]:
        return None
    try:
        months = int(raw.get("months"))
    except (TypeError, ValueError, OverflowError):
        return None
    if isinstance(raw.get("months"), bool) or not 1 <= months <= MAX_EVIDENCE_MONTHS:
        return None
    if (period[1].year - period[0].year) * 12 + period[1].month - period[0].month + 1 > months:
        return None
    title, evidence, uncertainty = _clean_text(raw.get("title"), 240), _clean_text(raw.get("evidence")), _clean_text(raw.get("uncertainty"), 500)
    limitations, impact = _clean_text(raw.get("limitations"), 700), _clean_text(raw.get("impact"), 700)
    before, after = _finite_number(raw.get("before")), _finite_number(raw.get("after"))
    if not all((title, evidence, uncertainty, limitations, impact)) or before is None or after is None:
        return None
    action = _normalize_action(raw, summary)
    if action is None:
        return None
    text_for_safety = " ".join((title, evidence, impact, limitations, action["kind"], action["scenario_kind"]))
    if _contains_unsupported(text_for_safety) or _is_protected_cut(text_for_safety, preferences):
        return None
    return {"title": title, "evidence": evidence, "period": f"{period[0]:%Y-%m} a {period[1]:%Y-%m}" if period[0] != period[1] else f"{period[0]:%Y-%m}", "months": months, "uncertainty": uncertainty, "limitations": limitations, "before": round(before, 2), "after": round(after, 2), "impact": impact, "action": action}


def validate_proposals(raw: Any, summary: dict[str, Any] | None, preferences: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    result = []
    for item in raw:
        proposal = validate_proposal(item, summary, preferences)
        if proposal is not None:
            result.append(proposal)
        if len(result) >= MAX_PROPOSALS:
            break
    return result


def parse_assistant_proposals(content: Any, summary: dict[str, Any] | None, preferences: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(content, str) or not content.strip():
        return {"answer": "", "proposals": []}
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        parsed = json.loads(text, parse_constant=lambda constant: (_ for _ in ()).throw(ValueError(constant)))
    except (TypeError, ValueError, json.JSONDecodeError):
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return {"answer": _clean_text(content, 7000), "proposals": []}
        try:
            parsed = json.loads(text[start:end + 1], parse_constant=lambda constant: (_ for _ in ()).throw(ValueError(constant)))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {"answer": _clean_text(content, 7000), "proposals": []}
    if not isinstance(parsed, dict):
        return {"answer": _clean_text(content, 7000), "proposals": []}
    answer = _clean_text(parsed.get("answer"), 7000) or _clean_text(content, 7000)
    return {"answer": answer, "proposals": validate_proposals(parsed.get("proposals"), summary, preferences)}


validate_proposal_response = parse_assistant_proposals
parse_proposals = parse_assistant_proposals

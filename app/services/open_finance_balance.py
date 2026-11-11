"""Read-only comparison between FinFam cash and Open Finance account snapshots."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping, Sequence

CENT = Decimal("0.01")
_CASH_MATCH_TYPES = {"Receita", "Fixo", "Débito/Pix"}
_COMPLEX_LOCAL_TYPES = {
    "Fatura cartão", "Reserva", "Caixinha", "Saque Caixinha",
    "Transferência", "Transf. Env.", "Transf. Receb.",
}


def money(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite():
        return None
    return parsed.quantize(CENT, rounding=ROUND_HALF_UP)


def bank_signed_amount(row: Any) -> Decimal | None:
    amount = money(getattr(row, "amount", None))
    if amount is None or amount == 0:
        return None
    direction = str(getattr(row, "transaction_type", "") or "").strip().upper()
    if direction == "CREDIT":
        return abs(amount)
    if direction == "DEBIT":
        return -abs(amount)
    return None


def local_signed_amount(item: Mapping[str, Any]) -> Decimal | None:
    amount = money(item.get("signed_amount"))
    if amount is not None:
        return amount
    value = money(item.get("amount"))
    if value is None:
        return None
    if item.get("type") == "Receita":
        return abs(value)
    if item.get("type") in _CASH_MATCH_TYPES | _COMPLEX_LOCAL_TYPES:
        return -abs(value)
    return None


def _transaction_entry(row: Any, result: Mapping[str, Any], history: Mapping[str, Any]) -> dict[str, Any]:
    external_id = str(getattr(row, "transaction_id", "") or "")
    candidates = list(result.get("candidates") or [])
    candidate = result.get("candidate") or (candidates[0] if len(candidates) == 1 else None)
    event = history.get(external_id) or {}
    return {
        "row": row,
        "state": str(result.get("state") or "none"),
        "reason": str(result.get("reason") or ""),
        "candidate": candidate,
        "candidates": candidates,
        "decision": str(event.get("decision") or result.get("decision") or ""),
        "local_item_key": str(event.get("local_item_key") or ""),
        "local_transaction_id": event.get("local_transaction_id"),
        "block_reason": str(result.get("block_reason") or ""),
        "can_confirm": bool(result.get("can_confirm")),
        "bank_effect": bank_signed_amount(row),
        "effect": Decimal("0.00"),
    }


def reconcile_account(
    *,
    account_name: str,
    bank_balance: Any,
    finfam_balance: Any,
    snapshot_date: str,
    bank_rows: Sequence[Any],
    matches: Mapping[str, Mapping[str, Any]],
    history: Mapping[str, Mapping[str, Any]],
    local_items: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Explain a balance delta with one-to-one evidence; never writes or invents a match."""
    bank_amount, local_amount = money(bank_balance), money(finfam_balance)
    if bank_amount is None or local_amount is None:
        return {"account": account_name, "comparable": False, "reason": "balance"}

    difference = (bank_amount - local_amount).quantize(CENT, rounding=ROUND_HALF_UP)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    reserved_item_keys: set[str] = set()
    effects: list[Decimal] = []
    seen_external_ids: set[str] = set()

    for row in sorted(bank_rows, key=lambda item: str(getattr(item, "occurred_on", "")), reverse=True):
        external_id = str(getattr(row, "transaction_id", "") or "")
        if not external_id or external_id in seen_external_ids:
            continue
        seen_external_ids.add(external_id)
        result = matches.get(external_id) or {}
        entry = _transaction_entry(row, result, history)
        status = str(getattr(row, "status", "") or "").strip().upper()
        currency = str(getattr(row, "currency_code", "") or "").strip().upper()
        state = entry["state"]
        candidates = entry["candidates"]
        candidate = entry["candidate"]

        for possible in candidates:
            key = str(possible.get("item_key") or "")
            if key:
                reserved_item_keys.add(key)
        if entry["local_item_key"]:
            reserved_item_keys.add(entry["local_item_key"])

        if status == "PENDING":
            groups["pending"].append(entry)
            continue
        if status != "POSTED" or currency != "BRL":
            entry["excluded_reason"] = "status_or_currency"
            groups["excluded"].append(entry)
            continue
        if state == "ambiguous":
            groups["ambiguous"].append(entry)
            continue
        if state == "candidate" and candidate:
            entry["effect"] = entry["bank_effect"] or Decimal("0.00")
            effects.append(entry["effect"])
            groups["unconfirmed_match"].append(entry)
            continue
        if state == "already_confirmed" and candidate:
            local_item = next((item for item in local_items
                               if str(item.get("item_key") or "") == str(candidate.get("item_key") or "")), None)
            local_effect = local_signed_amount(local_item) if local_item else None
            entry["effect"] = ((entry["bank_effect"] or Decimal("0.00")) - (local_effect or Decimal("0.00")))
            if entry["effect"]:
                effects.append(entry["effect"])
                groups["confirmed_mismatch"].append(entry)
            continue
        if state == "reviewed":
            decision = entry["decision"]
            local_item = next((item for item in local_items
                               if str(item.get("item_key") or "") == entry["local_item_key"]), None)
            if decision in {"registered_pending", "registered_and_confirmed"} and local_item is not None:
                if local_item.get("paid"):
                    local_effect = local_signed_amount(local_item) or Decimal("0.00")
                    entry["effect"] = (entry["bank_effect"] or Decimal("0.00")) - local_effect
                    if entry["effect"]:
                        effects.append(entry["effect"])
                        groups["confirmed_mismatch"].append(entry)
                else:
                    entry["effect"] = entry["bank_effect"] or Decimal("0.00")
                    effects.append(entry["effect"])
                    groups["registered_pending"].append(entry)
            elif decision in {"confirmed", "adjusted_and_confirmed"}:
                if local_item is not None and local_item.get("paid"):
                    local_effect = local_signed_amount(local_item) or Decimal("0.00")
                else:
                    planned = money(history.get(external_id, {}).get("planned_amount_before_review")) or Decimal("0.00")
                    local_effect = planned if candidate and candidate.get("type") == "Receita" else -planned
                entry["effect"] = (entry["bank_effect"] or Decimal("0.00")) - local_effect
                if entry["effect"]:
                    effects.append(entry["effect"])
                    groups["confirmed_mismatch"].append(entry)
            elif decision == "ignored":
                entry["effect"] = entry["bank_effect"] or Decimal("0.00")
                effects.append(entry["effect"])
                groups["bank_only"].append(entry)
            else:
                groups["reviewed"].append(entry)
            continue
        if state == "none":
            entry["effect"] = entry["bank_effect"] or Decimal("0.00")
            direction = str(getattr(row, "transaction_type", "") or "").strip().upper()
            if entry["bank_effect"] is None or direction not in {"DEBIT", "CREDIT"}:
                entry["excluded_reason"] = "transfer_or_non_cash"
                groups["excluded"].append(entry)
            else:
                effects.append(entry["effect"])
                groups["bank_only"].append(entry)
            continue
        entry["excluded_reason"] = str(result.get("reason") or "no_safe_match")
        groups["excluded"].append(entry)

    local_by_key = {str(item.get("item_key") or ""): item for item in local_items}
    for key, item in local_by_key.items():
        if not key or key in reserved_item_keys or not item.get("paid"):
            continue
        amount = local_signed_amount(item)
        if amount is None:
            continue
        entry = {"item": item, "effect": Decimal("0.00"), "date": item.get("paid_on") or item.get("transaction_date")}
        if item.get("type") in _CASH_MATCH_TYPES:
            entry["effect"] = -amount
            effects.append(entry["effect"])
            groups["local_only"].append(entry)
        elif item.get("type") in _COMPLEX_LOCAL_TYPES:
            entry["effect"] = None
            groups["local_complex"].append(entry)

    explained = sum(effects, Decimal("0.00")).quantize(CENT, rounding=ROUND_HALF_UP)
    remaining = (difference - explained).quantize(CENT, rounding=ROUND_HALF_UP)
    return {
        "account": account_name,
        "comparable": True,
        "snapshot_date": snapshot_date,
        "bank_balance": bank_amount,
        "finfam_balance": local_amount,
        "difference": difference,
        "explained_effect": explained,
        "remaining_difference": remaining,
        "groups": dict(groups),
        "has_movements": any(groups.values()),
        "excluded_count": len(groups.get("excluded", [])) + len(groups.get("pending", []))
            + len(groups.get("ambiguous", [])) + len(groups.get("local_complex", [])),
    }


def source_creation_block_reason(row: Any) -> str:
    """Do not offer a generic income/expense shortcut for ambiguous transfers or card bills."""
    import unicodedata

    def normalize(value: Any) -> str:
        text = unicodedata.normalize("NFKD", str(value or ""))
        return "".join(char for char in text if not unicodedata.combining(char)).casefold()

    description = normalize(getattr(row, "description", ""))
    category = normalize(getattr(row, "category", ""))
    combined = f"{description} {category}"
    direction = str(getattr(row, "transaction_type", "") or "").strip().upper()
    if "fatura" in combined or "pagamento cartao" in combined or "pagamento de cartao" in combined:
        return "possible_card_invoice"
    internal_transfer_terms = ("transferencia interna", "entre contas", "conta propria", "contas proprias")
    if any(term in combined for term in internal_transfer_terms):
        return "possible_internal_transfer"
    if direction == "CREDIT" and any(term in combined for term in (
        "transferencia", "transfer received", "pix recebido", "ted recebida", "doc recebido",
    )):
        return "possible_transfer_credit"
    if direction == "DEBIT" and any(term in category for term in ("transfer", "transferencia")):
        return "possible_transfer_debit"
    return ""

"""Motor financeiro em Python para reproduzir a lógica da planilha Hener/Ju.

Não executa fórmulas de Excel: calcula os indicadores diretamente a partir dos
lançamentos e das configurações, com regras explícitas e testáveis.
"""
from __future__ import annotations

from calendar import monthrange
from collections import defaultdict, deque
from datetime import date, datetime
from typing import Any, Iterable

from app.services.card_installments import allocated_month_lines, effective_card_amount

INFINITE_MONTH = 999999
FAMILY = "Família"
MEMBERS = ("Hener", "Ju")
CARD_TYPES = {"Parcelado", "Crédito à vista"}
CASH_TYPES = {"Fixo", "Débito/Pix", "Caixinha", "Saque Caixinha"}


def as_date(value: Any) -> date | None:
    if value in (None, "", "-"):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        # Suporte a datas Excel seriais caso um import futuro as leia como número.
        from openpyxl.utils.datetime import from_excel
        converted = from_excel(value)
        return converted.date() if isinstance(converted, datetime) else converted
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    try:
        parsed = datetime.strptime(text, "%Y-%m").date()
        return date(parsed.year, parsed.month, 1)
    except ValueError:
        pass
    raise ValueError(f"Data inválida: {value!r}")


def month_start(value: Any) -> date | None:
    d = as_date(value)
    return date(d.year, d.month, 1) if d else None


def month_id(value: Any) -> int | None:
    d = as_date(value)
    return d.year * 12 + d.month if d else None


def month_key(value: Any) -> str:
    d = month_start(value)
    if d is None:
        raise ValueError("Mês não informado")
    return d.strftime("%Y-%m")


def month_label(value: Any) -> str:
    d = month_start(value)
    if d is None:
        return "—"
    names = ("jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez")
    return f"{names[d.month - 1]}/{d.year}"


def month_from_id(value: int) -> date:
    year, month = divmod(int(value) - 1, 12)
    return date(year, month + 1, 1)


def month_range(start: Any, end: Any) -> list[date]:
    first = month_start(start)
    last = month_start(end)
    if first is None or last is None or first > last:
        return []
    result = []
    cursor = first
    while cursor <= last:
        result.append(cursor)
        if cursor == last:
            break
        current_idx = month_id(cursor)
        if current_idx is None:
            return result
        cursor = month_from_id(current_idx + 1)
    return result


def _num(value: Any) -> float:
    if value in (None, "", "-"):
        return 0.0
    if isinstance(value, str):
        text = value.strip().replace("R$", "").replace(" ", "")
        if "," in text:
            text = text.replace(".", "").replace(",", ".")
        value = text
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _text(value: Any) -> str:
    return str(value).strip() if value not in (None, "-") else ""


def normalize_transaction(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize database/workbook fields and recalculate derived month IDs."""
    row = dict(raw)
    row["id"] = int(row.get("id") or 0)
    row["member"] = _text(row.get("member"))
    row["category"] = _text(row.get("category"))
    row["description"] = _text(row.get("description"))
    row["type"] = _text(row.get("type"))
    row["account_card"] = _text(row.get("account_card"))
    row["amount"] = _num(row.get("amount"))
    row["start_month"] = month_start(row.get("start_month"))
    row["end_month"] = month_start(row.get("end_month"))
    row["destination_account"] = _text(row.get("destination_account"))
    row["linked_pot"] = _text(row.get("linked_pot"))
    row["card_flag"] = int(row.get("card_flag") or 0)
    row["payment_bank"] = _text(row.get("payment_bank")) or row["account_card"]
    row["transaction_date"] = as_date(row.get("transaction_date")) or row["start_month"]
    row["date_precision"] = _text(row.get("date_precision")) or "exact"
    row["installment_count"] = max(1, int(_num(row.get("installment_count")) or 1))
    row["total_amount"] = _num(row.get("total_amount")) or (
        row["amount"] * row["installment_count"] if row["type"] == "Parcelado" else row["amount"]
    )
    row["archived"] = bool(row.get("archived", False))
    row["start_id"] = month_id(row["start_month"])
    row["end_id"] = month_id(row["end_month"]) if row["end_month"] else INFINITE_MONTH
    return row


def _is_active(tx: dict[str, Any], idx: int) -> bool:
    start_id = tx.get("start_id")
    return start_id is not None and start_id <= idx <= int(tx.get("end_id") or INFINITE_MONTH)


def _member_ok(tx: dict[str, Any], person: str) -> bool:
    return person == FAMILY or tx.get("member") == person


def _card_ok(tx: dict[str, Any], person: str) -> bool:
    if person == FAMILY:
        return True
    if person == "Ju":
        return tx.get("account_card") == "Nubank cartão"
    if person == "Hener":
        return tx.get("account_card") != "Nubank cartão"
    return True


def _sum(rows: Iterable[dict[str, Any]], predicate) -> float:
    return sum(float(row["amount"]) for row in rows if predicate(row))


def _card_amounts_for_month(lines: list[dict[str, Any]], month: Any) -> dict[int, float]:
    """Return card lines for one month; allocate cents only when an override exists."""
    key = month_key(month)
    if any(isinstance(tx.get("installment_overrides"), dict) and key in tx["installment_overrides"] for tx in lines):
        _, allocated = allocated_month_lines(lines, month)
        return {int(tx.get("id") or 0): allocated[index] for index, tx in enumerate(lines)}
    return {int(tx.get("id") or 0): float(tx.get("amount") or 0) for tx in lines}


def _apply_card_overrides(rows: list[dict[str, Any]], month: Any) -> list[dict[str, Any]]:
    idx = month_id(month)
    if idx is None:
        return rows
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for tx in rows:
        if tx["card_flag"] == 1 and tx["type"] != "Receita":
            groups[tx["account_card"]].append(tx)
    replacements: dict[int, float] = {}
    for lines in groups.values():
        replacements.update(_card_amounts_for_month(lines, month))
    return [dict(tx, amount=replacements[tx["id"]]) if tx["id"] in replacements else tx for tx in rows]


def _linked_pot_usage(transactions: list[dict[str, Any]], idx: int, person: str) -> tuple[float, dict[str, float]]:
    total = 0.0
    by_pot: dict[str, float] = {}
    for tx in transactions:
        if not tx["linked_pot"] or tx["type"] == "Reserva" or tx["start_id"] is None:
            continue
        if not _member_ok(tx, person):
            continue
        # Na planilha, compra no cartão consome o pote um mês antes do vencimento.
        trigger = tx["start_id"] - tx["card_flag"]
        if trigger != idx:
            continue
        quantity = 1
        if tx["type"] == "Parcelado" and tx["end_id"] != INFINITE_MONTH:
            quantity = max(1, tx["end_id"] - tx["start_id"] + 1)
        consumed = tx["amount"] * quantity
        total += consumed
        by_pot[tx["linked_pot"]] = by_pot.get(tx["linked_pot"], 0.0) + consumed
    return total, by_pot


def _pot_balances(transactions: list[dict[str, Any]], idx: int, person: str) -> dict[str, float]:
    """Accumulated balances for each named savings pot (Pote Vinculado)."""
    balances: dict[str, float] = {}
    for tx in transactions:
        if not _member_ok(tx, person) or not _is_active(tx, idx):
            continue
        if tx["type"] == "Reserva":
            name = tx["description"]
            balances[name] = balances.get(name, 0.0) + tx["amount"]
    for tx in transactions:
        if not tx["linked_pot"] or tx["type"] == "Reserva" or tx["start_id"] is None:
            continue
        if not _member_ok(tx, person):
            continue
        trigger = tx["start_id"] - tx["card_flag"]
        if trigger <= idx:
            quantity = 1
            if tx["type"] == "Parcelado" and tx["end_id"] != INFINITE_MONTH:
                quantity = max(1, tx["end_id"] - tx["start_id"] + 1)
            balances[tx["linked_pot"]] = balances.get(tx["linked_pot"], 0.0) - tx["amount"] * quantity
    return balances


def _savings_month(transactions: list[dict[str, Any]], idx: int, person: str) -> tuple[float, dict[str, float]]:
    by_category: dict[str, float] = {}
    for tx in transactions:
        if tx["type"] not in ("Caixinha", "Saque Caixinha") or not _member_ok(tx, person) or not _is_active(tx, idx):
            continue
        category = tx["category"] or "Caixinha sem categoria"
        sign = 1.0 if tx["type"] == "Caixinha" else -1.0
        by_category[category] = by_category.get(category, 0.0) + sign * tx["amount"]
    return sum(by_category.values()), by_category


def monthly_details(
    transactions: Iterable[dict[str, Any]],
    month: Any,
    person: str = FAMILY,
    *,
    closed: bool = False,
    starting_balance: float = 0.0,
    accumulated_savings_before: float = 0.0,
) -> dict[str, Any]:
    """Compute the plan sheet's monthly categories, reserves and ending balance."""
    d = month_start(month)
    if d is None:
        raise ValueError("Informe um mês válido")
    idx = month_id(d)
    if idx is None:
        raise ValueError("Informe um mês válido")
    rows = [normalize_transaction(t) for t in transactions if not t.get("archived", False)]
    active = [t for t in rows if _is_active(t, idx)]
    # Card-only calculation view: purchase values stay immutable; adjusted invoice
    # cents are allocated consistently across the card's lines for this month.
    active = _apply_card_overrides(active, d)

    income = _sum(active, lambda t: t["type"] == "Receita" and _member_ok(t, person))

    transfer_in = transfer_out = 0.0
    if person != FAMILY:
        major_banks = {"Itaú", "Santander", "C6", "Mercado Pago"}
        for tx in active:
            if tx["type"] != "Transferência":
                continue
            destination = tx["destination_account"]
            if person == "Hener":
                if tx["member"] == "Ju" and destination in major_banks:
                    transfer_in += tx["amount"]
                if tx["member"] == "Hener" and destination == "Nubank":
                    transfer_out += tx["amount"]
            elif person == "Ju":
                if tx["member"] == "Hener" and destination == "Nubank":
                    transfer_in += tx["amount"]
                if tx["member"] == "Ju" and destination in major_banks:
                    transfer_out += tx["amount"]

    card_total = _sum(active, lambda t: t["card_flag"] == 1 and t["type"] != "Receita" and _card_ok(t, person))
    old_installments = _sum(
        active,
        lambda t: t["card_flag"] == 1 and t["type"] == "Parcelado"
        and t["start_id"] < idx and _card_ok(t, person),
    )
    new_installments = _sum(
        active,
        lambda t: t["card_flag"] == 1 and t["type"] == "Parcelado"
        and t["start_id"] == idx and _card_ok(t, person),
    )
    credit_without_pot = _sum(
        active,
        lambda t: t["card_flag"] == 1 and t["type"] == "Crédito à vista"
        and not t["linked_pot"] and _card_ok(t, person),
    )
    credit_with_pot = _sum(
        active,
        lambda t: t["card_flag"] == 1 and t["type"] == "Crédito à vista"
        and bool(t["linked_pot"]) and _card_ok(t, person),
    )
    card_by_name: dict[str, float] = {}
    for tx in active:
        if tx["card_flag"] == 1 and tx["type"] != "Receita" and _card_ok(tx, person):
            card_by_name[tx["account_card"]] = card_by_name.get(tx["account_card"], 0.0) + tx["amount"]

    fixed = _sum(active, lambda t: t["card_flag"] == 0 and t["type"] == "Fixo" and _member_ok(t, person))
    debit_no_pot = _sum(active, lambda t: t["card_flag"] == 0 and t["type"] == "Débito/Pix" and not t["linked_pot"] and _member_ok(t, person))
    debit_with_pot = _sum(active, lambda t: t["card_flag"] == 0 and t["type"] == "Débito/Pix" and bool(t["linked_pot"]) and _member_ok(t, person))
    planned_reserve = _sum(active, lambda t: t["type"] == "Reserva" and _member_ok(t, person))
    pot_use, pot_use_by_name = _linked_pot_usage(rows, idx, person)
    reserve_need = 0.0 if closed else max(0.0, planned_reserve - pot_use)
    reserve_excess = max(0.0, pot_use - planned_reserve)
    savings_net, savings_by_category = _savings_month(rows, idx, person)
    pot_balances = _pot_balances(rows, idx, person)
    saved_to_date = accumulated_savings_before + savings_net

    # Reproduz Painel!AE/AF. A coluna AF retira da fatura os itens de crédito
    # que a fórmula original trata como cobertos por potes: crédito à vista
    # vinculado a pote e parcelas ativas vinculadas a pote. Para os filtros
    # individuais, a planilha classifica essas parcelas pelo cartão, não pelo
    # membro registrado no lançamento.
    card_linked_pot_credit = credit_with_pot
    linked_pot_installments = _sum(
        active,
        lambda t: t["type"] == "Parcelado" and bool(t["linked_pot"])
        and _card_ok(t, person),
    )
    credit_pot_covered = max(0.0, min(planned_reserve, pot_use) - debit_with_pot)
    reserve_budget_outflow = max(planned_reserve, debit_with_pot + credit_pot_covered)
    monthly_balance_with_prior_impact = (
        income + transfer_in - transfer_out - card_total - fixed - debit_no_pot
        - savings_net - reserve_budget_outflow
    )
    prior_impact_adjustment = card_linked_pot_credit + linked_pot_installments
    monthly_balance_without_prior_impact = monthly_balance_with_prior_impact + prior_impact_adjustment

    # Mantém a ordem da fórmula Excel em Painel!AG: saldo inicial primeiro,
    # seguido por entradas e saídas. Evita pequenas diferenças de ponto
    # flutuante que se acumulavam ao encadear projeções futuras.
    ending_balance = (
        starting_balance + income + transfer_in - transfer_out - card_total
        - fixed - debit_no_pot - debit_with_pot - reserve_need - savings_net
    )
    net_change = ending_balance - starting_balance
    gasoline_reserved = _sum(active, lambda t: t["type"] == "Reserva" and "gasolina" in t["description"].casefold() and _member_ok(t, person))
    obligatory = card_total + fixed + gasoline_reserved
    reserve_share = (saved_to_date / income) if income else 0.0
    free_after = income - obligatory - savings_net

    return {
        "month": d,
        "person": person,
        "closed": bool(closed),
        "starting_balance": starting_balance,
        "income": income,
        "transfer_in": transfer_in,
        "transfer_out": transfer_out,
        "card_total": card_total,
        "card_by_name": card_by_name,
        "old_installments": old_installments,
        "new_installments": new_installments,
        "credit_without_pot": credit_without_pot,
        "credit_with_pot": credit_with_pot,
        "fixed": fixed,
        "debit_no_pot": debit_no_pot,
        "debit_with_pot": debit_with_pot,
        "planned_reserve": planned_reserve,
        "linked_pot_use": pot_use,
        "linked_pot_use_by_name": pot_use_by_name,
        "reserve_need": reserve_need,
        "reserve_excess": reserve_excess,
        "savings_net": savings_net,
        "savings_by_category": savings_by_category,
        "savings_accumulated": saved_to_date,
        "pot_balances": pot_balances,
        "card_linked_pot_credit": card_linked_pot_credit,
        "linked_pot_installments": linked_pot_installments,
        "prior_impact_adjustment": prior_impact_adjustment,
        "reserve_budget_outflow": reserve_budget_outflow,
        "monthly_balance_with_prior_impact": monthly_balance_with_prior_impact,
        "monthly_balance_without_prior_impact": monthly_balance_without_prior_impact,
        "net_change": net_change,
        "ending_balance": ending_balance,
        "minimum_balance_gap": ending_balance,
        "gasoline_reserved": gasoline_reserved,
        "obligatory": obligatory,
        "free_after_obligatory_and_savings": free_after,
        "free_share": (free_after / income) if income else 0.0,
        # Mantém a métrica histórica da planilha, cujo denominador é a receita.
        "months_covered": reserve_share,
        "runway_status": "Risco" if reserve_share < 1 else ("Atenção" if reserve_share < 3 else "Seguro"),
        "budget_status": "Saudável" if (free_after / income if income else 0.0) >= 0.2 else "Atenção",
    }


def projection(
    transactions: Iterable[dict[str, Any]],
    accounts: Iterable[dict[str, Any]],
    start: Any,
    end: Any,
    person: str = FAMILY,
    *,
    closed_months: dict[str, bool] | None = None,
    minimum_balance: float = 0.0,
    scenarios: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build a rolling monthly projection, beginning with configured balances."""
    rows = [normalize_transaction(t) for t in transactions if not t.get("archived", False)]
    accounts = list(accounts)
    first = month_start(start)
    last = month_start(end)
    if first is None or last is None or first > last:
        return []
    initial = sum(_num(a.get("initial_balance")) for a in accounts if person == FAMILY or a.get("owner") == person)
    output: list[dict[str, Any]] = []
    opening = initial
    accumulated = 0.0
    closed_months = closed_months or {}
    scenarios = scenarios or []
    for d in month_range(first, last):
        current_idx = month_id(d)
        if current_idx is None:
            continue
        key = month_key(d)
        detail = monthly_details(
            rows, d, person, closed=bool(closed_months.get(key, False)),
            starting_balance=opening, accumulated_savings_before=accumulated,
        )
        adjustment = 0.0
        for scenario in scenarios:
            scenario_start = month_id(scenario.get("start_month"))
            duration = max(0, int(_num(scenario.get("months"))))
            if scenario_start is not None and scenario_start <= current_idx < scenario_start + duration:
                adjustment += _num(scenario.get("monthly_amount"))
        detail["scenario_outflow"] = adjustment
        detail["projected_ending_balance"] = detail["ending_balance"] - adjustment
        detail["minimum_balance"] = minimum_balance
        detail["gap_to_minimum"] = detail["projected_ending_balance"] - minimum_balance
        detail["minimum_status"] = "Acima do piso" if detail["gap_to_minimum"] >= 0 else "Abaixo do piso"
        output.append(detail)
        # Próximo mês encadeia saldo projetado e a movimentação líquida de caixinhas.
        opening = detail["projected_ending_balance"]
        accumulated += detail["savings_net"]
    return output


def bill_description(card_name: str) -> str:
    clean = card_name.replace(" cartão", "").strip()
    return f"Fatura {clean}"


def _account_bank(tx: dict[str, Any]) -> str:
    """Return the bank/account for a non-card transaction.

    The workbook's account column is authoritative for non-card rows.  Keep
    the payment bank only as a compatibility fallback for incomplete legacy
    rows.
    """
    return tx.get("account_card", "") or tx.get("payment_bank", "")


def _card_banks(cards: Iterable[dict[str, Any]] | None) -> dict[str, str]:
    return {
        str(card.get("name") or "").strip(): _text(card.get("bank"))
        for card in (cards or [])
        if str(card.get("name") or "").strip()
    }


def _card_bank(tx: dict[str, Any], card_banks: dict[str, str]) -> str:
    """Return the configured issuing bank, with legacy data fallbacks."""
    return card_banks.get(tx.get("account_card", "")) or tx.get("payment_bank", "") or tx.get("account_card", "")


def cashflow_items(
    transactions: Iterable[dict[str, Any]],
    cards: Iterable[dict[str, Any]],
    accounts: Iterable[dict[str, Any]],
    month: Any,
    person: str = FAMILY,
) -> list[dict[str, Any]]:
    """Items to reconcile as paid/unpaid in the short-term cash panel."""
    d = month_start(month)
    if d is None:
        return []
    idx = month_id(d)
    if idx is None:
        return []
    txs = [normalize_transaction(t) for t in transactions if not t.get("archived", False)]
    cards = list(cards)
    account_rows = list(accounts)
    owner = {a["name"]: a.get("owner", "") for a in account_rows}
    result: list[dict[str, Any]] = []

    def add_item(key: str, account: str, member: str, kind: str, description: str, amount: float, signed: float, tx: dict[str, Any] | None = None):
        if abs(amount) < 0.0000001:
            return
        tx_date = tx.get("transaction_date") if tx else None
        precision = tx.get("date_precision", "exact") if tx else "exact"
        if tx and tx_date:
            exact_date = as_date(tx_date)
            start_date = month_start(tx.get("start_month"))
            if precision in ("exact", "recurrence") and exact_date and start_date and start_date < d:
                tx_date = date(d.year, d.month, min(exact_date.day, monthrange(d.year, d.month)[1]))
                precision = "recurrence"
            elif precision not in ("exact", "recurrence"):
                tx_date = None
        result.append({
            "item_key": key,
            "account": account,
            "member": member or "Família",
            "type": kind,
            "description": description,
            "amount": amount,
            "signed_amount": signed,
            "transaction_id": tx.get("id") if tx else None,
            "transaction_date": tx_date.isoformat() if isinstance(tx_date, date) else (str(tx_date) if tx_date else None),
            "date_precision": precision,
            "linked_pot": tx.get("linked_pot", "") if tx else "",
            "due_month": d,
            "month": d,
            "paid": False,
        })

    active = [t for t in txs if _is_active(t, idx)]
    # Receitas, despesas de débito e movimentações de caixinha linha a linha.
    for tx in active:
        if tx["type"] == "Transferência" or tx["card_flag"] == 1:
            continue
        if person != FAMILY and tx["member"] != person:
            continue
        bank = _account_bank(tx)
        key = f"{month_key(d)}:tx:{tx['id']}"
        if tx["type"] == "Receita":
            add_item(key, bank, tx["member"], "Receita", tx["description"], tx["amount"], tx["amount"], tx)
        elif tx["type"] in ("Fixo", "Débito/Pix"):
            add_item(key, bank, tx["member"], tx["type"], tx["description"], tx["amount"], -tx["amount"], tx)
        elif tx["type"] == "Caixinha":
            add_item(key, bank, tx["member"], "Caixinha", tx["description"], tx["amount"], -tx["amount"], tx)
        elif tx["type"] == "Saque Caixinha":
            add_item(key, bank, tx["member"], "Saque Caixinha", tx["description"], tx["amount"], tx["amount"], tx)

    # Uma fatura reconciliável por cartão, conforme o pagamento cai na conta bancária.
    by_card: dict[str, list[dict[str, Any]]] = {}
    for tx in active:
        if tx["card_flag"] != 1 or tx["type"] == "Receita" or not _card_ok(tx, person):
            continue
        by_card.setdefault(tx["account_card"], []).append(tx)
    card_bank = _card_banks(cards)
    for name, lines in by_card.items():
        if any(isinstance(tx.get("installment_overrides"), dict) and month_key(d) in tx["installment_overrides"] for tx in lines):
            amount, _ = allocated_month_lines(lines, d)
        else:
            amount = sum(tx["amount"] for tx in lines)
        bank = _card_bank(lines[0], card_bank)
        add_item(f"{month_key(d)}:invoice:{name}", bank, FAMILY, "Fatura cartão", bill_description(name), amount, -amount)

    # Transferência é uma saída na origem e uma entrada no destino; soma familiar = 0.
    for tx in active:
        if tx["type"] != "Transferência":
            continue
        source_bank = _account_bank(tx)
        target_bank = tx["destination_account"]
        if not source_bank or not target_bank:
            continue
        source_owner = owner.get(source_bank) or tx["member"]
        target_owner = owner.get(target_bank) or tx["member"]
        if person == FAMILY or source_owner == person:
            add_item(f"{month_key(d)}:tx:{tx['id']}:out", source_bank, tx["member"], "Transf. Env.", tx["description"], tx["amount"], -tx["amount"], tx)
        if person == FAMILY or target_owner == person:
            add_item(f"{month_key(d)}:tx:{tx['id']}:in", target_bank, tx["member"], "Transf. Receb.", tx["description"], tx["amount"], tx["amount"], tx)

    return sorted(result, key=lambda x: (x["account"].casefold(), x["type"], x["description"].casefold(), x["item_key"]))


def card_limits(
    transactions: Iterable[dict[str, Any]], cards: Iterable[dict[str, Any]], month: Any,
) -> list[dict[str, Any]]:
    reference_date = as_date(month)
    d = month_start(month)
    if d is None or reference_date is None:
        return []
    idx = month_id(d)
    if idx is None:
        return []
    txs = [normalize_transaction(t) for t in transactions if not t.get("archived", False)]
    results = []
    for card in cards:
        name = card["name"]
        current = future = 0.0
        card_txs = [tx for tx in txs if tx["account_card"] == name and tx["card_flag"] == 1 and tx["start_id"] is not None]
        has_overrides = any(isinstance(tx.get("installment_overrides"), dict) and tx["installment_overrides"] for tx in card_txs)
        if not has_overrides:
            for tx in card_txs:
                # Preserve the original floating-point aggregation unless a user
                # explicitly adjusts a statement line for this card.
                start_date = tx["start_month"]
                end_date = tx["end_month"]
                if start_date is not None and start_date <= reference_date and (end_date is None or end_date >= reference_date):
                    current += tx["amount"]
                if end_date is not None and end_date > reference_date and tx["end_id"] != INFINITE_MONTH:
                    first_future = max(tx["start_id"], idx + 1)
                    future += tx["amount"] * max(0, tx["end_id"] - first_future + 1)
        else:
            active_this_month = [tx for tx in card_txs if _is_active(tx, idx)]
            month_amounts = _card_amounts_for_month(active_this_month, d)
            for tx in card_txs:
                start_date, end_date = tx["start_month"], tx["end_month"]
                if start_date is not None and start_date <= reference_date and (end_date is None or end_date >= reference_date):
                    current += float(month_amounts.get(tx["id"], tx["amount"]) or 0)
            finite_end = max(
                (tx["end_id"] for tx in card_txs if tx["end_month"] is not None and tx["end_month"] > reference_date
                 and tx["end_id"] != INFINITE_MONTH),
                default=idx,
            )
            for future_idx in range(idx + 1, finite_end + 1):
                future_month = month_from_id(future_idx)
                month_lines = [
                    tx for tx in card_txs
                    if tx["end_month"] is not None and tx["end_month"] > reference_date and _is_active(tx, future_idx)
                ]
                month_amounts = _card_amounts_for_month(month_lines, future_month)
                future += sum(float(month_amounts.get(tx["id"], tx["amount"]) or 0) for tx in month_lines)
        limit = _num(card.get("limit_amount"))
        used = current + future
        results.append({
            "card": name,
            "bank": card.get("bank", ""),
            "limit": limit,
            "current_invoice": current,
            "future_installments": future,
            "total_committed": used,
            "available": limit - used,
            "utilization": (used / limit) if limit else None,
        })
    return results


def pot_monthly_status(
    transactions: Iterable[dict[str, Any]], month: Any, person: str = FAMILY,
    preferred_order: list[str] | None = None,
) -> dict[str, Any]:
    """Replicate the workbook's named-pot table (Painel!T77:X92).

    "Reservado" is the reserve contribution active in the selected month;
    "Usado" is linked spending triggered in that month (credit-card purchases
    are charged to the pot one month before the statement month); "Disponível"
    is their difference, with a warning below zero.
    """
    d = month_start(month)
    if d is None:
        return {"rows": [], "totals": {"reserved": 0.0, "used": 0.0, "available": 0.0}}
    idx = month_id(d)
    if idx is None:
        return {"rows": [], "totals": {"reserved": 0.0, "used": 0.0, "available": 0.0}}
    rows = [normalize_transaction(t) for t in transactions if not t.get("archived", False)]
    names: list[str] = []
    for tx in rows:
        if tx["type"] == "Reserva" and tx["description"] and _member_ok(tx, person) and _is_active(tx, idx):
            if tx["description"] not in names:
                names.append(tx["description"])
    if preferred_order:
        rank = {name: i for i, name in enumerate(preferred_order)}
        original_rank = {name: i for i, name in enumerate(names)}
        names.sort(key=lambda name: (rank.get(name, len(rank)), original_rank[name]))
    _, used_by_name = _linked_pot_usage(rows, idx, person)
    result = []
    for name in names:
        reserved = sum(
            tx["amount"] for tx in rows
            if tx["type"] == "Reserva" and tx["description"] == name
            and _member_ok(tx, person) and _is_active(tx, idx)
        )
        used = used_by_name.get(name, 0.0)
        available = reserved - used
        result.append({
            "pot": name,
            "reserved": reserved,
            "used": used,
            "available": available,
            "status": "✓" if available >= 0 else "⚠",
        })
    totals = {
        "reserved": sum(r["reserved"] for r in result),
        "used": sum(r["used"] for r in result),
        "available": sum(r["available"] for r in result),
    }
    return {"rows": result, "totals": totals}


def confirmed_pot_usage(
    transactions: Iterable[dict[str, Any]], month: Any,
    paid_items: Iterable[dict[str, Any]], pot_names: Iterable[str] = (),
) -> dict[str, dict[str, float | int]]:
    """Sum pot-linked expenses whose cash item or card invoice is confirmed paid."""
    d = month_start(month)
    idx = month_id(d) if d else None
    if d is None or idx is None:
        return {}
    names = {str(name).strip().casefold().removeprefix("reserva - "): str(name) for name in pot_names if str(name).strip()}
    paid_transaction_ids: set[int] = set()
    paid_cards: set[str] = set()
    for item in paid_items:
        if not item.get("paid"):
            continue
        transaction_id = item.get("transaction_id")
        if transaction_id is not None:
            paid_transaction_ids.add(int(transaction_id))
        item_key = str(item.get("item_key", ""))
        if item.get("type") == "Fatura cartão" and ":invoice:" in item_key:
            paid_cards.add(item_key.split(":invoice:", 1)[1])

    totals: dict[str, dict[str, float | int]] = {}
    for raw in transactions:
        tx = normalize_transaction(raw)
        if tx["archived"] or not _is_active(tx, idx):
            continue
        if tx["type"] not in ("Fixo", "Débito/Pix", "Crédito à vista", "Parcelado"):
            continue
        if tx["card_flag"]:
            if tx["account_card"] not in paid_cards:
                continue
        elif tx["id"] not in paid_transaction_ids:
            continue
        raw_pot = tx["linked_pot"]
        normalized_pot = raw_pot.casefold().removeprefix("reserva - ").strip()
        if not normalized_pot:
            normalized_pot = tx["category"].casefold().strip()
        pot = names.get(normalized_pot)
        if pot is None and not names and raw_pot:
            pot = raw_pot
        if not pot:
            continue
        record = totals.setdefault(pot, {"amount": 0.0, "count": 0})
        value = effective_card_amount(tx, d) if tx["card_flag"] else tx["amount"]
        record["amount"] = float(record["amount"]) + value
        record["count"] = int(record["count"]) + 1
    return totals


def opening_account_balances(
    transactions: Iterable[dict[str, Any]],
    accounts: Iterable[dict[str, Any]],
    base_month: Any,
    selected_month: Any,
    *,
    cards: Iterable[dict[str, Any]] | None = None,
) -> dict[str, float]:
    """Estimated account balances at the start of a selected month.

    Earlier months are carried forward as planned flows; current-month items
    remain controlled by the user's paid/unpaid checkboxes.
    """
    txs = [normalize_transaction(t) for t in transactions if not t.get("archived", False)]
    accounts = list(accounts)
    card_banks = _card_banks(cards)
    balances = {a["name"]: _num(a.get("initial_balance")) for a in accounts}
    base = month_start(base_month)
    selected = month_start(selected_month)
    if base is None or selected is None or selected <= base:
        return balances
    selected_idx = month_id(selected)
    if selected_idx is None:
        return balances
    for d in month_range(base, month_from_id(selected_idx - 1)):
        idx = month_id(d)
        if idx is None:
            continue
        active_month = [tx for tx in txs if _is_active(tx, idx)]
        card_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for tx in active_month:
            if tx["card_flag"] == 1 and tx["type"] != "Receita":
                card_groups[tx["account_card"]].append(tx)
        invoice_values = {
            name: allocated_month_lines(lines, d)[0]
            for name, lines in card_groups.items()
            if any(isinstance(tx.get("installment_overrides"), dict) and month_key(d) in tx["installment_overrides"] for tx in lines)
        }
        for tx in active_month:
            amount = effective_card_amount(tx, d) if tx["card_flag"] else tx["amount"]
            if tx["type"] == "Receita":
                bank = _account_bank(tx)
                balances[bank] = balances.get(bank, 0.0) + amount
            elif tx["type"] == "Transferência":
                source = _account_bank(tx)
                target = tx["destination_account"]
                balances[source] = balances.get(source, 0.0) - amount
                if target:
                    balances[target] = balances.get(target, 0.0) + amount
            elif tx["card_flag"] == 1 and tx["type"] != "Receita":
                if tx["account_card"] not in invoice_values:
                    bank = _card_bank(tx, card_banks)
                    balances[bank] = balances.get(bank, 0.0) - amount
                # Overridden invoices are reconciled once per card below.
                continue
            elif tx["type"] in ("Fixo", "Débito/Pix", "Caixinha"):
                bank = _account_bank(tx)
                balances[bank] = balances.get(bank, 0.0) - amount
            elif tx["type"] == "Saque Caixinha":
                bank = _account_bank(tx)
                balances[bank] = balances.get(bank, 0.0) + amount
        for name, invoice in invoice_values.items():
            lines = card_groups[name]
            bank = _card_bank(lines[0], card_banks)
            balances[bank] = balances.get(bank, 0.0) - invoice
    return balances


def status_natural_key(item: dict[str, Any]) -> tuple[str, str, str, str, int]:
    return (
        item.get("account", ""), item.get("type", ""), item.get("member", ""),
        item.get("description", ""), round(_num(item.get("amount")) * 100),
    )


def apply_payment_statuses(
    items: list[dict[str, Any]], legacy_rows: list[dict[str, Any]],
    explicit: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Attach saved app states, falling back to the source sheet checkboxes."""
    explicit = explicit or {}
    queues: dict[tuple, deque] = defaultdict(deque)
    for row in legacy_rows:
        key = (
            row.get("account", ""), row.get("type", ""), row.get("member", ""),
            row.get("description", ""), round(_num(row.get("amount")) * 100),
        )
        queues[key].append(row)
    result = []
    for original in items:
        item = dict(original)
        if item["item_key"] in explicit:
            state = explicit[item["item_key"]]
            item["paid"] = bool(state.get("paid"))
            item["paid_on"] = state.get("paid_on")
            item["status_origin"] = "app"
        else:
            matches = queues.get(status_natural_key(item))
            old = matches.popleft() if matches else None
            item["paid"] = bool(old.get("paid")) if old else False
            item["paid_on"] = None
            item["status_origin"] = "planilha" if old else "novo"
        result.append(item)
    transfer_groups: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for item in result:
        if item.get("type") not in ("Transf. Env.", "Transf. Receb."):
            continue
        key = item.get("transaction_id")
        if key is None:
            item_key = str(item.get("item_key", ""))
            key = item_key.rsplit(":", 1)[0] if item_key.endswith((":out", ":in")) else item_key
        transfer_groups[key].append(item)
    for group in transfer_groups.values():
        priority = lambda item: 0 if item.get("type") == "Transf. Env." else 1
        explicit_states = sorted((i for i in group if i.get("status_origin") == "app"), key=priority)
        legacy_states = sorted((i for i in group if i.get("status_origin") == "planilha"), key=priority)
        if explicit_states:
            canonical = explicit_states[0]
        elif legacy_states:
            paid_legacy = [item for item in legacy_states if item.get("paid")]
            canonical = sorted(paid_legacy or legacy_states, key=priority)[0]
        else:
            canonical = sorted(group, key=priority)[0]
        for item in group:
            item["paid"] = bool(canonical.get("paid"))
            item["paid_on"] = canonical.get("paid_on")
            item["status_origin"] = canonical.get("status_origin", "novo")
    return result


def cashflow_summary(
    items: list[dict[str, Any]], accounts: Iterable[dict[str, Any]],
    opening_balances: dict[str, float], person: str = FAMILY,
    *, account_items: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Separate personal activity metrics from account-owner bank balances.

    ``account_items`` can supply the fully reconciled household ledger for bank
    balances while ``items`` remains scoped to the selected member's activity.
    """
    relevant = [a for a in accounts if person == FAMILY or a.get("owner") == person]
    paid = [item for item in items if item.get("paid")]
    account_source = items if account_items is None else list(account_items)
    account_paid = [item for item in account_source if item.get("paid")]
    changes: dict[str, float] = defaultdict(float)
    for item in account_paid:
        changes[item.get("account", "")] += float(item.get("signed_amount") or 0)
    opening_total = sum(float(opening_balances.get(a["name"], 0) or 0) for a in relevant)
    account_balances = [
        {"account": a["name"], "owner": a.get("owner", ""),
         "opening": float(opening_balances.get(a["name"], 0) or 0),
         "paid_change": changes.get(a["name"], 0.0),
         "live": float(opening_balances.get(a["name"], 0) or 0) + changes.get(a["name"], 0.0)}
        for a in relevant
    ]
    transfer_groups: dict[Any, float] = {}
    for item in paid:
        if item.get("type") not in ("Transf. Env.", "Transf. Receb."):
            continue
        key = item.get("transaction_id")
        if key is None:
            item_key = str(item.get("item_key", ""))
            key = item_key.rsplit(":", 1)[0] if item_key.endswith((":out", ":in")) else item_key
        transfer_groups[key] = max(transfer_groups.get(key, 0.0), float(item.get("amount") or 0))
    transfer_items = [i for i in paid if i.get("type") in ("Transf. Env.", "Transf. Receb.")]
    return {
        "opening_total": opening_total,
        "income_paid": sum(float(i["amount"]) for i in paid if i.get("type") == "Receita"),
        "expenses_paid": sum(float(i["amount"]) for i in paid if i.get("type") in ("Fixo", "Débito/Pix", "Fatura cartão")),
        "live_total": opening_total + sum(row["paid_change"] for row in account_balances),
        "internal_transfers": sum(transfer_groups.values()),
        "transfer_net": sum(float(i.get("signed_amount") or 0) for i in transfer_items),
        "pot_contributions": sum(float(i["amount"]) for i in paid if i.get("type") == "Caixinha"),
        "pot_withdrawals": sum(float(i["amount"]) for i in paid if i.get("type") == "Saque Caixinha"),
        "pending_items": sum(1 for item in items if not item.get("paid")),
        "account_balances": account_balances,
    }


def daily_activity(
    transactions: Iterable[dict[str, Any]], start_date: Any, end_date: Any,
    person: str = FAMILY,
) -> list[dict[str, Any]]:
    """Expand dated transactions and recurring rows over a chosen interval."""
    first = as_date(start_date)
    last = as_date(end_date)
    if first is None or last is None or first > last:
        return []
    rows = [normalize_transaction(t) for t in transactions if not t.get("archived", False)]
    expanded: list[dict[str, Any]] = []
    recurring_types = {"Receita", "Fixo", "Reserva", "Caixinha", "Saque Caixinha"}

    def emit(tx: dict[str, Any], event_date: date, *, recurrence: bool = False) -> None:
        if not first <= event_date <= last:
            return
        if person != FAMILY:
            if tx["card_flag"] and not _card_ok(tx, person):
                return
            if tx["type"] != "Transferência" and not tx["card_flag"] and tx["member"] != person:
                return
            if tx["type"] == "Transferência" and tx["member"] != person:
                return
        tx_type = tx["type"]
        if tx_type == "Receita":
            signed = tx["amount"]
        elif tx_type in ("Fixo", "Débito/Pix", "Parcelado", "Crédito à vista", "Caixinha"):
            signed = -tx["amount"] * (tx["installment_count"] if tx_type == "Parcelado" else 1)
        elif tx_type == "Saque Caixinha":
            signed = tx["amount"]
        else:
            signed = 0.0
        if tx_type == "Parcelado":
            event_amount = tx["total_amount"]
            label = "Compra parcelada — valor total"
        elif tx_type == "Transferência":
            event_amount = tx["amount"]
            label = "Transferência interna"
            if person == FAMILY:
                signed = 0.0
        else:
            event_amount = tx["amount"]
            label = "Recorrente" if recurrence else tx_type
        # Uma ocorrência recorrente expandida é uma previsão de calendário,
        # não uma confirmação de que houve pagamento naquela data.
        displayed_precision = "recurrence" if recurrence else tx["date_precision"]
        expanded.append({
            "transaction_date": event_date,
            "date_label": f"{event_date.strftime('%d/%m/%Y')} (prevista: recorrência)" if recurrence else event_date.strftime("%d/%m/%Y"),
            "date_precision": displayed_precision,
            "id": tx["id"],
            "member": tx["member"],
            "category": tx["category"],
            "description": tx["description"],
            "type": tx_type,
            "event_type": label,
            "account_card": tx["account_card"],
            "amount": event_amount,
            "signed_amount": signed,
            "recurrence": recurrence,
        })

    for tx in rows:
        if tx["start_id"] is None:
            continue
        actual_date = tx["transaction_date"] or tx["start_month"]
        if actual_date is None:
            continue
        if tx["type"] in recurring_types and (tx["end_id"] == INFINITE_MONTH or tx["end_id"] > tx["start_id"]):
            low = max(tx["start_id"], month_id(first))
            high = min(tx["end_id"], month_id(last))
            for idx in range(low, high + 1):
                m = month_from_id(idx)
                day = min(actual_date.day, monthrange(m.year, m.month)[1])
                emit(tx, date(m.year, m.month, day), recurrence=True)
        else:
            emit(tx, actual_date)
    return sorted(expanded, key=lambda row: (row["transaction_date"], row["id"], row["event_type"]))

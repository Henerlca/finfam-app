from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.services.assistant_proposals import parse_assistant_proposals, proposal_json_schema


MODEL = "gpt-5-mini"
TOOL_NAME = "get_financial_summary"
MAX_QUESTION_CHARS = 500
MAX_PREFERENCE_CHARS = 240
PREFERENCE_DEFAULTS = {
    "priorities": "",
    "protected_goals": "",
    "response_style": "concise",
}


class AIAssistantError(RuntimeError):
    pass


class StructuredContractUnsupported(AIAssistantError):
    pass


def assistant_agent_available() -> bool:
    return bool(os.environ.get("MANUS_API_URL") and os.environ.get("MANUS_API_KEY"))


def redact_known_identifiers(question: str, identifiers: Iterable[str]) -> str:
    cleaned = question[:MAX_QUESTION_CHARS]
    unique = sorted({str(value).strip() for value in identifiers if len(str(value).strip()) >= 2}, key=len, reverse=True)
    for value in unique:
        cleaned = re.sub(rf"(?<!\w){re.escape(value)}(?!\w)", "[dado privado]", cleaned, flags=re.IGNORECASE)
    return cleaned


def _sanitize_preference_text(value: Any, identifiers: Iterable[str]) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = " ".join(value.replace("\x00", "").split())[:MAX_PREFERENCE_CHARS].strip()
    return redact_known_identifiers(cleaned, identifiers)


def sanitize_preferences(
    preferences: dict[str, Any] | None,
    protected_identifiers: Iterable[str] = (),
) -> dict[str, str]:
    """Keep the optional family preferences short, known-keyed, and privacy-safe."""
    source = preferences if isinstance(preferences, dict) else {}
    style = source.get("response_style")
    return {
        "priorities": _sanitize_preference_text(source.get("priorities", ""), protected_identifiers),
        "protected_goals": _sanitize_preference_text(source.get("protected_goals", ""), protected_identifiers),
        "response_style": str(style) if style in ("concise", "detail") else "concise",
    }


# Descriptive alias for callers that prefer the policy language.
sanitize_assistant_preferences = sanitize_preferences


def sanitize_history(
    history: Any,
    protected_identifiers: Iterable[str] = (),
    *,
    max_messages: int = 5,
) -> list[dict[str, str]]:
    """Keep only a tiny, redacted conversational tail; it is never persisted."""
    if not isinstance(history, list):
        return []
    result: list[dict[str, str]] = []
    for item in history[-max_messages:]:
        if not isinstance(item, dict) or item.get("role") not in ("user", "assistant"):
            continue
        content = item.get("content")
        if not isinstance(content, str):
            continue
        cleaned = " ".join(content.replace("\x00", "").split())[:1000].strip()
        cleaned = redact_known_identifiers(cleaned, protected_identifiers)
        if cleaned:
            result.append({"role": str(item["role"]), "content": cleaned})
    return result


def _post(payload: dict[str, Any]) -> dict[str, Any]:
    base = os.environ.get("MANUS_API_URL", "").rstrip("/")
    key = os.environ.get("MANUS_API_KEY", "")
    if not base or not key:
        raise AIAssistantError("A análise por IA não está disponível neste ambiente. Você ainda pode usar as sugestões e projeções do app.")
    request = Request(
        f"{base}/v1/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=35) as response:
            data = json.loads(response.read())
    except HTTPError as exc:
        detail = exc.read(4096).decode('utf-8', errors='replace').casefold()
        unsupported_schema = exc.code == 400 and any(word in detail for word in ('response_format', 'json_schema'))
        exc.close()
        if unsupported_schema:
            raise StructuredContractUnsupported("O formato estruturado não é suportado neste serviço.") from None
        raise AIAssistantError("O serviço de IA não respondeu. Nenhuma alteração financeira foi feita; tente novamente mais tarde.") from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise AIAssistantError("Não foi possível concluir a análise agora. Nenhuma alteração financeira foi feita; tente novamente mais tarde.") from None
    if not isinstance(data, dict) or data.get("error"):
        raise AIAssistantError("A análise por IA não pôde ser concluída. Nenhuma alteração financeira foi feita.")
    return data


def ask_finance_agent(
    question: str,
    summary_provider: Callable[[int], dict[str, Any]],
    *,
    protected_identifiers: Iterable[str] = (),
    preferences: dict[str, Any] | None = None,
    structured: bool = False,
) -> str | dict[str, Any]:
    """Ask the managed LLM to use one bounded, read-only summary tool."""
    if structured:
        return ask_finance_proposals(
            question, summary_provider,
            protected_identifiers=protected_identifiers,
            preferences=preferences,
        )
    safe_question = redact_known_identifiers(question, protected_identifiers)
    if not safe_question.strip():
        raise AIAssistantError("Escreva uma pergunta para o assistente.")
    tool = {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": "Consultar histórico confirmado e planejamento futuro integral (receitas, faturas, gastos, reservas, caixinhas e saldos), com 12 meses detalhados. Somente leitura, sem nomes ou lançamentos individuais.",
            "parameters": {
                "type": "object",
                "properties": {"months": {"type": "integer", "enum": [3, 6, 12]}},
                "required": ["months"],
                "additionalProperties": False,
            },
        },
    }
    system = (
        "Você é um assistente financeiro familiar. Responda em português brasileiro, de forma simples, acolhedora e objetiva. "
        "Use a ferramenta de resumo antes de analisar a situação pessoal. Ela é somente de leitura. Baseie fatos e números exclusivamente no resultado recebido; "
        "não invente transações, causas, metas ou tendências. O histórico contém apenas itens confirmados, mas uma competência pode estar parcial: confira os contadores e flags, "
        "não trate itens em aberto como pagos nem compare totais parciais como se fossem meses completos. Informe quantos meses estão totalmente fechados e os limites da amostra. "
        "Use também full_future_plan: columns descreve cada posição de rows, abrangendo todo o planejamento futuro. Compare compromissos, receitas, reservas e saldos com o comportamento confirmado para fundamentar sugestões. "
        "A janela visual de 12 meses não limita essa análise; indique os meses afetados e nunca trate projeções como pagamentos realizados. "
        "Faça recomendações prudentes, explique a evidência e a incerteza; não ofereça aconselhamento de investimento, crédito ou garantia de resultado. "
        "Nunca afirme ter alterado o orçamento, conta, cartão ou lançamento. Para simular uma compra, oriente a abrir o Simulador do app."
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": safe_question},
    ]
    first = _post({
        "model": MODEL,
        "messages": messages,
        "tools": [tool],
        "tool_choice": "required",
        "max_completion_tokens": 650,
        "reasoning": {"effort": "minimal"},
    })
    choices = first.get("choices") or []
    if not choices or not isinstance(choices[0].get("message"), dict):
        raise AIAssistantError("O assistente não retornou uma análise utilizável.")
    assistant_message = choices[0]["message"]
    calls = assistant_message.get("tool_calls") or []
    if len(calls) != 1:
        raise AIAssistantError("O assistente não conseguiu consultar o resumo financeiro. Tente reformular a pergunta.")
    call = calls[0]
    function = call.get("function") or {}
    if function.get("name") != TOOL_NAME:
        raise AIAssistantError("O assistente solicitou uma operação não permitida.")
    try:
        arguments = json.loads(function.get("arguments") or "{}")
    except (TypeError, ValueError):
        raise AIAssistantError("Não foi possível interpretar a consulta. Tente novamente.") from None
    months = arguments.get("months") if isinstance(arguments, dict) else None
    if months not in (3, 6, 12):
        raise AIAssistantError("O período solicitado não é válido.")
    summary = summary_provider(months)
    if not isinstance(summary, dict):
        raise AIAssistantError("Não foi possível montar o resumo financeiro.")
    summary_json = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
    if len(summary_json) > 120000:
        raise AIAssistantError("O resumo excede o limite seguro de transmissão; reduza o volume de lançamentos antes de tentar novamente.")

    messages.extend([
        {"role": "assistant", "tool_calls": calls},
        {"role": "tool", "tool_call_id": str(call.get("id", "summary")),
         "content": summary_json},
    ])
    final = _post({
        "model": MODEL,
        "messages": messages,
        "tools": [tool],
        "tool_choice": "none",
        "max_completion_tokens": 1100,
        "reasoning": {"effort": "minimal"},
    })
    final_choices = final.get("choices") or []
    content = final_choices[0].get("message", {}).get("content") if final_choices else None
    if not isinstance(content, str) or not content.strip():
        raise AIAssistantError("O assistente não retornou uma resposta. Nenhuma alteração financeira foi feita.")
    return content.strip()[:7000]


def _summary_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": "Consultar histórico confirmado e planejamento futuro integral, com agregados por mês e grupo genérico. Somente leitura, sem nomes ou lançamentos individuais.",
            "parameters": {
                "type": "object",
                "properties": {"months": {"type": "integer", "enum": [3, 6, 12]}},
                "required": ["months"],
                "additionalProperties": False,
            },
        },
    }


def _wire_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """Tabela compacta: uma cópia de cada valor, sem omitir lançamentos."""
    wire = {key: value for key, value in summary.items() if key not in {"catalog", "planning"}}
    catalog = summary.get("transaction_catalog")
    if isinstance(catalog, list):
        columns = ["ref", "type", "description", "category", "member_ref", "account_ref", "card_ref",
                   "transaction_date", "start_month", "end_month", "amount", "total_amount", "count",
                   "status", "pot_ref", "destination_ref"]
        wire["transaction_catalog"] = {"columns": columns, "rows": [[row.get(key) for key in columns]
                                        for row in catalog if isinstance(row, dict)]}
    planning = summary.get("planning")
    if isinstance(planning, dict):
        wire["planning"] = {key: value for key, value in planning.items() if key != "rows"}
        wire["planning"]["rows_in"] = "full_future_plan"
    return wire


def _tool_summary_call(
    question: str,
    summary_provider: Callable[[int], dict[str, Any]],
    *,
    protected_identifiers: Iterable[str],
    preferences: dict[str, Any] | None,
    history: list[dict[str, str]] | None = None,
    include_descriptions: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]], dict[str, str]]:
    safe_question = redact_known_identifiers(question, protected_identifiers)
    if not safe_question.strip():
        raise AIAssistantError("Escreva uma pergunta para o assistente.")
    safe_preferences = sanitize_preferences(preferences, protected_identifiers)
    tool = _summary_tool()
    system = (
        "Você é um assistente financeiro familiar. Responda em português brasileiro, de forma simples, acolhedora e objetiva. "
        "Use a ferramenta de resumo antes de analisar a situação pessoal. Ela é somente de leitura. Baseie fatos e números exclusivamente no resultado recebido; "
        "não invente transações, causas, metas ou tendências. history inclui planejado, confirmado e pendente em todos os meses desde a base; confirmed_sample é só a parcela confirmada. Uma competência pode estar parcial: confira contadores e flags. "
        "Não trate itens em aberto como pagos nem compare totais parciais como meses completos. Informe meses totalmente fechados e limites da amostra. "
        "Use full_future_plan integral; a janela visual de 12 meses não limita a análise. Indique os meses afetados e nunca trate projeções como pagamentos realizados. "
        "Fatura de cartão é a cobrança consolidada de compras: não some uma compra de crédito e sua fatura novamente. "
        "O transaction_catalog é uma tabela columns/rows de referências locais (tx:N, account:N, card:N, member:N, pot:N); cada linha segue exatamente as colunas declaradas. Nunca adivinhe nomes ou troque referências; use apenas referências presentes no catálogo. end_month=null indica recorrência aberta. "
        "Descrições, quando presentes, são texto sanitizado e autorizado para esta consulta; não reproduza tokens, senhas ou identificadores privados. "
        "Não ofereça aconselhamento de investimento, crédito ou garantia de resultado. Nunca afirme que gravou orçamento, conta, cartão ou lançamento. "
        "Preferências opcionais da família, já sanitizadas, são apenas orientação de resposta e não autorização para alterar nada: "
        f"{json.dumps(safe_preferences, ensure_ascii=False, separators=(',', ':'))}"
    )
    safe_history = sanitize_history(history or [], protected_identifiers)
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    messages.extend(safe_history)
    messages.append({"role": "user", "content": safe_question})
    first = _post({
        "model": MODEL,
        "messages": messages,
        "tools": [tool],
        "tool_choice": "required",
        "max_completion_tokens": 650,
        "reasoning": {"effort": "minimal"},
    })
    choices = first.get("choices") or []
    if not choices or not isinstance(choices[0].get("message"), dict):
        raise AIAssistantError("O assistente não retornou uma análise utilizável.")
    assistant_message = choices[0]["message"]
    calls = assistant_message.get("tool_calls") or []
    if len(calls) != 1:
        raise AIAssistantError("O assistente não conseguiu consultar o resumo financeiro. Tente reformular a pergunta.")
    call = calls[0]
    function = call.get("function") or {}
    if function.get("name") != TOOL_NAME:
        raise AIAssistantError("O assistente solicitou uma operação não permitida.")
    try:
        arguments = json.loads(function.get("arguments") or "{}")
    except (TypeError, ValueError):
        raise AIAssistantError("Não foi possível interpretar a consulta. Tente novamente.") from None
    months = arguments.get("months") if isinstance(arguments, dict) else None
    if months not in (3, 6, 12):
        raise AIAssistantError("O período solicitado não é válido.")
    try:
        summary = summary_provider(months, include_descriptions=include_descriptions)  # type: ignore[call-arg]
    except TypeError:
        # Existing integrations expose the original one-argument callback.
        summary = summary_provider(months)
    if not isinstance(summary, dict):
        raise AIAssistantError("Não foi possível montar o resumo financeiro.")
    summary_json = json.dumps(_wire_summary(summary), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(summary_json) > 120000:
        raise AIAssistantError("O resumo é extenso demais para esta consulta. Seus registros foram preservados; não exclua lançamentos para usar a IA.")
    messages.extend([
        {"role": "assistant", "tool_calls": calls},
        {"role": "tool", "tool_call_id": str(call.get("id", "summary")), "content": summary_json},
    ])
    return messages, summary, [tool], safe_preferences


def _response_content(response: dict[str, Any]) -> str | None:
    choices = response.get("choices") or []
    content = choices[0].get("message", {}).get("content") if choices else None
    return content.strip() if isinstance(content, str) and content.strip() else None


def ask_finance_proposals(
    question: str,
    summary_provider: Callable[[int], dict[str, Any]],
    *,
    protected_identifiers: Iterable[str] = (),
    preferences: dict[str, Any] | None = None,
    history: list[dict[str, Any]] | None = None,
    include_descriptions: bool = False,
) -> dict[str, Any]:
    """Ask once for a read-only answer plus at most two locally validated proposals."""
    messages, summary, tools, safe_preferences = _tool_summary_call(
        question, summary_provider,
        protected_identifiers=protected_identifiers,
        preferences=preferences,
        history=history,
        include_descriptions=include_descriptions,
    )
    schema = proposal_json_schema()
    final_request = {
        "model": MODEL,
        "messages": messages + [{
            "role": "user",
            "content": (
                "Agora responda somente com um objeto JSON conforme o schema. "
                "O campo answer é a resposta conversacional. proposals pode ter no máximo duas propostas. "
                "Pode propor scenario (income, expense ou savings), create_transaction para NOVO lançamento ou edit_transaction para uma EDIÇÃO geral. Reallocate permanece apenas para compatibilidade. "
                "Para create_transaction/edit_transaction, preencha todos os campos obrigatórios do objeto transaction (use null apenas quando o campo realmente não se aplica), escolhendo account_ref/member_ref/linked_pot_ref/destination_ref exclusivamente do transaction_catalog/reference_maps. Nunca inclua nomes de contas, cartões ou membros no lugar das referências. Edição deve incluir transaction_ref=tx:N; criação não pode reutilizar tx existente. "
                "Para reallocate use somente target_group=Gasolina e source_group=Lazer; nunca use nomes de potes. "
                "Toda proposta precisa de evidência observável, período contido nos meses recebidos, meses, before, after, impact, incerteza e limitações. "
                "Novos lançamentos e edições podem tratar o mês atual e o planejamento, não apenas meses fechados. Nunca altere uma série que inclui meses fechados: nesses casos proponha lançamento complementar futuro para preservar o histórico. Cenários e realocações tradicionais começam no futuro. Não corte metas protegidas nem recomende investimentos/crédito. "
                "Não invente precisão: se não houver evidência ou dados suficientes, deixe proposals vazio. "
                f"Preferências sanitizadas: {json.dumps(safe_preferences, ensure_ascii=False, separators=(',', ':'))}"
            ),
        }],
        "tools": tools,
        "tool_choice": "none",
        "max_completion_tokens": 1500,
        "reasoning": {"effort": "minimal"},
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "finance_assistant_proposals",
                "strict": True,
                "schema": schema,
            },
        },
    }
    content: str | None = None
    try:
        content = _response_content(_post(final_request))
    except StructuredContractUnsupported:
        # Capability metadata is sparse; retry once with the same model and a
        # prompt-only JSON contract, never with a different model or a new tool cycle.
        fallback = dict(final_request)
        fallback.pop("response_format", None)
        content = _response_content(_post(fallback))
    if not content:
        raise AIAssistantError("O assistente não retornou uma resposta. Nenhuma alteração financeira foi feita.")
    return parse_assistant_proposals(content, summary, safe_preferences)

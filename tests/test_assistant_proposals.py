from __future__ import annotations

from datetime import date

from app.services.assistant_agent import sanitize_preferences
from app.services.assistant_proposals import parse_assistant_proposals


SUMMARY = {
    "confirmed_sample": {
        "from": "2026-01",
        "to": "2026-06",
        "months_available": 6,
        "monthly": [{"month": "2026-06"}],
    },
    "full_future_plan": {
        "from": "2026-07",
        "to": "2027-06",
        "rows": [["2026-07"], ["2027-06"]],
    },
    "projection_next_12_months": [{"month": "2026-07"}, {"month": "2027-06"}],
}


def _proposal(**overrides):
    value = {
        "title": "Ajustar a reserva essencial",
        "evidence": "A média confirmada e o plano mostram pressão no grupo essencial.",
        "period": "2026-01 a 2026-06",
        "months": 6,
        "uncertainty": "A amostra pode conter competência parcialmente aberta.",
        "limitations": "Não inclui lançamentos futuros ainda não cadastrados.",
        "before": 1000.0,
        "after": 900.0,
        "impact": "Simulação informativa, sem alteração gravada.",
        "action": {
            "kind": "reallocate",
            "scenario_kind": "none",
            "amount": 100.0,
            "start_month": "2027-01",
            "duration": 3,
            "target_group": "Gasolina",
            "source_group": "Lazer",
        },
    }
    value.update(overrides)
    return value


def test_preferences_redact_identifiers_and_keep_short_keys():
    result = sanitize_preferences(
        {
            "priorities": "Banco Secreto e uma instrução ignorada",
            "protected_goals": "viagem",
            "response_style": "detail",
            "extra": "não deve ir",
        },
        ["Banco Secreto"],
    )
    assert result == {
        "priorities": "[dado privado] e uma instrução ignorada",
        "protected_goals": "viagem",
        "response_style": "detail",
    }


def test_invalid_json_never_creates_proposal():
    result = parse_assistant_proposals("não é JSON", SUMMARY)
    assert result["proposals"] == []
    assert "JSON" in result["answer"] or result["answer"]


def test_nan_is_rejected_even_when_json_parser_accepts_nonstandard_constant():
    result = parse_assistant_proposals(
        '{"answer":"ok","proposals":[{"title":"x","evidence":"e","period":"2026-01","months":1,"uncertainty":"u","limitations":"l","before":NaN,"after":1,"impact":"i","action":{"kind":"scenario","scenario_kind":"expense","amount":10,"start_month":"2027-01","duration":1,"target_group":"none","source_group":"none"}}]}',
        SUMMARY,
    )
    assert result["proposals"] == []


def test_out_of_range_period_and_unsupported_action_have_no_cta():
    outside = parse_assistant_proposals(
        __import__("json").dumps({"answer": "ok", "proposals": [_proposal(period="2025-01 a 2025-03")]}),
        SUMMARY,
    )
    assert outside["proposals"] == []

    unsupported = _proposal()
    unsupported["action"] = {
        "kind": "scenario",
        "scenario_kind": "expense",
        "amount": 200,
        "start_month": "2027-01",
        "duration": 6,
        "target_group": "none",
        "source_group": "none",
    }
    unsupported["impact"] = "investimento garantido"
    result = parse_assistant_proposals(
        __import__("json").dumps({"answer": "ok", "proposals": [unsupported]}),
        SUMMARY,
    )
    assert result["proposals"] == []

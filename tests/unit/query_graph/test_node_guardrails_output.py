"""Unit tests for node_guardrails_output.

Tests cover:
- chitchat halt → returns out-of-scope Polish message
- no_results=True → returns "not found" Polish message
- add_disclaimer=True → disclaimer appended to answer
- Normal answer → pass through unchanged
- LLM guardrails disabled → pass through (existing behaviour unchanged)
- LLM guardrails enabled + safe=True → answer unchanged
- LLM guardrails enabled + safe=False → answer replaced with refusal
- LLM guardrails enabled + LLM fails → fail-open (original answer kept)
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.graphs.query_graph.nodes.node_guardrails_output import (
    _LLM_REFUSAL_ANSWER,
    _NO_RESULTS_ANSWER,
    _OUT_OF_SCOPE_ANSWER,
    node_guardrails_output,
)
from src.graphs.query_graph.state import QueryState

TENANT_ID = uuid.uuid4()
LLM_MODEL_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()
PIPELINE_ID = uuid.uuid4()


def _make_state(**kwargs: object) -> QueryState:
    defaults: dict[str, object] = {
        "question": "Jakie dokumenty?",
        "conversation_id": uuid.uuid4(),
        "tenant_id": TENANT_ID,
        "allowed_collection_ids": [COLLECTION_ID],
        "pipeline_id": PIPELINE_ID,
        "llm_model_id": LLM_MODEL_ID,
        "collection_ids": [COLLECTION_ID],
    }
    defaults.update(kwargs)
    return QueryState(**defaults)  # type: ignore[arg-type]


_EMPTY_CONFIG: dict = {"configurable": {}}


# ---------------------------------------------------------------------------
# Existing deterministic tests (must remain green)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chitchat_halt_returns_out_of_scope_message() -> None:
    """halt=True with chitchat intent returns the out-of-scope Polish message."""
    state = _make_state(halt=True, intent="chitchat")
    result = await node_guardrails_output(state, _EMPTY_CONFIG)
    assert result["answer"] == _OUT_OF_SCOPE_ANSWER


@pytest.mark.asyncio
async def test_out_of_scope_halt_returns_out_of_scope_message() -> None:
    """halt=True with out_of_scope intent returns the out-of-scope message."""
    state = _make_state(halt=True, intent="out_of_scope")
    result = await node_guardrails_output(state, _EMPTY_CONFIG)
    assert result["answer"] == _OUT_OF_SCOPE_ANSWER


@pytest.mark.asyncio
async def test_no_results_returns_not_found_message() -> None:
    """no_results=True returns the Polish 'not found in documents' message."""
    state = _make_state(no_results=True, halt=False)
    result = await node_guardrails_output(state, _EMPTY_CONFIG)
    assert result["answer"] == _NO_RESULTS_ANSWER


@pytest.mark.asyncio
async def test_add_disclaimer_appends_to_answer() -> None:
    """add_disclaimer=True in guardrails_config appends disclaimer to answer."""
    answer_text = "Pacjent powinien zgłosić się do rejestracji z dowodem tożsamości."
    state = _make_state(
        guardrails_config={"add_disclaimer": True},
        answer=answer_text,
        halt=False,
        no_results=False,
    )
    result = await node_guardrails_output(state, _EMPTY_CONFIG)
    # Answer should be extended with disclaimer
    assert result["answer"].startswith(answer_text)
    assert "\n\n" in result["answer"]
    assert len(result["answer"]) > len(answer_text)


@pytest.mark.asyncio
async def test_normal_answer_passes_through_unchanged() -> None:
    """When no special case applies, state.answer is returned unchanged."""
    answer_text = "Godziny otwarcia poradni: 8:00–16:00."
    state = _make_state(
        answer=answer_text,
        halt=False,
        no_results=False,
        guardrails_config={},
    )
    result = await node_guardrails_output(state, _EMPTY_CONFIG)
    assert result["answer"] == answer_text


@pytest.mark.asyncio
async def test_no_answer_and_no_special_case_returns_empty_string() -> None:
    """If answer is None and no special case, returns empty string."""
    state = _make_state(
        answer=None,
        halt=False,
        no_results=False,
        guardrails_config={},
    )
    result = await node_guardrails_output(state, _EMPTY_CONFIG)
    assert result["answer"] == ""


@pytest.mark.asyncio
async def test_add_disclaimer_false_does_not_append() -> None:
    """add_disclaimer=False does not modify the answer."""
    answer_text = "Dokumenty wymagane do operacji."
    state = _make_state(
        guardrails_config={"add_disclaimer": False},
        answer=answer_text,
        halt=False,
        no_results=False,
    )
    result = await node_guardrails_output(state, _EMPTY_CONFIG)
    assert result["answer"] == answer_text


# ---------------------------------------------------------------------------
# LLM guardrails helpers
# ---------------------------------------------------------------------------


def _make_model_record() -> MagicMock:
    record = MagicMock()
    record.id = LLM_MODEL_ID
    record.model_id = "llama3.2"
    record.endpoint_url = "http://ollama:11434/v1"
    return record


def _make_db(model_record: object) -> AsyncMock:
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = model_record
    db.execute = AsyncMock(return_value=result)
    return db


def _make_llm_response(safe: bool, reason_code: str | None = None) -> MagicMock:
    payload: dict[str, object] = {"safe": safe}
    if reason_code is not None:
        payload["reason_code"] = reason_code
    choice = MagicMock()
    choice.message.content = json.dumps(payload)
    response = MagicMock()
    response.choices = [choice]
    return response


def _make_config_with_llm(llm: object, db: object) -> dict:
    return {"configurable": {"llm": llm, "db": db}}


# ---------------------------------------------------------------------------
# LLM guardrails — new tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_guardrails_disabled_passes_through() -> None:
    """When llm_guardrails_enabled is False (default), no LLM call is made."""
    answer_text = "Rejestracja czynna od 8:00 do 16:00."
    llm = AsyncMock()
    db = _make_db(_make_model_record())

    state = _make_state(
        answer=answer_text,
        halt=False,
        no_results=False,
        guardrails_config={},  # llm_guardrails_enabled not set → defaults to False
    )
    result = await node_guardrails_output(state, _make_config_with_llm(llm, db))

    assert result["answer"] == answer_text
    llm.chat_completion.assert_not_called()


@pytest.mark.asyncio
async def test_llm_guardrails_enabled_safe_answer_unchanged() -> None:
    """LLM guardrails enabled + LLM says safe=True → answer passes through unchanged."""
    answer_text = "Rejestracja czynna od 8:00 do 16:00."
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(safe=True))

    state = _make_state(
        answer=answer_text,
        halt=False,
        no_results=False,
        guardrails_config={"llm_guardrails_enabled": True},
    )
    result = await node_guardrails_output(state, _make_config_with_llm(llm, db))

    assert result["answer"] == answer_text
    llm.chat_completion.assert_called_once()


@pytest.mark.asyncio
async def test_llm_guardrails_enabled_unsafe_answer_replaced_with_refusal() -> None:
    """LLM guardrails enabled + LLM says safe=False → answer replaced with refusal message."""
    unsafe_answer = "Proszę wziąć 500 mg ibuprofenu dwa razy dziennie."
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(
        return_value=_make_llm_response(safe=False, reason_code="unsafe_medical_advice")
    )

    state = _make_state(
        answer=unsafe_answer,
        halt=False,
        no_results=False,
        guardrails_config={"llm_guardrails_enabled": True},
    )
    result = await node_guardrails_output(state, _make_config_with_llm(llm, db))

    assert result["answer"] == _LLM_REFUSAL_ANSWER
    assert result["answer"] != unsafe_answer


@pytest.mark.asyncio
async def test_llm_guardrails_enabled_llm_fails_fail_open() -> None:
    """LLM guardrails enabled + LLM call raises → fail-open: original answer kept."""
    answer_text = "Rejestracja czynna od 8:00 do 16:00."
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(side_effect=RuntimeError("LLM unreachable"))

    state = _make_state(
        answer=answer_text,
        halt=False,
        no_results=False,
        guardrails_config={"llm_guardrails_enabled": True},
    )
    # Must not raise; pipeline continues with original answer
    result = await node_guardrails_output(state, _make_config_with_llm(llm, db))

    assert result["answer"] == answer_text


@pytest.mark.asyncio
async def test_llm_guardrails_skipped_for_out_of_scope_case() -> None:
    """LLM guardrails are not called when halt=True (out_of_scope deterministic path)."""
    llm = AsyncMock()
    db = _make_db(_make_model_record())

    state = _make_state(
        halt=True,
        intent="out_of_scope",
        guardrails_config={"llm_guardrails_enabled": True},
    )
    result = await node_guardrails_output(state, _make_config_with_llm(llm, db))

    assert result["answer"] == _OUT_OF_SCOPE_ANSWER
    llm.chat_completion.assert_not_called()


@pytest.mark.asyncio
async def test_llm_guardrails_skipped_for_no_results_case() -> None:
    """LLM guardrails are not called when no_results=True (deterministic path)."""
    llm = AsyncMock()
    db = _make_db(_make_model_record())

    state = _make_state(
        no_results=True,
        halt=False,
        guardrails_config={"llm_guardrails_enabled": True},
    )
    result = await node_guardrails_output(state, _make_config_with_llm(llm, db))

    assert result["answer"] == _NO_RESULTS_ANSWER
    llm.chat_completion.assert_not_called()


@pytest.mark.asyncio
async def test_llm_guardrails_with_disclaimer_unsafe_replaced() -> None:
    """LLM guardrails run after disclaimer is appended; unsafe content → refusal."""
    unsafe_answer = "Odstawić metforminę natychmiast."
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(
        return_value=_make_llm_response(safe=False, reason_code="unsafe_medical_advice")
    )

    state = _make_state(
        answer=unsafe_answer,
        halt=False,
        no_results=False,
        guardrails_config={"add_disclaimer": True, "llm_guardrails_enabled": True},
    )
    result = await node_guardrails_output(state, _make_config_with_llm(llm, db))

    assert result["answer"] == _LLM_REFUSAL_ANSWER


# ---------------------------------------------------------------------------
# ADR-017 v2: llm_check path — structured output with full GuardrailsDecision
# ---------------------------------------------------------------------------


def _make_llm_response_v2(
    safe: bool,
    modifications: list[str] | None = None,
    disclaimer_added: bool = False,
    pii_detected: bool = False,
    reasoning: str = "factually grounded",
) -> MagicMock:
    payload: dict[str, object] = {
        "safe": safe,
        "modifications": modifications or [],
        "disclaimer_added": disclaimer_added,
        "pii_detected": pii_detected,
        "reasoning": reasoning,
    }
    choice = MagicMock()
    choice.message.content = json.dumps(payload)
    response = MagicMock()
    response.choices = [choice]
    return response


@pytest.mark.asyncio
async def test_llm_check_safe_no_modifications_passes_through() -> None:
    """llm_check=True + safe=True + no modifications → answer unchanged."""
    answer_text = "Gabinety zabiegowe czynne od 7:00."
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response_v2(safe=True))

    state = _make_state(
        answer=answer_text,
        halt=False,
        no_results=False,
        guardrails_config={"llm_check": True},
    )
    result = await node_guardrails_output(state, _make_config_with_llm(llm, db))

    assert result["answer"] == answer_text
    llm.chat_completion.assert_called_once()


@pytest.mark.asyncio
async def test_llm_check_safe_with_modifications_applies_first_modification() -> None:
    """llm_check=True + safe=True + modifications → first modification replaces answer."""
    original_answer = "Gabinety czynne od 7."
    modified_answer = "Gabinety zabiegowe czynne od 7:00."
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(
        return_value=_make_llm_response_v2(safe=True, modifications=[modified_answer])
    )

    state = _make_state(
        answer=original_answer,
        halt=False,
        no_results=False,
        guardrails_config={"llm_check": True},
    )
    result = await node_guardrails_output(state, _make_config_with_llm(llm, db))

    assert result["answer"] == modified_answer


@pytest.mark.asyncio
async def test_llm_check_safe_disclaimer_added_by_llm() -> None:
    """llm_check=True + safe=True + disclaimer_added=True → disclaimer appended."""
    answer_text = "Konsultacja u lekarza jest obowiązkowa przed zabiegiem."
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(
        return_value=_make_llm_response_v2(safe=True, disclaimer_added=True)
    )

    state = _make_state(
        answer=answer_text,
        halt=False,
        no_results=False,
        guardrails_config={"llm_check": True},  # add_disclaimer NOT set in rule-based
    )
    result = await node_guardrails_output(state, _make_config_with_llm(llm, db))

    assert result["answer"].startswith(answer_text)
    assert len(result["answer"]) > len(answer_text)


@pytest.mark.asyncio
async def test_llm_check_unsafe_replaced_with_refusal() -> None:
    """llm_check=True + safe=False → answer replaced with refusal."""
    unsafe_answer = "Dawka insuliny to 10 jednostek dziennie."
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(
        return_value=_make_llm_response_v2(safe=False, pii_detected=False)
    )

    state = _make_state(
        answer=unsafe_answer,
        halt=False,
        no_results=False,
        guardrails_config={"llm_check": True},
    )
    result = await node_guardrails_output(state, _make_config_with_llm(llm, db))

    assert result["answer"] == _LLM_REFUSAL_ANSWER


@pytest.mark.asyncio
async def test_llm_check_pii_detected_and_unsafe_replaced() -> None:
    """llm_check=True + pii_detected=True + safe=False → refusal (pii_detected flag set)."""
    pii_answer = "Pacjent Jan Kowalski, PESEL 80010112345, zgłosił się na wizytę."
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(
        return_value=_make_llm_response_v2(safe=False, pii_detected=True)
    )

    state = _make_state(
        answer=pii_answer,
        halt=False,
        no_results=False,
        guardrails_config={"llm_check": True},
    )
    result = await node_guardrails_output(state, _make_config_with_llm(llm, db))

    assert result["answer"] == _LLM_REFUSAL_ANSWER


@pytest.mark.asyncio
async def test_llm_check_timeout_fail_open() -> None:
    """llm_check=True + LLM call times out → fail-open: original answer kept."""
    answer_text = "Dokumenty do wizyty."
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(side_effect=TimeoutError())

    state = _make_state(
        answer=answer_text,
        halt=False,
        no_results=False,
        guardrails_config={"llm_check": True},
    )
    result = await node_guardrails_output(state, _make_config_with_llm(llm, db))

    assert result["answer"] == answer_text


@pytest.mark.asyncio
async def test_llm_check_passes_timeout_30s_to_llm() -> None:
    """llm_check=True must call llm.chat_completion with timeout=30.0 (ADR-017 §4)."""
    from src.graphs.query_graph.nodes.node_guardrails_output import _GUARDRAILS_TIMEOUT_S

    answer_text = "Informacja o procedurze."
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response_v2(safe=True))

    state = _make_state(
        answer=answer_text,
        halt=False,
        no_results=False,
        guardrails_config={"llm_check": True},
    )
    await node_guardrails_output(state, _make_config_with_llm(llm, db))

    call_kwargs = llm.chat_completion.call_args.kwargs
    assert call_kwargs.get("timeout") == _GUARDRAILS_TIMEOUT_S


@pytest.mark.asyncio
async def test_llm_check_uses_v2_prompt_with_context() -> None:
    """llm_check=True passes graded_chunks highlight_text as context to LLM."""
    from unittest.mock import patch

    answer_text = "Procedura operacyjna wymaga zgody."
    chunk_text = "Zgodnie z protokołem operacyjnym wymagana jest pisemna zgoda pacjenta."
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response_v2(safe=True))

    state = _make_state(
        answer=answer_text,
        halt=False,
        no_results=False,
        graded_chunks=[{"point_id": "abc", "highlight_text": chunk_text, "score": 0.9}],
        guardrails_config={"llm_check": True},
    )
    with patch(
        "src.graphs.query_graph.nodes.node_guardrails_output._load_guardrails_prompt_v2",
        return_value="{{ANSWER}} | {{CONTEXT}}",
    ):
        await node_guardrails_output(state, _make_config_with_llm(llm, db))

    call_kwargs = llm.chat_completion.call_args.kwargs
    prompt_sent = call_kwargs["messages"][0]["content"]
    assert chunk_text in prompt_sent
    assert answer_text in prompt_sent


@pytest.mark.asyncio
async def test_reasoning_field_not_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    """reasoning field from LLM decision must NEVER appear in application logs (GDPR)."""
    import logging

    answer_text = "Informacja o dostępności lekarzy."
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    secret_reasoning = "SECRET_REASONING_CONTENT_MUST_NOT_LEAK"
    llm.chat_completion = AsyncMock(
        return_value=_make_llm_response_v2(safe=True, reasoning=secret_reasoning)
    )

    state = _make_state(
        answer=answer_text,
        halt=False,
        no_results=False,
        guardrails_config={"llm_check": True},
    )
    with caplog.at_level(logging.DEBUG):
        await node_guardrails_output(state, _make_config_with_llm(llm, db))

    for record in caplog.records:
        assert secret_reasoning not in record.getMessage(), (
            "reasoning field must never appear in logs (GDPR)"
        )


# ---------------------------------------------------------------------------
# GuardrailsConfig schema — ADR-017 fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_check_modification_preserves_disclaimer_from_rule_based_pass() -> None:
    """MAJOR-1: When add_disclaimer=True (rule-based) AND llm_check=True returns a
    modification, the disclaimer must still appear in the final answer."""
    original_answer = "Gabinety czynne od 7."
    improved_answer = "Gabinety zabiegowe czynne od 7:00."
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(
        return_value=_make_llm_response_v2(safe=True, modifications=[improved_answer])
    )

    state = _make_state(
        answer=original_answer,
        halt=False,
        no_results=False,
        guardrails_config={"add_disclaimer": True, "llm_check": True},
    )
    result = await node_guardrails_output(state, _make_config_with_llm(llm, db))

    # Modification applied AND disclaimer not lost
    assert result["answer"].startswith(improved_answer)
    assert len(result["answer"]) > len(improved_answer)  # disclaimer appended


@pytest.mark.asyncio
async def test_llm_check_oversized_modification_discarded() -> None:
    """MAJOR-2: modification longer than 3× the original answer is discarded (injection guard)."""
    original_answer = "Short."
    oversized = "X" * (3 * len(original_answer) + 100)  # clearly exceeds 3× limit
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(
        return_value=_make_llm_response_v2(safe=True, modifications=[oversized])
    )

    state = _make_state(
        answer=original_answer,
        halt=False,
        no_results=False,
        guardrails_config={"llm_check": True},
    )
    result = await node_guardrails_output(state, _make_config_with_llm(llm, db))

    # Oversized modification must be discarded — original answer kept
    assert result["answer"] == original_answer
    assert oversized not in result["answer"]


def test_guardrails_config_llm_check_defaults_false() -> None:
    """GuardrailsConfig.llm_check defaults to False (ADR-017)."""
    from src.domain.schemas.pipeline import GuardrailsConfig

    config = GuardrailsConfig()
    assert config.llm_check is False


def test_guardrails_config_llm_check_can_be_enabled() -> None:
    """GuardrailsConfig.llm_check=True is valid."""
    from src.domain.schemas.pipeline import GuardrailsConfig

    config = GuardrailsConfig(llm_check=True)
    assert config.llm_check is True


def test_guardrails_config_add_disclaimer_defaults_false() -> None:
    """GuardrailsConfig.add_disclaimer defaults to False."""
    from src.domain.schemas.pipeline import GuardrailsConfig

    config = GuardrailsConfig()
    assert config.add_disclaimer is False


# ---------------------------------------------------------------------------
# LLMClient timeout parameter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_client_accepts_timeout_parameter() -> None:
    """LLMClient.chat_completion accepts and forwards the timeout parameter."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from src.core.clients.llm_client import LLMClient

    client = LLMClient()
    mock_create = AsyncMock(return_value=MagicMock())

    with patch.object(client, "_client") as mock_client_factory:
        mock_openai = MagicMock()
        mock_openai.chat.completions.create = mock_create
        mock_client_factory.return_value = mock_openai

        await client.chat_completion(
            model="test-model",
            messages=[{"role": "user", "content": "hi"}],
            timeout=30.0,
        )

    call_kwargs = mock_create.call_args.kwargs
    assert call_kwargs.get("timeout") == 30.0

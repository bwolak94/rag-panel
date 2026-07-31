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


def _make_llm_response(safe: bool, reason: str | None = None) -> MagicMock:
    payload: dict[str, object] = {"safe": safe}
    if reason is not None:
        payload["reason"] = reason
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
        return_value=_make_llm_response(safe=False, reason="unsafe_medical_advice")
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
        return_value=_make_llm_response(safe=False, reason="unsafe_medical_advice")
    )

    state = _make_state(
        answer=unsafe_answer,
        halt=False,
        no_results=False,
        guardrails_config={"add_disclaimer": True, "llm_guardrails_enabled": True},
    )
    result = await node_guardrails_output(state, _make_config_with_llm(llm, db))

    assert result["answer"] == _LLM_REFUSAL_ANSWER

"""Unit tests for node_guardrails_output.

Tests cover:
- chitchat halt → returns out-of-scope Polish message
- no_results=True → returns "not found" Polish message
- add_disclaimer=True → disclaimer appended to answer
- Normal answer → pass through unchanged
"""

from __future__ import annotations

import uuid

import pytest

from src.graphs.query_graph.nodes.node_guardrails_output import (
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

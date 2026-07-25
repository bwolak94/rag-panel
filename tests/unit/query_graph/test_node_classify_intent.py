"""Unit tests for node_classify_intent.

Tests cover:
- topical question → intent="topical", halt=False
- chitchat → intent="chitchat", halt=True
- out_of_scope → intent="out_of_scope", halt=True
- LLM returns invalid JSON → defaults to "topical"
- LLM returns unexpected intent value → defaults to "topical"
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.graphs.query_graph.nodes.node_classify_intent import node_classify_intent
from src.graphs.query_graph.state import QueryState

TENANT_ID = uuid.uuid4()
LLM_MODEL_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()
PIPELINE_ID = uuid.uuid4()


def _make_state(question: str = "Jakie są procedury przyjęcia pacjenta?") -> QueryState:
    return QueryState(
        question=question,
        conversation_id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        allowed_collection_ids=[COLLECTION_ID],
        pipeline_id=PIPELINE_ID,
        llm_model_id=LLM_MODEL_ID,
        collection_ids=[COLLECTION_ID],
    )


def _make_model_record() -> MagicMock:
    record = MagicMock()
    record.id = LLM_MODEL_ID
    record.model_id = "llama3.2"
    record.endpoint_url = "http://ollama:11434/v1"
    return record


def _make_llm_response(intent: str) -> MagicMock:
    content = json.dumps({"intent": intent})
    choice = MagicMock()
    choice.message.content = content
    response = MagicMock()
    response.choices = [choice]
    return response


def _make_config(llm: object, db: object) -> dict:
    return {"configurable": {"llm": llm, "db": db}}


def _make_db(model_record: object) -> AsyncMock:
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = model_record
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.mark.asyncio
async def test_topical_question_returns_topical() -> None:
    """A medical question is classified as topical, halt=False."""
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response("topical"))

    state = _make_state("Jakie są procedury przyjęcia pacjenta?")
    result = await node_classify_intent(state, _make_config(llm, db))

    assert result["intent"] == "topical"
    assert result["halt"] is False


@pytest.mark.asyncio
async def test_chitchat_returns_halt_true() -> None:
    """A greeting is classified as chitchat, halt=True."""
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response("chitchat"))

    state = _make_state("Dzień dobry!")
    result = await node_classify_intent(state, _make_config(llm, db))

    assert result["intent"] == "chitchat"
    assert result["halt"] is True


@pytest.mark.asyncio
async def test_out_of_scope_returns_halt_true() -> None:
    """An off-topic question is classified as out_of_scope, halt=True."""
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response("out_of_scope"))

    state = _make_state("Jaki jest aktualny kurs dolara?")
    result = await node_classify_intent(state, _make_config(llm, db))

    assert result["intent"] == "out_of_scope"
    assert result["halt"] is True


@pytest.mark.asyncio
async def test_invalid_json_defaults_to_topical() -> None:
    """If LLM returns invalid JSON, intent defaults to topical (conservative)."""
    model_record = _make_model_record()
    db = _make_db(model_record)

    choice = MagicMock()
    choice.message.content = "This is not JSON at all"
    response = MagicMock()
    response.choices = [choice]

    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=response)

    state = _make_state("Jakie dokumenty są potrzebne?")
    result = await node_classify_intent(state, _make_config(llm, db))

    assert result["intent"] == "topical"
    assert result["halt"] is False


@pytest.mark.asyncio
async def test_unexpected_intent_value_defaults_to_topical() -> None:
    """If LLM returns unknown intent string, defaults to topical."""
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response("unknown_category"))

    state = _make_state("Jakie są godziny otwarcia?")
    result = await node_classify_intent(state, _make_config(llm, db))

    assert result["intent"] == "topical"
    assert result["halt"] is False


@pytest.mark.asyncio
async def test_model_not_found_raises_query_node_error() -> None:
    """If DB returns None for model, QueryNodeError is raised."""
    from src.core.exceptions import QueryNodeError

    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result_mock)

    llm = AsyncMock()
    state = _make_state()
    with pytest.raises(QueryNodeError, match="LLM model not found"):
        await node_classify_intent(state, _make_config(llm, db))

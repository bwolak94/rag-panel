"""Unit tests for node_translate_query.

Tests cover:
- Successful translation returns translated_query and cross_language_retrieval=True
- Empty LLM response falls back to original query
- LLM error raises QueryNodeError
- Missing collection_ids raises QueryNodeError
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.exceptions import QueryNodeError
from src.graphs.query_graph.nodes.node_translate_query import node_translate_query
from src.graphs.query_graph.state import QueryState

TENANT_ID = uuid.uuid4()
PIPELINE_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()
LLM_MODEL_ID = uuid.uuid4()
EMBEDDING_MODEL_ID = uuid.uuid4()


def _make_state(
    question: str = "What are the patient requirements?",
    detected_language: str = "eng",
    collection_ids: list[uuid.UUID] | None = None,
) -> QueryState:
    return QueryState(
        question=question,
        conversation_id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        allowed_collection_ids=[COLLECTION_ID],
        pipeline_id=PIPELINE_ID,
        llm_model_id=LLM_MODEL_ID,
        collection_ids=collection_ids if collection_ids is not None else [COLLECTION_ID],
        rewritten_query=question,
        detected_language=detected_language,
    )


def _make_db(collection_lang: str = "pol") -> MagicMock:
    col = MagicMock()
    col.primary_language = collection_lang

    model_record = MagicMock()
    model_record.model_id = "llama3"
    model_record.endpoint_url = "http://ollama:11434/v1"

    db = MagicMock()

    async def _execute(stmt: Any) -> MagicMock:
        result = MagicMock()
        # Decide what to return based on call count
        if not hasattr(_execute, "_call_count"):
            _execute._call_count = 0  # type: ignore[attr-defined]
        _execute._call_count += 1  # type: ignore[attr-defined]
        if _execute._call_count == 1:  # type: ignore[attr-defined]
            result.scalar_one_or_none = MagicMock(return_value=col)
        else:
            result.scalar_one_or_none = MagicMock(return_value=model_record)
        return result

    db.execute = _execute
    return db, model_record


def _make_llm_response(content: str) -> MagicMock:
    choice = MagicMock()
    choice.message.content = content
    resp = MagicMock()
    resp.choices = [choice]
    return resp


def _make_config(db: MagicMock, llm: MagicMock) -> dict[str, Any]:
    return {"configurable": {"db": db, "llm": llm}}


@pytest.mark.asyncio
async def test_successful_translation() -> None:
    """LLM returns translation → translated_query set, cross_language_retrieval=True."""
    state = _make_state()
    db, model_record = _make_db()

    llm = MagicMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response("Jakie są wymagania pacjenta?"))

    result = await node_translate_query(state, _make_config(db, llm))

    assert result["translated_query"] == "Jakie są wymagania pacjenta?"
    assert result["cross_language_retrieval"] is True


@pytest.mark.asyncio
async def test_empty_llm_response_falls_back_to_original() -> None:
    """LLM returns empty string → fallback to original rewritten_query."""
    state = _make_state()
    db, model_record = _make_db()

    llm = MagicMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(""))

    result = await node_translate_query(state, _make_config(db, llm))

    assert result["translated_query"] == state.rewritten_query
    assert result["cross_language_retrieval"] is True


@pytest.mark.asyncio
async def test_llm_error_raises_query_node_error() -> None:
    """LLM raises exception → QueryNodeError."""
    state = _make_state()
    db, model_record = _make_db()

    llm = MagicMock()
    llm.chat_completion = AsyncMock(side_effect=RuntimeError("connection refused"))

    with pytest.raises(QueryNodeError, match="query_translation_error"):
        await node_translate_query(state, _make_config(db, llm))


@pytest.mark.asyncio
async def test_no_collection_ids_raises_query_node_error() -> None:
    """Empty collection_ids → QueryNodeError immediately."""
    state = _make_state(collection_ids=[])
    db = MagicMock()
    llm = MagicMock()

    with pytest.raises(QueryNodeError, match="No collection_ids"):
        await node_translate_query(state, _make_config(db, llm))

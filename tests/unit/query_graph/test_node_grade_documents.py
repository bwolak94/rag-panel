"""Unit tests for node_grade_documents.

Tests cover:
- All chunks marked relevant → graded_chunks contains all, no_results=False
- Some chunks irrelevant → only relevant ones returned, no_results=False
- All chunks marked irrelevant → graded_chunks=[], no_results=True
- No chunks in state (retrieved_chunks=[]) → graded_chunks=[], no_results=True, LLM NOT called
- LLM returns invalid JSON → falls back: all chunks kept, no_results=False
- LLM raises QueryNodeError → re-raised
- Model not found in DB → QueryNodeError raised
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.exceptions import QueryNodeError
from src.graphs.query_graph.nodes.node_grade_documents import node_grade_documents
from src.graphs.query_graph.state import QueryState

TENANT_ID = uuid.uuid4()
LLM_MODEL_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()
PIPELINE_ID = uuid.uuid4()


def _make_chunk(
    point_id: str | None = None,
    highlight_text: str = "Some chunk content",
    score: float = 0.9,
) -> dict:
    return {
        "point_id": point_id or str(uuid.uuid4()),
        "document_id": str(uuid.uuid4()),
        "score": score,
        "highlight_text": highlight_text,
        "page_number": 1,
        "collection_id": str(COLLECTION_ID),
        "payload": {},
    }


def _make_state(retrieved_chunks: list[dict] | None = None) -> QueryState:
    return QueryState(
        question="Jakie są procedury przyjęcia pacjenta?",
        conversation_id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        allowed_collection_ids=[COLLECTION_ID],
        pipeline_id=PIPELINE_ID,
        llm_model_id=LLM_MODEL_ID,
        collection_ids=[COLLECTION_ID],
        retrieved_chunks=retrieved_chunks if retrieved_chunks is not None else [],
    )


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


def _make_config(llm: object, db: object) -> dict:
    return {"configurable": {"llm": llm, "db": db}}


def _make_llm_response(grades: list[dict]) -> MagicMock:
    content = json.dumps({"grades": grades})
    choice = MagicMock()
    choice.message.content = content
    response = MagicMock()
    response.choices = [choice]
    return response


@pytest.mark.asyncio
async def test_all_chunks_relevant_returns_all() -> None:
    """When every chunk is graded relevant, all are returned and no_results=False."""
    chunks = [
        _make_chunk(highlight_text="Procedura rejestracji pacjenta"),
        _make_chunk(highlight_text="Dokumenty wymagane przy przyjęciu"),
        _make_chunk(highlight_text="Formularz zgody na leczenie"),
    ]
    grades = [
        {"chunk_index": 1, "relevant": True},
        {"chunk_index": 2, "relevant": True},
        {"chunk_index": 3, "relevant": True},
    ]

    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(grades))

    state = _make_state(retrieved_chunks=chunks)
    result = await node_grade_documents(state, _make_config(llm, db))

    assert result["no_results"] is False
    assert len(result["graded_chunks"]) == 3
    assert result["graded_chunks"] == chunks


@pytest.mark.asyncio
async def test_some_chunks_irrelevant_returns_only_relevant() -> None:
    """When only some chunks pass grading, only relevant ones are returned."""
    chunk_a = _make_chunk(highlight_text="Procedura rejestracji pacjenta")
    chunk_b = _make_chunk(highlight_text="Przepis na zupę pomidorową")
    chunk_c = _make_chunk(highlight_text="Formularz zgody na leczenie")
    chunks = [chunk_a, chunk_b, chunk_c]

    grades = [
        {"chunk_index": 1, "relevant": True},
        {"chunk_index": 2, "relevant": False},
        {"chunk_index": 3, "relevant": True},
    ]

    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(grades))

    state = _make_state(retrieved_chunks=chunks)
    result = await node_grade_documents(state, _make_config(llm, db))

    assert result["no_results"] is False
    assert len(result["graded_chunks"]) == 2
    assert chunk_a in result["graded_chunks"]
    assert chunk_b not in result["graded_chunks"]
    assert chunk_c in result["graded_chunks"]


@pytest.mark.asyncio
async def test_all_chunks_irrelevant_returns_empty_and_no_results_true() -> None:
    """When all chunks are graded irrelevant, graded_chunks=[] and no_results=True."""
    chunks = [
        _make_chunk(highlight_text="Informacja niezwiązana z pytaniem"),
        _make_chunk(highlight_text="Inny nieistotny fragment"),
    ]
    grades = [
        {"chunk_index": 1, "relevant": False},
        {"chunk_index": 2, "relevant": False},
    ]

    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(grades))

    state = _make_state(retrieved_chunks=chunks)
    result = await node_grade_documents(state, _make_config(llm, db))

    assert result["no_results"] is True
    assert result["graded_chunks"] == []


@pytest.mark.asyncio
async def test_empty_retrieved_chunks_skips_llm_call() -> None:
    """When retrieved_chunks is empty, LLM is never called and no_results=True immediately."""
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock()

    state = _make_state(retrieved_chunks=[])
    result = await node_grade_documents(state, _make_config(llm, db))

    assert result["graded_chunks"] == []
    assert result["no_results"] is True
    llm.chat_completion.assert_not_called()
    # DB should also not be queried when there are no chunks
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_json_response_keeps_all_chunks() -> None:
    """When LLM returns invalid JSON, fallback keeps all chunks and no_results=False."""
    chunks = [
        _make_chunk(highlight_text="Fragment A"),
        _make_chunk(highlight_text="Fragment B"),
    ]

    invalid_choice = MagicMock()
    invalid_choice.message.content = "To nie jest poprawny JSON {{{!!!"
    invalid_response = MagicMock()
    invalid_response.choices = [invalid_choice]

    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=invalid_response)

    state = _make_state(retrieved_chunks=chunks)
    result = await node_grade_documents(state, _make_config(llm, db))

    assert result["no_results"] is False
    assert len(result["graded_chunks"]) == 2
    assert result["graded_chunks"] == chunks


@pytest.mark.asyncio
async def test_missing_grades_key_in_json_keeps_all_chunks() -> None:
    """When LLM returns valid JSON but without 'grades' key → empty grades, no_results=True.

    Note: the node uses parsed.get('grades', []) so missing key yields empty list,
    which means all chunks are filtered out → no_results=True. This tests the actual
    behaviour, not the fallback path (fallback only triggers on parse errors).
    """
    chunks = [
        _make_chunk(highlight_text="Fragment A"),
    ]

    # Valid JSON but 'grades' is absent — node gets empty list, all filtered → no_results
    choice = MagicMock()
    choice.message.content = json.dumps({"result": "ok"})
    response = MagicMock()
    response.choices = [choice]

    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=response)

    state = _make_state(retrieved_chunks=chunks)
    result = await node_grade_documents(state, _make_config(llm, db))

    assert result["no_results"] is True
    assert result["graded_chunks"] == []


@pytest.mark.asyncio
async def test_llm_raises_query_node_error_is_re_raised() -> None:
    """QueryNodeError raised by LLM propagates unchanged without wrapping."""
    chunks = [_make_chunk()]

    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(side_effect=QueryNodeError("upstream LLM error"))

    state = _make_state(retrieved_chunks=chunks)
    with pytest.raises(QueryNodeError, match="upstream LLM error"):
        await node_grade_documents(state, _make_config(llm, db))


@pytest.mark.asyncio
async def test_llm_raises_generic_exception_wrapped_in_query_node_error() -> None:
    """A generic exception from the LLM is wrapped in QueryNodeError."""
    chunks = [_make_chunk()]

    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(side_effect=RuntimeError("connection timeout"))

    state = _make_state(retrieved_chunks=chunks)
    with pytest.raises(QueryNodeError, match="grade_documents_llm_error"):
        await node_grade_documents(state, _make_config(llm, db))


@pytest.mark.asyncio
async def test_model_not_found_raises_query_node_error() -> None:
    """If DB returns None for the model record, QueryNodeError is raised before LLM call."""
    chunks = [_make_chunk()]

    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result_mock)

    llm = AsyncMock()
    llm.chat_completion = AsyncMock()

    state = _make_state(retrieved_chunks=chunks)
    with pytest.raises(QueryNodeError, match="LLM model not found"):
        await node_grade_documents(state, _make_config(llm, db))

    llm.chat_completion.assert_not_called()


@pytest.mark.asyncio
async def test_grading_uses_one_based_chunk_indices() -> None:
    """Verify that chunk_index is 1-based: index 1 → first chunk, index 2 → second chunk."""
    chunk_first = _make_chunk(highlight_text="First chunk")
    chunk_second = _make_chunk(highlight_text="Second chunk")
    chunks = [chunk_first, chunk_second]

    # Only chunk at index 2 (second) is marked relevant
    grades = [
        {"chunk_index": 1, "relevant": False},
        {"chunk_index": 2, "relevant": True},
    ]

    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(grades))

    state = _make_state(retrieved_chunks=chunks)
    result = await node_grade_documents(state, _make_config(llm, db))

    assert result["no_results"] is False
    assert len(result["graded_chunks"]) == 1
    assert result["graded_chunks"][0]["highlight_text"] == "Second chunk"


@pytest.mark.asyncio
async def test_grades_with_non_integer_chunk_index_are_ignored() -> None:
    """Grade entries with non-integer chunk_index are silently skipped."""
    chunks = [_make_chunk(highlight_text="Fragment A")]

    grades = [
        {"chunk_index": "1", "relevant": True},  # string index — should be ignored
    ]

    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(grades))

    state = _make_state(retrieved_chunks=chunks)
    result = await node_grade_documents(state, _make_config(llm, db))

    # String index is ignored → no valid relevant indices → all filtered out
    assert result["graded_chunks"] == []
    assert result["no_results"] is True


@pytest.mark.asyncio
async def test_llm_receives_correct_model_id_and_endpoint() -> None:
    """LLM chat_completion is called with model_id and endpoint_url from the DB record."""
    chunks = [_make_chunk()]
    grades = [{"chunk_index": 1, "relevant": True}]

    model_record = _make_model_record()
    model_record.model_id = "mistral-7b"
    model_record.endpoint_url = "http://vllm:8000/v1"

    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(grades))

    state = _make_state(retrieved_chunks=chunks)
    await node_grade_documents(state, _make_config(llm, db))

    llm.chat_completion.assert_called_once()
    call_kwargs = llm.chat_completion.call_args.kwargs
    assert call_kwargs["model"] == "mistral-7b"
    assert call_kwargs["base_url"] == "http://vllm:8000/v1"
    assert call_kwargs["temperature"] == 0.0
    assert call_kwargs["response_format"] == {"type": "json_object"}

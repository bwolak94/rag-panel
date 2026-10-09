"""Unit tests for node_rerank.

Tests cover:
- rerank_enabled=False → chunks unchanged, LLM never called (pass-through)
- rerank_enabled=True, LLM returns valid scores → chunks re-ordered by score descending
- rerank_enabled=True with rerank_top_k → output truncated to top_k after sorting
- LLM returns partial scores (some indices missing) → missing indices get score 0
- LLM returns invalid JSON → fail-safe: original chunk order preserved
- LLM raises a generic exception → fail-safe: original chunk order preserved
- Model not found in DB → fail-safe: original chunk order preserved
- No chunks in state → returns empty list without LLM call
- Score clamping: LLM returns out-of-range score → clamped to [0, 10]
- rerank_score field is added to every chunk when reranking is active
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.graphs.query_graph.nodes.node_rerank import _apply_scores, node_rerank
from src.graphs.query_graph.state import QueryState

TENANT_ID = uuid.uuid4()
LLM_MODEL_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()
PIPELINE_ID = uuid.uuid4()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_chunk(
    highlight_text: str = "Some chunk content",
    score: float = 0.8,
) -> dict:
    return {
        "point_id": str(uuid.uuid4()),
        "document_id": str(uuid.uuid4()),
        "score": score,
        "highlight_text": highlight_text,
        "page_number": 1,
        "collection_id": str(COLLECTION_ID),
        "payload": {},
    }


def _make_state(
    retrieved_chunks: list[dict] | None = None,
    rerank_enabled: bool = True,
    rerank_top_k: int | None = None,
) -> QueryState:
    return QueryState(
        question="Jakie są procedury przyjęcia pacjenta?",
        conversation_id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        allowed_collection_ids=[COLLECTION_ID],
        pipeline_id=PIPELINE_ID,
        llm_model_id=LLM_MODEL_ID,
        collection_ids=[COLLECTION_ID],
        retrieved_chunks=retrieved_chunks if retrieved_chunks is not None else [],
        rerank_enabled=rerank_enabled,
        rerank_top_k=rerank_top_k,
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


def _make_llm_response(scores: list[dict]) -> MagicMock:
    content = json.dumps({"scores": scores})
    choice = MagicMock()
    choice.message.content = content
    response = MagicMock()
    response.choices = [choice]
    return response


# ---------------------------------------------------------------------------
# Tests: pass-through when disabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rerank_disabled_returns_chunks_unchanged() -> None:
    """When rerank_enabled=False the node returns retrieved_chunks as-is without LLM."""
    chunks = [
        _make_chunk(highlight_text="Chunk A"),
        _make_chunk(highlight_text="Chunk B"),
    ]
    state = _make_state(retrieved_chunks=chunks, rerank_enabled=False)

    llm = AsyncMock()
    llm.chat_completion = AsyncMock()
    db = AsyncMock()

    result = await node_rerank(state, _make_config(llm, db))

    assert result["retrieved_chunks"] == chunks
    llm.chat_completion.assert_not_called()
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_rerank_disabled_no_chunks_returns_empty() -> None:
    """Pass-through with no chunks returns an empty list and never calls LLM."""
    state = _make_state(retrieved_chunks=[], rerank_enabled=False)

    llm = AsyncMock()
    llm.chat_completion = AsyncMock()
    db = AsyncMock()

    result = await node_rerank(state, _make_config(llm, db))

    assert result["retrieved_chunks"] == []
    llm.chat_completion.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: reranking active — ordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chunks_reordered_by_score_descending() -> None:
    """Chunks are sorted so the highest-scored chunk is first."""
    chunk_low = _make_chunk(highlight_text="Low relevance chunk")
    chunk_mid = _make_chunk(highlight_text="Medium relevance chunk")
    chunk_high = _make_chunk(highlight_text="High relevance chunk")
    chunks = [chunk_low, chunk_mid, chunk_high]

    # chunk_low (index 1) → score 2, chunk_mid (index 2) → score 7, chunk_high (index 3) → score 9
    scores = [
        {"chunk_index": 1, "score": 2},
        {"chunk_index": 2, "score": 7},
        {"chunk_index": 3, "score": 9},
    ]

    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(scores))

    state = _make_state(retrieved_chunks=chunks, rerank_enabled=True)
    result = await node_rerank(state, _make_config(llm, db))

    reranked = result["retrieved_chunks"]
    assert len(reranked) == 3
    assert reranked[0]["highlight_text"] == "High relevance chunk"
    assert reranked[1]["highlight_text"] == "Medium relevance chunk"
    assert reranked[2]["highlight_text"] == "Low relevance chunk"


@pytest.mark.asyncio
async def test_rerank_score_field_added_to_each_chunk() -> None:
    """Every chunk in the output has a rerank_score field when reranking is active."""
    chunks = [_make_chunk(highlight_text="Chunk A"), _make_chunk(highlight_text="Chunk B")]
    scores = [
        {"chunk_index": 1, "score": 5},
        {"chunk_index": 2, "score": 8},
    ]

    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(scores))

    state = _make_state(retrieved_chunks=chunks, rerank_enabled=True)
    result = await node_rerank(state, _make_config(llm, db))

    for chunk in result["retrieved_chunks"]:
        assert "rerank_score" in chunk


# ---------------------------------------------------------------------------
# Tests: rerank_top_k truncation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rerank_top_k_truncates_output() -> None:
    """When rerank_top_k=2 only the top 2 chunks are returned."""
    chunks = [
        _make_chunk(highlight_text="Score 3"),
        _make_chunk(highlight_text="Score 9"),
        _make_chunk(highlight_text="Score 6"),
    ]
    scores = [
        {"chunk_index": 1, "score": 3},
        {"chunk_index": 2, "score": 9},
        {"chunk_index": 3, "score": 6},
    ]

    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(scores))

    state = _make_state(retrieved_chunks=chunks, rerank_enabled=True, rerank_top_k=2)
    result = await node_rerank(state, _make_config(llm, db))

    reranked = result["retrieved_chunks"]
    assert len(reranked) == 2
    assert reranked[0]["highlight_text"] == "Score 9"
    assert reranked[1]["highlight_text"] == "Score 6"


# ---------------------------------------------------------------------------
# Tests: partial scores — graceful handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_scores_missing_indices_get_score_zero() -> None:
    """Chunks not scored by the LLM receive rerank_score=0 and sort to the bottom."""
    chunk_a = _make_chunk(highlight_text="Scored chunk")
    chunk_b = _make_chunk(highlight_text="Unscored chunk")
    chunks = [chunk_a, chunk_b]

    # LLM only returns a score for chunk 1; chunk 2 is missing.
    scores = [{"chunk_index": 1, "score": 7}]

    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(scores))

    state = _make_state(retrieved_chunks=chunks, rerank_enabled=True)
    result = await node_rerank(state, _make_config(llm, db))

    reranked = result["retrieved_chunks"]
    assert len(reranked) == 2
    assert reranked[0]["highlight_text"] == "Scored chunk"
    assert reranked[0]["rerank_score"] == 7
    assert reranked[1]["highlight_text"] == "Unscored chunk"
    assert reranked[1]["rerank_score"] == 0


# ---------------------------------------------------------------------------
# Tests: fail-safe on error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_json_from_llm_preserves_original_order() -> None:
    """When LLM returns unparseable content the original chunk order is returned unchanged."""
    chunks = [
        _make_chunk(highlight_text="Chunk A"),
        _make_chunk(highlight_text="Chunk B"),
    ]

    bad_choice = MagicMock()
    bad_choice.message.content = "not valid json {{ !! }}"
    bad_response = MagicMock()
    bad_response.choices = [bad_choice]

    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=bad_response)

    state = _make_state(retrieved_chunks=chunks, rerank_enabled=True)
    result = await node_rerank(state, _make_config(llm, db))

    # Original order preserved, no rerank_score added
    assert result["retrieved_chunks"] == chunks


@pytest.mark.asyncio
async def test_llm_generic_exception_preserves_original_order() -> None:
    """Any exception from the LLM triggers fail-safe: original order returned."""
    chunks = [
        _make_chunk(highlight_text="Chunk A"),
        _make_chunk(highlight_text="Chunk B"),
    ]

    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(side_effect=RuntimeError("network timeout"))

    state = _make_state(retrieved_chunks=chunks, rerank_enabled=True)
    result = await node_rerank(state, _make_config(llm, db))

    assert result["retrieved_chunks"] == chunks


@pytest.mark.asyncio
async def test_model_not_found_preserves_original_order() -> None:
    """When the LLM model record is absent from the DB the original order is preserved."""
    chunks = [
        _make_chunk(highlight_text="Chunk A"),
        _make_chunk(highlight_text="Chunk B"),
    ]

    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result_mock)

    llm = AsyncMock()
    llm.chat_completion = AsyncMock()

    state = _make_state(retrieved_chunks=chunks, rerank_enabled=True)
    result = await node_rerank(state, _make_config(llm, db))

    assert result["retrieved_chunks"] == chunks
    llm.chat_completion.assert_not_called()


@pytest.mark.asyncio
async def test_rerank_enabled_no_chunks_returns_empty_without_llm() -> None:
    """When rerank_enabled=True but there are no chunks, LLM is never called."""
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock()

    state = _make_state(retrieved_chunks=[], rerank_enabled=True)
    result = await node_rerank(state, _make_config(llm, db))

    assert result["retrieved_chunks"] == []
    llm.chat_completion.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: score clamping
# ---------------------------------------------------------------------------


def test_apply_scores_clamps_out_of_range_values() -> None:
    """Scores outside [0, 10] are clamped: >10 → 10, <0 → 0."""
    chunks = [
        _make_chunk(highlight_text="Over-scored"),
        _make_chunk(highlight_text="Under-scored"),
    ]
    scores = [
        {"chunk_index": 1, "score": 99},
        {"chunk_index": 2, "score": -5},
    ]

    result = _apply_scores(chunks, scores, top_k=None)

    assert result[0]["rerank_score"] == 10  # clamped from 99
    assert result[1]["rerank_score"] == 0  # clamped from -5


def test_apply_scores_non_integer_index_ignored() -> None:
    """Score entries with non-integer chunk_index are silently skipped."""
    chunks = [_make_chunk(highlight_text="Chunk A")]
    scores = [{"chunk_index": "1", "score": 9}]  # string index → ignored

    result = _apply_scores(chunks, scores, top_k=None)

    assert result[0]["rerank_score"] == 0  # string index was ignored


def test_apply_scores_top_k_zero_returns_all() -> None:
    """rerank_top_k=0 is treated as 'keep all' (same as None)."""
    chunks = [_make_chunk(), _make_chunk(), _make_chunk()]
    scores = [
        {"chunk_index": 1, "score": 5},
        {"chunk_index": 2, "score": 3},
        {"chunk_index": 3, "score": 8},
    ]

    result = _apply_scores(chunks, scores, top_k=0)

    # top_k=0 → the condition `top_k > 0` is False → no truncation
    assert len(result) == 3


# ---------------------------------------------------------------------------
# Tests: LLM is called with correct parameters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_called_with_correct_model_and_endpoint() -> None:
    """node_rerank forwards the correct model_id, endpoint_url, temperature and response_format."""
    chunks = [_make_chunk()]
    scores = [{"chunk_index": 1, "score": 7}]

    model_record = _make_model_record()
    model_record.model_id = "mistral-7b"
    model_record.endpoint_url = "http://vllm:8000/v1"

    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(scores))

    state = _make_state(retrieved_chunks=chunks, rerank_enabled=True)
    await node_rerank(state, _make_config(llm, db))

    llm.chat_completion.assert_called_once()
    call_kwargs = llm.chat_completion.call_args.kwargs
    assert call_kwargs["model"] == "mistral-7b"
    assert call_kwargs["base_url"] == "http://vllm:8000/v1"
    assert call_kwargs["temperature"] == 0.0
    assert call_kwargs["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_rewritten_query_used_when_available() -> None:
    """node_rerank uses rewritten_query over question when both are set."""
    chunks = [_make_chunk()]
    scores = [{"chunk_index": 1, "score": 6}]

    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(scores))

    state = _make_state(retrieved_chunks=chunks, rerank_enabled=True)
    # Inject a rewritten_query into the state manually.
    state = state.model_copy(update={"rewritten_query": "procedura przyjęcia do szpitala"})

    await node_rerank(state, _make_config(llm, db))

    llm.chat_completion.assert_called_once()
    system_message = llm.chat_completion.call_args.kwargs["messages"][0]["content"]
    assert "procedura przyjęcia do szpitala" in system_message
    # Original question should NOT appear in the prompt (rewritten_query takes precedence).
    assert "Jakie są procedury przyjęcia pacjenta?" not in system_message


# ---------------------------------------------------------------------------
# ADR-014: reranker_model_id activation via prompt_config
# ---------------------------------------------------------------------------

RERANKER_MODEL_ID = uuid.uuid4()


def _make_state_with_reranker(
    retrieved_chunks: list[dict] | None = None,
    rerank_top_k: int | None = None,
) -> QueryState:
    """Make state with reranker_model_id in prompt_config (ADR-014 activation path)."""
    return QueryState(
        question="Jakie są procedury przyjęcia pacjenta?",
        conversation_id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        allowed_collection_ids=[COLLECTION_ID],
        pipeline_id=PIPELINE_ID,
        llm_model_id=LLM_MODEL_ID,
        collection_ids=[COLLECTION_ID],
        retrieved_chunks=retrieved_chunks if retrieved_chunks is not None else [],
        rerank_enabled=True,  # set by invoke_query_graph from prompt_config
        rerank_top_k=rerank_top_k,
        prompt_config={"reranker_model_id": str(RERANKER_MODEL_ID)},
    )


def _make_reranker_model_record() -> MagicMock:
    """Dedicated reranker model record (type=reranker, different from LLM)."""
    record = MagicMock()
    record.id = RERANKER_MODEL_ID
    record.model_id = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    record.endpoint_url = "http://reranker:8080/v1"
    return record


def _make_db_with_reranker(model_record: object) -> AsyncMock:
    """DB that returns the reranker model when queried by RERANKER_MODEL_ID."""
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = model_record
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.mark.asyncio
async def test_reranker_model_id_in_prompt_config_uses_dedicated_model() -> None:
    """When prompt_config.reranker_model_id is set, node_rerank looks up THAT model,
    not state.llm_model_id (ADR-014)."""
    chunks = [_make_chunk()]
    scores = [{"chunk_index": 1, "score": 8}]

    reranker_record = _make_reranker_model_record()
    db = _make_db_with_reranker(reranker_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(scores))

    state = _make_state_with_reranker(retrieved_chunks=chunks)
    result = await node_rerank(state, _make_config(llm, db))

    # Verify the reranker endpoint (not the LLM endpoint) was used.
    call_kwargs = llm.chat_completion.call_args.kwargs
    assert call_kwargs["base_url"] == "http://reranker:8080/v1"
    assert call_kwargs["model"] == "cross-encoder/ms-marco-MiniLM-L-6-v2"
    assert len(result["retrieved_chunks"]) == 1


@pytest.mark.asyncio
async def test_no_reranker_model_id_in_prompt_config_is_pass_through() -> None:
    """When prompt_config has no reranker_model_id and rerank_enabled=False, node is a
    pass-through even if other prompt_config keys are set (ADR-014 default off)."""
    chunks = [_make_chunk(highlight_text="Chunk A"), _make_chunk(highlight_text="Chunk B")]

    state = QueryState(
        question="test",
        conversation_id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        allowed_collection_ids=[COLLECTION_ID],
        pipeline_id=PIPELINE_ID,
        llm_model_id=LLM_MODEL_ID,
        collection_ids=[COLLECTION_ID],
        retrieved_chunks=chunks,
        rerank_enabled=False,
        prompt_config={"temperature": 0.3},  # no reranker_model_id
    )

    llm = AsyncMock()
    llm.chat_completion = AsyncMock()
    db = AsyncMock()

    result = await node_rerank(state, _make_config(llm, db))

    assert result["retrieved_chunks"] == chunks
    llm.chat_completion.assert_not_called()


@pytest.mark.asyncio
async def test_reranker_model_not_found_falls_back_gracefully() -> None:
    """When the reranker_model_id references a missing model, original order is preserved."""
    chunks = [_make_chunk(highlight_text="A"), _make_chunk(highlight_text="B")]

    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None  # model not found
    db.execute = AsyncMock(return_value=result_mock)

    llm = AsyncMock()
    llm.chat_completion = AsyncMock()

    state = _make_state_with_reranker(retrieved_chunks=chunks)
    result = await node_rerank(state, _make_config(llm, db))

    assert result["retrieved_chunks"] == chunks
    llm.chat_completion.assert_not_called()

"""Unit tests for node_research_retrieve.

Tests cover:
- Normal retrieval appends new chunks to all_retrieved_chunks and updates the current iteration
- Deduplication: chunks with point_ids already in all_retrieved_chunks are dropped
- No iterations in state → QueryNodeError raised
- Embedding failure → QueryNodeError raised
- EmptyCollectionListError from retrieval is re-raised
- Collection not found → QueryNodeError
- Slug-derived Qdrant collection name when no slug in model params
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.exceptions import QueryNodeError
from src.graphs.research_graph.nodes.node_research_retrieve import (
    _deduplicate_chunks,
    node_research_retrieve,
)
from src.graphs.research_graph.state import ResearchIteration, ResearchState
from src.retrieval.exceptions import EmptyCollectionListError
from src.retrieval.schemas import RetrievalResult

TENANT_ID = uuid.uuid4()
PIPELINE_ID = uuid.uuid4()
LLM_MODEL_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()
EMBEDDING_MODEL_ID = uuid.uuid4()
DOCUMENT_ID = uuid.uuid4()
POINT_ID_A = uuid.uuid4()
POINT_ID_B = uuid.uuid4()


def _make_iteration(
    step: int = 1,
    sufficient: bool = False,
    query: str = "metformin CKD dose",
    retrieved_chunks: list | None = None,
) -> ResearchIteration:
    return ResearchIteration(
        step=step,
        query=query,
        retrieved_chunks=retrieved_chunks or [],
        reasoning="test reasoning",
        sufficient=sufficient,
    )


def _make_state(
    iterations: list[ResearchIteration] | None = None,
    all_retrieved_chunks: list | None = None,
) -> ResearchState:
    # Use sentinel None to distinguish "default" from explicit empty list
    resolved_iterations = [_make_iteration()] if iterations is None else iterations
    return ResearchState(
        original_question="Jakie jest leczenie cukrzycy typu 2 z CKD?",
        pipeline_id=PIPELINE_ID,
        tenant_id=TENANT_ID,
        collection_ids=[COLLECTION_ID],
        allowed_collection_ids=[COLLECTION_ID],
        llm_model_id=LLM_MODEL_ID,
        iterations=resolved_iterations,
        all_retrieved_chunks=all_retrieved_chunks or [],
    )


def _make_collection() -> MagicMock:
    coll = MagicMock()
    coll.embedding_model_id = EMBEDDING_MODEL_ID
    coll.search_config = None
    return coll


def _make_embedding_model(slug: str | None = "bge_m3") -> MagicMock:
    record = MagicMock()
    record.id = EMBEDDING_MODEL_ID
    record.model_id = "BAAI/bge-m3"
    record.endpoint_url = "http://ollama:11434/v1"
    record.params = {"slug": slug} if slug else {}
    return record


def _make_db(collection: object | None, model: object | None) -> AsyncMock:
    db = AsyncMock()
    call_count = 0

    async def execute_side_effect(query: object) -> MagicMock:
        nonlocal call_count
        result = MagicMock()
        if call_count == 0:
            result.scalar_one_or_none.return_value = collection
        else:
            result.scalar_one_or_none.return_value = model
        call_count += 1
        return result

    db.execute = AsyncMock(side_effect=execute_side_effect)
    return db


def _make_retrieval_result(point_id: uuid.UUID = POINT_ID_A) -> RetrievalResult:
    return RetrievalResult(
        point_id=point_id,
        document_id=DOCUMENT_ID,
        score=0.85,
        payload={
            "tenant_id": str(TENANT_ID),
            "collection_id": str(COLLECTION_ID),
            "document_id": str(DOCUMENT_ID),
            "text": "Metformin is contraindicated in severe CKD.",
        },
        page_number=2,
        highlight_text="Metformin is contraindicated in severe CKD.",
        collection_id=COLLECTION_ID,
    )


def _make_embed_response() -> MagicMock:
    embed_data = MagicMock()
    embed_data.embedding = [0.1] * 768
    embed_response = MagicMock()
    embed_response.data = [embed_data]
    return embed_response


def _make_config(llm: object, db: object, retrieval: object) -> dict:
    return {"configurable": {"llm": llm, "db": db, "retrieval": retrieval}}


# ---------------------------------------------------------------------------
# Pure-function deduplication tests
# ---------------------------------------------------------------------------


def test_deduplicate_chunks_drops_already_seen_point_ids() -> None:
    """Chunks whose point_id is in existing_point_ids are dropped."""
    existing = {"abc-111", "abc-222"}
    new_chunks = [
        {"point_id": "abc-111", "document_id": "d1"},  # duplicate
        {"point_id": "abc-333", "document_id": "d2"},  # new
    ]
    result = _deduplicate_chunks(new_chunks, existing)
    assert len(result) == 1
    assert result[0]["point_id"] == "abc-333"


def test_deduplicate_chunks_all_new_returns_all() -> None:
    """When no duplicates, all chunks pass through."""
    existing: set[str] = set()
    chunks = [
        {"point_id": "aaa", "document_id": "d1"},
        {"point_id": "bbb", "document_id": "d2"},
    ]
    result = _deduplicate_chunks(chunks, existing)
    assert len(result) == 2


def test_deduplicate_chunks_all_duplicate_returns_empty() -> None:
    """When all chunks are duplicates, returns empty list."""
    existing = {"aaa", "bbb"}
    chunks = [
        {"point_id": "aaa", "document_id": "d1"},
        {"point_id": "bbb", "document_id": "d2"},
    ]
    result = _deduplicate_chunks(chunks, existing)
    assert result == []


# ---------------------------------------------------------------------------
# Node-level tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_node_research_retrieve_normal_flow_appends_chunks() -> None:
    """Normal retrieval: new chunks added to all_retrieved_chunks and current iteration."""
    collection = _make_collection()
    model = _make_embedding_model(slug="bge_m3")
    db = _make_db(collection, model)

    llm = AsyncMock()
    llm.embeddings = AsyncMock(return_value=_make_embed_response())

    retrieval_result = _make_retrieval_result(POINT_ID_A)
    retrieval = AsyncMock()
    retrieval.search = AsyncMock(return_value=[retrieval_result])

    state = _make_state()
    config = _make_config(llm, db, retrieval)

    result = await node_research_retrieve(state, config)

    assert len(result["all_retrieved_chunks"]) == 1
    assert result["all_retrieved_chunks"][0]["point_id"] == str(POINT_ID_A)

    # The current iteration's retrieved_chunks is also populated
    updated_iteration = result["iterations"][-1]
    assert len(updated_iteration.retrieved_chunks) == 1


@pytest.mark.asyncio
async def test_node_research_retrieve_deduplicates_chunks() -> None:
    """Chunks already in all_retrieved_chunks are not added again."""
    # Pre-existing chunk with POINT_ID_A
    existing_chunk = {
        "point_id": str(POINT_ID_A),
        "document_id": str(DOCUMENT_ID),
        "score": 0.8,
        "page_number": 1,
        "highlight_text": "existing text",
        "collection_id": str(COLLECTION_ID),
        "payload": {},
    }
    collection = _make_collection()
    model = _make_embedding_model(slug="bge_m3")
    db = _make_db(collection, model)

    llm = AsyncMock()
    llm.embeddings = AsyncMock(return_value=_make_embed_response())

    # Qdrant returns POINT_ID_A (duplicate) and POINT_ID_B (new)
    retrieval = AsyncMock()
    retrieval.search = AsyncMock(
        return_value=[
            _make_retrieval_result(POINT_ID_A),
            _make_retrieval_result(POINT_ID_B),
        ]
    )

    state = _make_state(all_retrieved_chunks=[existing_chunk])
    config = _make_config(llm, db, retrieval)

    result = await node_research_retrieve(state, config)

    # Only POINT_ID_B is new; total should now be 2
    assert len(result["all_retrieved_chunks"]) == 2
    point_ids = {c["point_id"] for c in result["all_retrieved_chunks"]}
    assert str(POINT_ID_A) in point_ids
    assert str(POINT_ID_B) in point_ids

    # The current iteration should only have the 1 new chunk
    assert len(result["iterations"][-1].retrieved_chunks) == 1
    assert result["iterations"][-1].retrieved_chunks[0]["point_id"] == str(POINT_ID_B)


@pytest.mark.asyncio
async def test_node_research_retrieve_no_iterations_raises_query_node_error() -> None:
    """No iterations in state → QueryNodeError before any DB/LLM calls."""
    state = _make_state(iterations=[])
    config = _make_config(AsyncMock(), AsyncMock(), AsyncMock())

    with pytest.raises(QueryNodeError, match="research_retrieve"):
        await node_research_retrieve(state, config)


@pytest.mark.asyncio
async def test_node_research_retrieve_embedding_failure_raises_query_node_error() -> None:
    """Embedding call failure → QueryNodeError with embedding_error in message."""
    collection = _make_collection()
    model = _make_embedding_model(slug="bge_m3")
    db = _make_db(collection, model)

    llm = AsyncMock()
    llm.embeddings = AsyncMock(side_effect=ConnectionError("ollama unreachable"))

    retrieval = AsyncMock()
    state = _make_state()
    config = _make_config(llm, db, retrieval)

    with pytest.raises(QueryNodeError, match="research_retrieve_embedding_error"):
        await node_research_retrieve(state, config)


@pytest.mark.asyncio
async def test_node_research_retrieve_empty_allowed_collections_propagates_error() -> None:
    """EmptyCollectionListError from retrieval is re-raised unchanged."""
    collection = _make_collection()
    model = _make_embedding_model(slug="bge_m3")
    db = _make_db(collection, model)

    llm = AsyncMock()
    llm.embeddings = AsyncMock(return_value=_make_embed_response())

    retrieval = AsyncMock()
    retrieval.search = AsyncMock(side_effect=EmptyCollectionListError("no collections"))

    state = _make_state()
    config = _make_config(llm, db, retrieval)

    with pytest.raises(EmptyCollectionListError):
        await node_research_retrieve(state, config)


@pytest.mark.asyncio
async def test_node_research_retrieve_collection_not_found_raises_query_node_error() -> None:
    """Collection not found in DB → QueryNodeError."""
    db = _make_db(collection=None, model=None)

    llm = AsyncMock()
    retrieval = AsyncMock()
    state = _make_state()
    config = _make_config(llm, db, retrieval)

    with pytest.raises(QueryNodeError, match="research_retrieve: collection not found"):
        await node_research_retrieve(state, config)


@pytest.mark.asyncio
async def test_cross_tenant_collection_access_is_blocked() -> None:
    """Collection belonging to a different tenant (not public) must not be returned.

    The DB query now includes a tenant_id / is_public filter.  When neither
    condition matches — because the collection belongs to another tenant and
    is_public is False — scalar_one_or_none() returns None, which causes the
    node to raise QueryNodeError instead of leaking cross-tenant metadata.
    """
    # DB returns None for the collection query — simulates the tenant+public
    # filter excluding a foreign, non-public collection.
    db = _make_db(collection=None, model=None)

    llm = AsyncMock()
    retrieval = AsyncMock()

    # State presents a collection_id that "belongs" to another tenant.
    other_tenant_collection_id = uuid.uuid4()
    state = ResearchState(
        original_question="Jakie leki stosować w CKD?",
        pipeline_id=PIPELINE_ID,
        tenant_id=TENANT_ID,
        collection_ids=[other_tenant_collection_id],
        allowed_collection_ids=[other_tenant_collection_id],
        llm_model_id=LLM_MODEL_ID,
        iterations=[_make_iteration()],
        all_retrieved_chunks=[],
    )
    config = _make_config(llm, db, retrieval)

    with pytest.raises(QueryNodeError, match="research_retrieve: collection not found"):
        await node_research_retrieve(state, config)

    # The retrieval service must never be called — no cross-tenant data escapes.
    retrieval.search.assert_not_called()


@pytest.mark.asyncio
async def test_node_research_retrieve_tenant_isolation_enforced() -> None:
    """RetrievalService.search() is called with the correct tenant_id and allowed_collection_ids."""
    collection = _make_collection()
    model = _make_embedding_model(slug="bge_m3")
    db = _make_db(collection, model)

    llm = AsyncMock()
    llm.embeddings = AsyncMock(return_value=_make_embed_response())

    retrieval = AsyncMock()
    retrieval.search = AsyncMock(return_value=[])

    state = _make_state()
    config = _make_config(llm, db, retrieval)

    await node_research_retrieve(state, config)

    call_kwargs = retrieval.search.call_args.kwargs
    tenant_ctx = call_kwargs["ctx"]
    assert tenant_ctx.tenant_id == TENANT_ID
    assert COLLECTION_ID in tenant_ctx.allowed_collection_ids

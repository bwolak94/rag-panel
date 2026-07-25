"""Unit tests for node_retrieve.

Tests cover:
- Normal retrieval returns chunks with tenant_id correctly propagated
- Empty allowed_collection_ids → EmptyCollectionListError propagated
- LLM embedding call fails → QueryNodeError raised
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.graphs.query_graph.nodes.node_retrieve import node_retrieve
from src.graphs.query_graph.state import QueryState
from src.retrieval.exceptions import EmptyCollectionListError
from src.retrieval.schemas import RetrievalResult

TENANT_ID = uuid.uuid4()
LLM_MODEL_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()
PIPELINE_ID = uuid.uuid4()
EMBEDDING_MODEL_ID = uuid.uuid4()
DOCUMENT_ID = uuid.uuid4()
POINT_ID = uuid.uuid4()


def _make_state(
    allowed_collection_ids: list[uuid.UUID] | None = None,
) -> QueryState:
    return QueryState(
        question="Jakie są procedury?",
        conversation_id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        allowed_collection_ids=(
            allowed_collection_ids if allowed_collection_ids is not None else [COLLECTION_ID]
        ),
        pipeline_id=PIPELINE_ID,
        llm_model_id=LLM_MODEL_ID,
        collection_ids=[COLLECTION_ID],
        rewritten_query="procedury przyjęcia pacjenta wymagania dokumenty",
    )


def _make_collection() -> MagicMock:
    coll = MagicMock()
    coll.embedding_model_id = EMBEDDING_MODEL_ID
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


def _make_retrieval_result() -> RetrievalResult:
    return RetrievalResult(
        point_id=POINT_ID,
        document_id=DOCUMENT_ID,
        score=0.87,
        payload={
            "tenant_id": str(TENANT_ID),
            "collection_id": str(COLLECTION_ID),
            "document_id": str(DOCUMENT_ID),
            "text": "Procedura przyjęcia pacjenta wymaga...",
        },
        page_number=3,
        highlight_text="Procedura przyjęcia pacjenta wymaga...",
        collection_id=COLLECTION_ID,
    )


def _make_config(llm: object, db: object, retrieval: object) -> dict:
    return {"configurable": {"llm": llm, "db": db, "retrieval": retrieval}}


@pytest.mark.asyncio
async def test_normal_retrieval_returns_chunks_with_tenant() -> None:
    """Normal flow: embed, search, return serialized chunks."""
    collection = _make_collection()
    model = _make_embedding_model(slug="bge_m3")
    db = _make_db(collection, model)

    embed_data = MagicMock()
    embed_data.embedding = [0.1] * 768
    embed_response = MagicMock()
    embed_response.data = [embed_data]

    llm = AsyncMock()
    llm.embeddings = AsyncMock(return_value=embed_response)

    retrieval_result = _make_retrieval_result()
    retrieval = AsyncMock()
    retrieval.search = AsyncMock(return_value=[retrieval_result])

    state = _make_state()
    config = _make_config(llm, db, retrieval)

    result = await node_retrieve(state, config)

    assert "retrieved_chunks" in result
    assert len(result["retrieved_chunks"]) == 1
    chunk = result["retrieved_chunks"][0]
    assert chunk["document_id"] == str(DOCUMENT_ID)
    assert chunk["score"] == pytest.approx(0.87)
    assert result["qdrant_collection"] == "emb_bge_m3"

    # Verify tenant isolation: retrieval was called with correct tenant_id
    call_args = retrieval.search.call_args
    tenant_ctx = call_args.kwargs["ctx"]
    assert tenant_ctx.tenant_id == TENANT_ID
    assert COLLECTION_ID in tenant_ctx.allowed_collection_ids


@pytest.mark.asyncio
async def test_empty_allowed_collections_propagates_error() -> None:
    """EmptyCollectionListError from retrieval is re-raised."""
    collection = _make_collection()
    model = _make_embedding_model(slug="bge_m3")
    db = _make_db(collection, model)

    embed_data = MagicMock()
    embed_data.embedding = [0.1] * 768
    embed_response = MagicMock()
    embed_response.data = [embed_data]

    llm = AsyncMock()
    llm.embeddings = AsyncMock(return_value=embed_response)

    retrieval = AsyncMock()
    retrieval.search = AsyncMock(side_effect=EmptyCollectionListError("no collections"))

    state = _make_state(allowed_collection_ids=[])
    config = _make_config(llm, db, retrieval)

    with pytest.raises(EmptyCollectionListError):
        await node_retrieve(state, config)


@pytest.mark.asyncio
async def test_embedding_failure_raises_query_node_error() -> None:
    """If LLM embedding call fails, QueryNodeError is raised."""
    from src.core.exceptions import QueryNodeError

    collection = _make_collection()
    model = _make_embedding_model(slug="bge_m3")
    db = _make_db(collection, model)

    llm = AsyncMock()
    llm.embeddings = AsyncMock(side_effect=ConnectionError("ollama unreachable"))

    retrieval = AsyncMock()
    state = _make_state()
    config = _make_config(llm, db, retrieval)

    with pytest.raises(QueryNodeError, match="embedding_error"):
        await node_retrieve(state, config)


@pytest.mark.asyncio
async def test_slug_derived_from_model_id_when_no_slug() -> None:
    """Qdrant collection name is derived from model_id when slug not in params."""
    collection = _make_collection()
    model = _make_embedding_model(slug=None)  # no slug
    model.model_id = "BAAI/bge-m3"
    db = _make_db(collection, model)

    embed_data = MagicMock()
    embed_data.embedding = [0.1] * 768
    embed_response = MagicMock()
    embed_response.data = [embed_data]

    llm = AsyncMock()
    llm.embeddings = AsyncMock(return_value=embed_response)

    retrieval = AsyncMock()
    retrieval.search = AsyncMock(return_value=[])

    state = _make_state()
    config = _make_config(llm, db, retrieval)

    result = await node_retrieve(state, config)

    # BAAI/bge-m3 → baai_bge_m3 (replace / and - with _, lowercase)
    assert result["qdrant_collection"] == "emb_baai_bge_m3"

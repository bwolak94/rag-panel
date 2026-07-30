"""Unit tests for node_embed."""

from __future__ import annotations

import hashlib
import math
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.exceptions import IngestNodeError
from src.graphs.ingest_graph.nodes.node_embed import _BATCH_SIZE, node_embed
from src.graphs.ingest_graph.state import ChunkData, IngestState

TENANT_ID = uuid.uuid4()
DOCUMENT_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()
JOB_ID = uuid.uuid4()
EMBEDDING_MODEL_ID = uuid.uuid4()


def _make_point_id(doc_id: uuid.UUID, idx: int) -> uuid.UUID:
    return uuid.UUID(hashlib.sha256(f"{doc_id}:{idx}".encode()).hexdigest()[:32])


def _make_chunks(n: int) -> list[ChunkData]:
    return [
        ChunkData(
            chunk_index=i,
            text=f"chunk text {i}",
            page=i + 1,
            section=None,
            token_count=10,
            point_id=_make_point_id(DOCUMENT_ID, i),
        )
        for i in range(n)
    ]


def _make_state(**kwargs: object) -> IngestState:
    return IngestState(
        document_id=DOCUMENT_ID,
        tenant_id=TENANT_ID,
        collection_id=COLLECTION_ID,
        minio_key=f"raw/{COLLECTION_ID}/{DOCUMENT_ID}/doc.pdf",
        job_id=JOB_ID,
        **kwargs,
    )


def _make_config(session: object, llm: object) -> dict:
    return {"configurable": {"db": session, "llm": llm}}


def _make_session() -> AsyncMock:
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock())
    session.commit = AsyncMock()
    return session


def _make_collection(embedding_model_id: uuid.UUID = EMBEDDING_MODEL_ID) -> MagicMock:
    collection = MagicMock()
    collection.embedding_model_id = embedding_model_id
    return collection


def _make_model_record(
    model_id: str = "bge-m3",
    endpoint_url: str = "http://ollama:11434",
    record_id: uuid.UUID | None = None,
) -> MagicMock:
    model = MagicMock()
    model.model_id = model_id
    model.endpoint_url = endpoint_url
    model.id = record_id or uuid.uuid4()
    return model


def _make_embeddings_response(n: int, dim: int = 384) -> MagicMock:
    """Build a mock llm.embeddings response with n embedding items."""
    items = []
    for i in range(n):
        item = MagicMock()
        item.embedding = [0.1 * i] * dim
        items.append(item)
    response = MagicMock()
    response.data = items
    return response


@pytest.mark.asyncio
async def test_embeddings_returned() -> None:
    """3 chunks → llm.embeddings called once → state['embeddings'] has 3 vectors."""
    n = 3
    chunks = _make_chunks(n)
    session = _make_session()
    llm = MagicMock()
    llm.embeddings = AsyncMock(return_value=_make_embeddings_response(n))
    state = _make_state(chunks=chunks)

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_embed.get_collection",
            new=AsyncMock(return_value=_make_collection()),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_embed.get_model",
            new=AsyncMock(return_value=_make_model_record()),
        ),
        patch("src.graphs.ingest_graph.nodes.node_embed.update_step", new=AsyncMock()),
    ):
        result = await node_embed(state, _make_config(session, llm))

    assert len(result["embeddings"]) == n
    assert all(isinstance(v, list) for v in result["embeddings"])


@pytest.mark.asyncio
async def test_empty_chunks_raises_ingest_node_error() -> None:
    """chunks=[] → IngestNodeError before any API call."""
    session = _make_session()
    llm = MagicMock()
    llm.embeddings = AsyncMock()
    state = _make_state(chunks=[])

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_embed.get_collection",
            new=AsyncMock(return_value=_make_collection()),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_embed.get_model",
            new=AsyncMock(return_value=_make_model_record()),
        ),
        patch("src.graphs.ingest_graph.nodes.node_embed.update_step", new=AsyncMock()),
        pytest.raises(IngestNodeError, match="no chunks to embed"),
    ):
        await node_embed(state, _make_config(session, llm))

    llm.embeddings.assert_not_called()


@pytest.mark.asyncio
async def test_llm_api_error_raises_ingest_node_error() -> None:
    """llm.embeddings raises Exception → IngestNodeError with embed_api_error."""
    chunks = _make_chunks(3)
    session = _make_session()
    llm = MagicMock()
    llm.embeddings = AsyncMock(side_effect=Exception("API unreachable"))
    state = _make_state(chunks=chunks)

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_embed.get_collection",
            new=AsyncMock(return_value=_make_collection()),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_embed.get_model",
            new=AsyncMock(return_value=_make_model_record()),
        ),
        patch("src.graphs.ingest_graph.nodes.node_embed.update_step", new=AsyncMock()),
        pytest.raises(IngestNodeError, match="embed_api_error"),
    ):
        await node_embed(state, _make_config(session, llm))


@pytest.mark.asyncio
async def test_batching_respects_batch_size() -> None:
    """70 chunks → llm.embeddings called ceil(70/32) = 3 times."""
    n = 70
    chunks = _make_chunks(n)
    session = _make_session()

    call_count = 0

    async def embeddings_side_effect(**kwargs: object) -> MagicMock:
        nonlocal call_count
        batch = kwargs.get("input", [])
        call_count += 1
        return _make_embeddings_response(len(batch))

    llm = MagicMock()
    llm.embeddings = embeddings_side_effect
    state = _make_state(chunks=chunks)

    expected_calls = math.ceil(n / _BATCH_SIZE)

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_embed.get_collection",
            new=AsyncMock(return_value=_make_collection()),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_embed.get_model",
            new=AsyncMock(return_value=_make_model_record()),
        ),
        patch("src.graphs.ingest_graph.nodes.node_embed.update_step", new=AsyncMock()),
    ):
        result = await node_embed(state, _make_config(session, llm))

    assert call_count == expected_calls
    assert len(result["embeddings"]) == n


@pytest.mark.asyncio
async def test_uses_model_endpoint_from_registry() -> None:
    """llm.embeddings is called with model_id and base_url from the model record."""
    n = 2
    chunks = _make_chunks(n)
    session = _make_session()
    model_record = _make_model_record(model_id="nomic-embed-text", endpoint_url="http://vllm:8080")

    captured_kwargs: dict = {}

    async def capture_embeddings(**kwargs: object) -> MagicMock:
        captured_kwargs.update(kwargs)
        return _make_embeddings_response(len(kwargs.get("input", [])))

    llm = MagicMock()
    llm.embeddings = capture_embeddings
    state = _make_state(chunks=chunks)

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_embed.get_collection",
            new=AsyncMock(return_value=_make_collection()),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_embed.get_model",
            new=AsyncMock(return_value=model_record),
        ),
        patch("src.graphs.ingest_graph.nodes.node_embed.update_step", new=AsyncMock()),
    ):
        await node_embed(state, _make_config(session, llm))

    assert captured_kwargs.get("model") == "nomic-embed-text"
    assert captured_kwargs.get("base_url") == "http://vllm:8080"

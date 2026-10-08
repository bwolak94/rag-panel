"""Unit tests for node_upsert."""

from __future__ import annotations

import hashlib
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.graphs.ingest_graph.nodes.node_upsert import node_upsert
from src.graphs.ingest_graph.state import ChunkData, IngestState
from src.retrieval.schemas import QdrantPoint, TenantContext
from src.retrieval.service import RetrievalService

TENANT_ID = uuid.uuid4()
DOCUMENT_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()
JOB_ID = uuid.uuid4()


def _make_point_id(doc_id: uuid.UUID, idx: int) -> uuid.UUID:
    return uuid.UUID(hashlib.sha256(f"{doc_id}:{idx}".encode()).hexdigest()[:32])


def _make_chunks(n: int = 3) -> list[ChunkData]:
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


def _make_embeddings(n: int = 3) -> list[list[float]]:
    return [[0.1 * i] * 384 for i in range(n)]


def _make_state(**kwargs: object) -> IngestState:
    return IngestState(
        document_id=DOCUMENT_ID,
        tenant_id=TENANT_ID,
        collection_id=COLLECTION_ID,
        minio_key=f"raw/{COLLECTION_ID}/{DOCUMENT_ID}/doc.pdf",
        job_id=JOB_ID,
        **kwargs,
    )


def _make_config(session: object, retrieval: object) -> dict:
    return {"configurable": {"db": session, "retrieval": retrieval}}


def _make_session(model_name: str = "BGE-M3") -> AsyncMock:
    session = AsyncMock()
    # scalar_one_or_none() returns model_name for the embedding model lookup
    scalar_result = MagicMock()
    scalar_result.scalar_one_or_none.return_value = model_name
    session.execute = AsyncMock(return_value=scalar_result)
    session.commit = AsyncMock()
    return session


@pytest.mark.asyncio
async def test_upsert_calls_retrieval_service() -> None:
    """RetrievalService.upsert_batch is called with correct TenantContext and points."""
    n = 3
    chunks = _make_chunks(n)
    embeddings = _make_embeddings(n)

    session = _make_session()
    retrieval = MagicMock(spec=RetrievalService)
    retrieval.upsert_batch = AsyncMock()
    state = _make_state(chunks=chunks, embeddings=embeddings)

    with patch("src.graphs.ingest_graph.nodes.node_upsert.update_step", new=AsyncMock()):
        result = await node_upsert(state, _make_config(session, retrieval))

    retrieval.upsert_batch.assert_called_once()
    call_ctx, call_collection, call_points = retrieval.upsert_batch.call_args[0]
    assert isinstance(call_ctx, TenantContext)
    assert call_ctx.tenant_id == TENANT_ID
    assert call_ctx.allowed_collection_ids == [COLLECTION_ID]
    assert call_collection == "emb_bge_m3"
    assert len(call_points) == n
    assert len(result["point_ids"]) == n


@pytest.mark.asyncio
async def test_upsert_payload_contains_tenant_id() -> None:
    """Every Qdrant point payload must contain tenant_id == state.tenant_id."""
    chunks = _make_chunks(2)
    embeddings = _make_embeddings(2)

    session = _make_session()
    retrieval = MagicMock(spec=RetrievalService)
    captured_points: list[QdrantPoint] = []

    async def capture_upsert(
        ctx: TenantContext, qdrant_collection: str, points: list[QdrantPoint]
    ) -> None:
        captured_points.extend(points)

    retrieval.upsert_batch = capture_upsert
    state = _make_state(chunks=chunks, embeddings=embeddings)

    with patch("src.graphs.ingest_graph.nodes.node_upsert.update_step", new=AsyncMock()):
        await node_upsert(state, _make_config(session, retrieval))

    assert len(captured_points) == 2
    for point in captured_points:
        assert str(TENANT_ID) == point.payload["tenant_id"]


@pytest.mark.asyncio
async def test_upsert_no_direct_qdrant_import() -> None:
    """node_upsert.py must not import qdrant_client directly."""
    import importlib
    import sys

    mod_name = "src.graphs.ingest_graph.nodes.node_upsert"
    mod = sys.modules[mod_name] if mod_name in sys.modules else importlib.import_module(mod_name)

    # Check source lines for direct import statement (not just docstring mentions)
    import inspect

    import_lines = [
        line.strip()
        for line in inspect.getsource(mod).splitlines()
        if line.strip().startswith(("import ", "from ")) and "qdrant_client" in line
    ]
    assert not import_lines, f"node_upsert must not import qdrant_client directly: {import_lines}"


@pytest.mark.asyncio
async def test_upsert_chunk_embedding_mismatch_raises() -> None:
    """Chunk/embedding count mismatch → IngestNodeError."""
    from src.core.exceptions import IngestNodeError

    chunks = _make_chunks(3)
    embeddings = _make_embeddings(2)  # mismatch: 3 vs 2

    session = _make_session()
    retrieval = MagicMock(spec=RetrievalService)
    state = _make_state(chunks=chunks, embeddings=embeddings)

    with (
        patch("src.graphs.ingest_graph.nodes.node_upsert.update_step", new=AsyncMock()),
        pytest.raises(IngestNodeError, match="mismatch"),
    ):
        await node_upsert(state, _make_config(session, retrieval))


# ---------------------------------------------------------------------------
# ADR-013: Sparse vector encoding in node_upsert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_attaches_sparse_vectors_when_fastembed_available() -> None:
    """When fastembed encoding succeeds, each QdrantPoint receives a non-None sparse_vector."""
    from qdrant_client.models import SparseVector

    n = 2
    chunks = _make_chunks(n)
    embeddings = _make_embeddings(n)

    session = _make_session()
    retrieval = MagicMock(spec=RetrievalService)
    captured_points: list[QdrantPoint] = []

    async def capture_upsert(ctx: object, collection: str, points: list[QdrantPoint]) -> None:
        captured_points.extend(points)

    retrieval.upsert_batch = capture_upsert
    state = _make_state(chunks=chunks, embeddings=embeddings)

    fake_sparse = SparseVector(indices=[0, 5], values=[0.3, 0.7])

    with (
        patch("src.graphs.ingest_graph.nodes.node_upsert.update_step", new=AsyncMock()),
        patch(
            "src.retrieval.service._encode_sparse_sync",
            return_value=fake_sparse,
        ),
    ):
        await node_upsert(state, _make_config(session, retrieval))

    assert len(captured_points) == n
    for point in captured_points:
        assert point.sparse_vector is not None, "sparse_vector must be set when fastembed succeeds"
        assert isinstance(point.sparse_vector, SparseVector)


@pytest.mark.asyncio
async def test_upsert_falls_back_to_none_sparse_when_encode_fails() -> None:
    """When fastembed encoding raises, sparse_vector falls back to None (graceful degradation)."""
    n = 2
    chunks = _make_chunks(n)
    embeddings = _make_embeddings(n)

    session = _make_session()
    retrieval = MagicMock(spec=RetrievalService)
    captured_points: list[QdrantPoint] = []

    async def capture_upsert(ctx: object, collection: str, points: list[QdrantPoint]) -> None:
        captured_points.extend(points)

    retrieval.upsert_batch = capture_upsert
    state = _make_state(chunks=chunks, embeddings=embeddings)

    with (
        patch("src.graphs.ingest_graph.nodes.node_upsert.update_step", new=AsyncMock()),
        patch(
            "src.retrieval.service._encode_sparse_sync",
            side_effect=RuntimeError("fastembed unavailable"),
        ),
    ):
        await node_upsert(state, _make_config(session, retrieval))

    assert len(captured_points) == n
    for point in captured_points:
        assert point.sparse_vector is None, "sparse_vector must be None when encoding fails"

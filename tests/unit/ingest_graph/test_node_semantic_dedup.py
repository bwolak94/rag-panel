"""Unit tests for node_semantic_dedup.

Tests cover:
- _mean_pool: correct element-wise mean
- _cosine_similarity: correct cosine computation, zero-vector handling
- Near-duplicate detection: similarity >= threshold → needs_review (or rejected)
- Unique document: similarity < threshold → continue pipeline (empty dict)
- No embeddings → IngestNodeError
- Exact hash short-circuit via similar doc score == 1.0
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.exceptions import IngestNodeError
from src.graphs.ingest_graph.nodes.node_semantic_dedup import (
    _cosine_similarity,
    _mean_pool,
    node_semantic_dedup,
)
from src.graphs.ingest_graph.state import IngestState

TENANT_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()
DOCUMENT_ID = uuid.uuid4()
JOB_ID = uuid.uuid4()


def _make_state(embeddings: list[list[float]] | None = None) -> IngestState:
    return IngestState(
        document_id=DOCUMENT_ID,
        tenant_id=TENANT_ID,
        collection_id=COLLECTION_ID,
        minio_key="test/doc.pdf",
        job_id=JOB_ID,
        sha256="abc123",
        embeddings=embeddings,
    )


def _make_config(db: Any) -> dict[str, Any]:
    return {"configurable": {"db": db}}


def _make_db(
    existing_docs: list[tuple[uuid.UUID, list[float]]] | None = None,
    chunk_config: dict[str, Any] | None = None,
) -> MagicMock:
    collection = MagicMock()
    collection.chunk_config = chunk_config or {}

    db = MagicMock()
    execute_results = []

    # update call 1: set dedup_embedding
    res1 = MagicMock()
    execute_results.append(res1)

    # select call: existing docs
    existing = existing_docs or []
    res2 = MagicMock()
    res2.all = MagicMock(return_value=existing)
    execute_results.append(res2)

    # update call 2: set dedup_similarity
    res3 = MagicMock()
    execute_results.append(res3)

    # update call 3 (if near-dup): set status
    res4 = MagicMock()
    execute_results.append(res4)

    call_count = [0]

    async def _execute(stmt: Any) -> MagicMock:
        idx = call_count[0]
        call_count[0] += 1
        return execute_results[min(idx, len(execute_results) - 1)]

    db.execute = _execute
    db.flush = AsyncMock()
    db.commit = AsyncMock()

    return db, collection


# ── _mean_pool ────────────────────────────────────────────────────────────────


def test_mean_pool_single_vector() -> None:
    result = _mean_pool([[1.0, 2.0, 3.0]])
    assert result == [1.0, 2.0, 3.0]


def test_mean_pool_multiple_vectors() -> None:
    result = _mean_pool([[1.0, 0.0], [0.0, 1.0]])
    assert result == [0.5, 0.5]


def test_mean_pool_empty_raises() -> None:
    with pytest.raises(ValueError, match="empty"):
        _mean_pool([])


# ── _cosine_similarity ────────────────────────────────────────────────────────


def test_cosine_similarity_identical_vectors() -> None:
    v = [1.0, 2.0, 3.0]
    assert _cosine_similarity(v, v) == pytest.approx(1.0)


def test_cosine_similarity_orthogonal_vectors() -> None:
    assert _cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_similarity_zero_vector_returns_zero() -> None:
    assert _cosine_similarity([0.0, 0.0], [1.0, 2.0]) == 0.0


def test_cosine_similarity_opposite_vectors() -> None:
    sim = _cosine_similarity([1.0, 0.0], [-1.0, 0.0])
    assert sim == pytest.approx(-1.0)


# ── node_semantic_dedup ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unique_document_returns_empty_dict() -> None:
    """Similarity below threshold → unique, returns {}."""
    embeddings = [[1.0, 0.0], [0.9, 0.1]]
    state = _make_state(embeddings)

    # Existing doc with orthogonal embedding — very low similarity
    other_id = uuid.uuid4()
    other_embedding = [0.0, 1.0]
    db, collection = _make_db(
        existing_docs=[(other_id, other_embedding)],
        chunk_config={"dedup_threshold": 0.95},
    )

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_semantic_dedup.get_collection",
            AsyncMock(return_value=collection),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_semantic_dedup.update_step",
            AsyncMock(),
        ),
    ):
        result = await node_semantic_dedup(state, _make_config(db))

    assert result == {}


@pytest.mark.asyncio
async def test_near_duplicate_routes_to_needs_review() -> None:
    """Similarity >= threshold and reject_near_duplicates=False → needs_review."""
    embeddings = [[1.0, 0.0]]
    state = _make_state(embeddings)

    other_id = uuid.uuid4()
    # Identical embedding → similarity = 1.0 >= 0.95
    db, collection = _make_db(
        existing_docs=[(other_id, [1.0, 0.0])],
        chunk_config={"dedup_threshold": 0.95, "reject_near_duplicates": False},
    )

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_semantic_dedup.get_collection",
            AsyncMock(return_value=collection),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_semantic_dedup.update_step",
            AsyncMock(),
        ),
    ):
        result = await node_semantic_dedup(state, _make_config(db))

    assert result["status"] == "needs_review"
    assert result["halt"] is True


@pytest.mark.asyncio
async def test_near_duplicate_with_reject_flag_routes_to_rejected() -> None:
    """reject_near_duplicates=True → status rejected."""
    embeddings = [[1.0, 0.0]]
    state = _make_state(embeddings)

    other_id = uuid.uuid4()
    db, collection = _make_db(
        existing_docs=[(other_id, [1.0, 0.0])],
        chunk_config={"dedup_threshold": 0.95, "reject_near_duplicates": True},
    )

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_semantic_dedup.get_collection",
            AsyncMock(return_value=collection),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_semantic_dedup.update_step",
            AsyncMock(),
        ),
    ):
        result = await node_semantic_dedup(state, _make_config(db))

    assert result["status"] == "rejected"
    assert result["halt"] is True


@pytest.mark.asyncio
async def test_no_embeddings_raises_ingest_node_error() -> None:
    """State with no embeddings → IngestNodeError."""
    state = _make_state(embeddings=None)
    db = MagicMock()
    db.flush = AsyncMock()

    collection = MagicMock()
    collection.chunk_config = {}

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_semantic_dedup.get_collection",
            AsyncMock(return_value=collection),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_semantic_dedup.update_step",
            AsyncMock(),
        ),
        pytest.raises(IngestNodeError, match="no embeddings"),
    ):
        await node_semantic_dedup(state, _make_config(db))

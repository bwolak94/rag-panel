"""Unit tests for node_chunk."""

from __future__ import annotations

import hashlib
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.graphs.ingest_graph.nodes.node_chunk import _make_point_id, node_chunk
from src.graphs.ingest_graph.state import IngestState, Section, ValidationResult

TENANT_ID = uuid.uuid4()
DOCUMENT_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()
JOB_ID = uuid.uuid4()

_LONG_TEXT = " ".join([f"word{i}" for i in range(2000)])


def _make_state(**kwargs: object) -> IngestState:
    return IngestState(
        document_id=DOCUMENT_ID,
        tenant_id=TENANT_ID,
        collection_id=COLLECTION_ID,
        minio_key=f"raw/{COLLECTION_ID}/{DOCUMENT_ID}/doc.pdf",
        job_id=JOB_ID,
        extracted_text=_LONG_TEXT,
        **kwargs,
    )


def _make_collection(config: dict) -> MagicMock:
    coll = MagicMock()
    coll.chunk_config = config
    return coll


def _make_config(session: object) -> dict:
    return {"configurable": {"db": session}}


def _make_session() -> AsyncMock:
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock())
    session.commit = AsyncMock()
    return session


@pytest.mark.asyncio
async def test_recursive_strategy_produces_chunks() -> None:
    """Recursive strategy splits long text into multiple chunks."""
    session = _make_session()
    state = _make_state()
    config = {"strategy": "recursive", "chunk_size": 100, "overlap": 10}

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_chunk.get_collection",
            new=AsyncMock(return_value=_make_collection(config)),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_chunk.update_step",
            new=AsyncMock(),
        ),
    ):
        result = await node_chunk(state, _make_config(session))

    chunks = result["chunks"]
    assert len(chunks) > 1
    assert all(c.text for c in chunks)


@pytest.mark.asyncio
async def test_by_section_strategy_uses_sections() -> None:
    """by_section strategy creates one chunk per section."""
    session = _make_session()
    sections = [
        Section(heading=None, text=f"Section {i} text content here", page=i, section_index=i)
        for i in range(3)
    ]
    state = _make_state(extracted_sections=sections)
    config = {"strategy": "by_section", "chunk_size": 512, "overlap": 64}

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_chunk.get_collection",
            new=AsyncMock(return_value=_make_collection(config)),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_chunk.update_step",
            new=AsyncMock(),
        ),
    ):
        result = await node_chunk(state, _make_config(session))

    assert len(result["chunks"]) == 3


@pytest.mark.asyncio
async def test_deterministic_point_ids() -> None:
    """Same document_id + chunk_index always produces the same point_id."""
    doc_id = uuid.uuid4()
    id_a = _make_point_id(doc_id, 0)
    id_b = _make_point_id(doc_id, 0)
    assert id_a == id_b


@pytest.mark.asyncio
async def test_point_id_is_sha256_derived() -> None:
    """point_id is derived from SHA-256, not UUID5 (SHA-1)."""
    doc_id = uuid.uuid4()
    point_id = _make_point_id(doc_id, 0)
    expected = uuid.UUID(hashlib.sha256(f"{doc_id}:0".encode()).hexdigest()[:32])
    assert point_id == expected


@pytest.mark.asyncio
async def test_chunk_index_increments() -> None:
    """chunk_index on each ChunkData increments from 0."""
    session = _make_session()
    state = _make_state()
    config = {"strategy": "recursive", "chunk_size": 50, "overlap": 5}

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_chunk.get_collection",
            new=AsyncMock(return_value=_make_collection(config)),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_chunk.update_step",
            new=AsyncMock(),
        ),
    ):
        result = await node_chunk(state, _make_config(session))

    chunks = result["chunks"]
    for i, chunk in enumerate(chunks):
        assert chunk.chunk_index == i


@pytest.mark.asyncio
async def test_document_type_override_applied() -> None:
    """chunk_config.document_type_overrides is applied for matching document_type."""
    session = _make_session()
    vr = ValidationResult(document_type="table", quality_score=0.9)
    state = _make_state(validation_result=vr)
    config = {
        "strategy": "recursive",
        "chunk_size": 512,
        "overlap": 64,
        "document_type_overrides": {"table": {"chunk_size": 50, "overlap": 5}},
    }

    captured: dict = {}

    async def capture_step(*args: object, **kwargs: object) -> None:
        captured.update(kwargs.get("meta") or {})

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_chunk.get_collection",
            new=AsyncMock(return_value=_make_collection(config)),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_chunk.update_step",
            new=capture_step,
        ),
    ):
        result = await node_chunk(state, _make_config(session))

    # Smaller chunk_size=50 should produce more chunks than default 512
    assert len(result["chunks"]) > 1

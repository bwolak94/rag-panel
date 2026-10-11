"""Unit tests for node_persist."""

from __future__ import annotations

import hashlib
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.graphs.ingest_graph.nodes.node_persist import node_persist
from src.graphs.ingest_graph.state import ChunkData, IngestState

TENANT_ID = uuid.uuid4()
DOCUMENT_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()
JOB_ID = uuid.uuid4()


def _make_point_id(idx: int) -> uuid.UUID:
    return uuid.UUID(hashlib.sha256(f"{DOCUMENT_ID}:{idx}".encode()).hexdigest()[:32])


def _make_chunks(n: int = 3) -> list[ChunkData]:
    return [
        ChunkData(
            chunk_index=i,
            text=f"chunk {i}",
            page=i + 1,
            section=None,
            token_count=10,
            point_id=_make_point_id(i),
        )
        for i in range(n)
    ]


def _make_point_ids(n: int = 3) -> list[uuid.UUID]:
    return [_make_point_id(i) for i in range(n)]


def _make_state(**kwargs: object) -> IngestState:
    n = 3
    return IngestState(
        document_id=DOCUMENT_ID,
        tenant_id=TENANT_ID,
        collection_id=COLLECTION_ID,
        minio_key=f"raw/{COLLECTION_ID}/{DOCUMENT_ID}/doc.pdf",
        job_id=JOB_ID,
        chunks=_make_chunks(n),
        point_ids=_make_point_ids(n),
        **kwargs,
    )


def _make_config(session: object) -> dict:
    return {"configurable": {"db": session}}


def _make_session() -> AsyncMock:
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock())
    session.commit = AsyncMock()
    session.add = MagicMock()
    return session


@pytest.mark.asyncio
async def test_persist_sets_document_ready() -> None:
    """node_persist updates document.status to 'ready'."""
    session = _make_session()
    state = _make_state()

    with patch("src.graphs.ingest_graph.nodes.node_persist.update_step", new=AsyncMock()):
        result = await node_persist(state, _make_config(session))

    assert result == {"status": "ready"}


@pytest.mark.asyncio
async def test_persist_inserts_chunks_registry() -> None:
    """DB execute is called at least twice (insert + update doc + update job)."""
    session = _make_session()
    state = _make_state()

    with patch("src.graphs.ingest_graph.nodes.node_persist.update_step", new=AsyncMock()):
        await node_persist(state, _make_config(session))

    # chunks_registry insert + document status update + job status update
    assert session.execute.call_count >= 2


@pytest.mark.asyncio
async def test_persist_writes_audit_log() -> None:
    """An AuditLog entry is added to the session with action='document.indexed'."""
    session = _make_session()
    state = _make_state()

    with patch("src.graphs.ingest_graph.nodes.node_persist.update_step", new=AsyncMock()):
        await node_persist(state, _make_config(session))

    session.add.assert_called_once()
    audit_entry = session.add.call_args[0][0]
    from src.db.models.audit_log import AuditLog

    assert isinstance(audit_entry, AuditLog)
    assert audit_entry.action == "document.indexed"
    assert audit_entry.tenant_id == TENANT_ID


@pytest.mark.asyncio
async def test_persist_commits_session() -> None:
    """Session is committed after all DB operations."""
    session = _make_session()
    state = _make_state()

    with patch("src.graphs.ingest_graph.nodes.node_persist.update_step", new=AsyncMock()):
        await node_persist(state, _make_config(session))

    session.commit.assert_called()


# ---------------------------------------------------------------------------
# ADR-023: entity persistence path
# ---------------------------------------------------------------------------

_ENTITY_BATCH = [
    {
        "entities": [
            {
                "type": "drug",
                "name": "metformin",
                "surface_forms": ["Metformin", "metformin"],
                "icd_code": None,
                "atc_code": "A10BA02",
                "confidence": 0.95,
                "chunk_ids": ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"],
            }
        ],
        "relations": [
            {
                "source_name": "metformin",
                "target_name": "type 2 diabetes",
                "relation_type": "treats",
                "confidence": 0.9,
                "evidence_text_hash": "abc123",
            }
        ],
    }
]


@pytest.mark.asyncio
async def test_persist_calls_entity_insert_when_extracted_entities_set() -> None:
    """ADR-023: when extracted_entities is not None, _persist_entities is called."""
    session = _make_session()
    state = _make_state(extracted_entities=_ENTITY_BATCH)

    with (
        patch("src.graphs.ingest_graph.nodes.node_persist.update_step", new=AsyncMock()),
        patch(
            "src.graphs.ingest_graph.nodes.node_persist._persist_entities",
            new=AsyncMock(return_value=(1, 1)),
        ) as mock_persist_entities,
    ):
        result = await node_persist(state, _make_config(session))

    assert result == {"status": "ready"}
    mock_persist_entities.assert_called_once()
    call_kwargs = mock_persist_entities.call_args.kwargs
    assert call_kwargs["tenant_id"] == TENANT_ID
    assert call_kwargs["document_id"] == DOCUMENT_ID
    assert call_kwargs["extracted_entities"] == _ENTITY_BATCH


@pytest.mark.asyncio
async def test_persist_skips_entity_insert_when_extracted_entities_none() -> None:
    """ADR-023: when extracted_entities is None, _persist_entities is not called."""
    session = _make_session()
    state = _make_state(extracted_entities=None)

    with (
        patch("src.graphs.ingest_graph.nodes.node_persist.update_step", new=AsyncMock()),
        patch(
            "src.graphs.ingest_graph.nodes.node_persist._persist_entities",
            new=AsyncMock(return_value=(0, 0)),
        ) as mock_persist_entities,
    ):
        await node_persist(state, _make_config(session))

    mock_persist_entities.assert_not_called()


@pytest.mark.asyncio
async def test_persist_entity_tenant_id_always_from_state() -> None:
    """ADR-023: tenant_id passed to _persist_entities comes from state, not from LLM output."""
    session = _make_session()
    state = _make_state(extracted_entities=_ENTITY_BATCH)

    with (
        patch("src.graphs.ingest_graph.nodes.node_persist.update_step", new=AsyncMock()),
        patch(
            "src.graphs.ingest_graph.nodes.node_persist._persist_entities",
            new=AsyncMock(return_value=(1, 0)),
        ) as mock_persist_entities,
    ):
        await node_persist(state, _make_config(session))

    call_kwargs = mock_persist_entities.call_args.kwargs
    # tenant_id must come from state, never from LLM output
    assert call_kwargs["tenant_id"] == TENANT_ID

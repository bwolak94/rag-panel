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


# ---------------------------------------------------------------------------
# ADR-024: version promotion tests
# ---------------------------------------------------------------------------

PREVIOUS_DOC_ID = uuid.uuid4()


def _make_config_with_retrieval(session: object, retrieval: object | None = None) -> dict:
    cfg: dict = {"configurable": {"db": session}}
    if retrieval is not None:
        cfg["configurable"]["retrieval"] = retrieval
    return cfg


@pytest.mark.asyncio
async def test_persist_version_promotion_called_when_previous_version_id_set() -> None:
    """ADR-024: both demotion helpers are called when previous_version_id is set."""
    session = _make_session()
    state = _make_state(previous_version_id=PREVIOUS_DOC_ID)
    mock_retrieval = AsyncMock()

    with (
        patch("src.graphs.ingest_graph.nodes.node_persist.update_step", new=AsyncMock()),
        patch(
            "src.graphs.ingest_graph.nodes.node_persist._demote_previous_version_postgres",
            new=AsyncMock(),
        ) as mock_pg,
        patch(
            "src.graphs.ingest_graph.nodes.node_persist._demote_previous_version_qdrant",
            new=AsyncMock(),
        ) as mock_qdrant,
    ):
        result = await node_persist(state, _make_config_with_retrieval(session, mock_retrieval))

    assert result == {"status": "ready"}
    mock_pg.assert_called_once()
    mock_qdrant.assert_called_once()
    assert mock_pg.call_args.kwargs["state"].previous_version_id == PREVIOUS_DOC_ID


@pytest.mark.asyncio
async def test_persist_version_promotion_skipped_when_no_previous_version() -> None:
    """ADR-024: demotion helpers are NOT called when previous_version_id is None."""
    session = _make_session()
    state = _make_state(previous_version_id=None)

    with (
        patch("src.graphs.ingest_graph.nodes.node_persist.update_step", new=AsyncMock()),
        patch(
            "src.graphs.ingest_graph.nodes.node_persist._demote_previous_version_postgres",
            new=AsyncMock(),
        ) as mock_pg,
        patch(
            "src.graphs.ingest_graph.nodes.node_persist._demote_previous_version_qdrant",
            new=AsyncMock(),
        ) as mock_qdrant,
    ):
        await node_persist(state, _make_config(session))

    mock_pg.assert_not_called()
    mock_qdrant.assert_not_called()


@pytest.mark.asyncio
async def test_persist_raises_when_retrieval_absent_and_previous_version_set() -> None:
    """ADR-024: IngestNodeError is raised when previous_version_id is set but retrieval absent."""
    from src.core.exceptions import IngestNodeError

    session = _make_session()
    state = _make_state(previous_version_id=PREVIOUS_DOC_ID)

    with (
        patch("src.graphs.ingest_graph.nodes.node_persist.update_step", new=AsyncMock()),
        pytest.raises(IngestNodeError, match="retrieval"),
    ):
        # No retrieval in config
        await node_persist(state, _make_config(session))


@pytest.mark.asyncio
async def test_persist_demote_updates_postgres_is_current_false() -> None:
    """ADR-024: _demote_previous_version_postgres executes an UPDATE setting is_current=False."""
    from src.graphs.ingest_graph.nodes.node_persist import _demote_previous_version_postgres

    session = _make_session()
    state = _make_state(previous_version_id=PREVIOUS_DOC_ID)

    await _demote_previous_version_postgres(session=session, state=state)

    session.execute.assert_called_once()


@pytest.mark.asyncio
async def test_persist_demote_calls_retrieval_update_payload() -> None:
    """ADR-024: _demote_previous_version_qdrant calls update_payload for previous version."""
    from src.graphs.ingest_graph.nodes.node_persist import _demote_previous_version_qdrant

    session = _make_session()
    # Make session return a model name for the collection lookup
    model_name_result = MagicMock()
    model_name_result.scalar_one_or_none.return_value = "bge-m3"
    session.execute = AsyncMock(return_value=model_name_result)

    mock_retrieval = AsyncMock()
    mock_retrieval.update_payload = AsyncMock()

    state = _make_state(previous_version_id=PREVIOUS_DOC_ID)

    await _demote_previous_version_qdrant(session=session, retrieval=mock_retrieval, state=state)

    mock_retrieval.update_payload.assert_called_once()
    call_kwargs = mock_retrieval.update_payload.call_args.kwargs
    assert call_kwargs["document_id"] == PREVIOUS_DOC_ID
    assert call_kwargs["payload_patch"] == {"is_current": False}
    assert call_kwargs["qdrant_collection"] == "emb_bge_m3"

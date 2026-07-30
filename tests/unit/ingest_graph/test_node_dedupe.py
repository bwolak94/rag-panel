"""Unit tests for node_dedupe."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.graphs.ingest_graph.nodes.node_dedupe import node_dedupe
from src.graphs.ingest_graph.state import IngestState

TENANT_ID = uuid.uuid4()
DOCUMENT_ID = uuid.uuid4()
OTHER_DOCUMENT_ID = uuid.uuid4()
OTHER_TENANT_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()
JOB_ID = uuid.uuid4()
SHA256 = "a" * 64


def _make_state(**kwargs: object) -> IngestState:
    return IngestState(
        document_id=DOCUMENT_ID,
        tenant_id=TENANT_ID,
        collection_id=COLLECTION_ID,
        minio_key=f"raw/{COLLECTION_ID}/{DOCUMENT_ID}/doc.pdf",
        job_id=JOB_ID,
        sha256=SHA256,
        **kwargs,
    )


def _make_config(session: object) -> dict:
    return {"configurable": {"db": session}}


def _make_session(existing_id: uuid.UUID | None) -> AsyncMock:
    session = AsyncMock()
    scalar_result = MagicMock()
    scalar_result.scalar_one_or_none.return_value = existing_id
    session.execute = AsyncMock(return_value=scalar_result)
    session.commit = AsyncMock()
    return session


@pytest.mark.asyncio
async def test_unique_document_returns_empty() -> None:
    """No duplicate found → return {} with no status change."""
    session = _make_session(None)
    state = _make_state()

    with patch("src.graphs.ingest_graph.nodes.node_dedupe.update_step", new=AsyncMock()):
        result = await node_dedupe(state, _make_config(session))

    assert result == {}


@pytest.mark.asyncio
async def test_duplicate_detected_halts() -> None:
    """Duplicate sha256 for same tenant → {"status": "rejected", "halt": True}."""
    session = _make_session(OTHER_DOCUMENT_ID)
    state = _make_state()

    with patch("src.graphs.ingest_graph.nodes.node_dedupe.update_step", new=AsyncMock()):
        result = await node_dedupe(state, _make_config(session))

    assert result["status"] == "rejected"
    assert result["halt"] is True


@pytest.mark.asyncio
async def test_duplicate_updates_document_status() -> None:
    """On duplicate, DB UPDATE sets document.status = 'rejected'."""
    session = _make_session(OTHER_DOCUMENT_ID)
    state = _make_state()

    with patch("src.graphs.ingest_graph.nodes.node_dedupe.update_step", new=AsyncMock()):
        await node_dedupe(state, _make_config(session))

    # execute called twice: SELECT for duplicate check + UPDATE for status
    assert session.execute.call_count == 2
    session.commit.assert_called()


@pytest.mark.asyncio
async def test_step_meta_contains_result_key() -> None:
    """update_step is called with meta.result in ["unique", "duplicate"]."""
    session = _make_session(None)
    state = _make_state()

    captured_meta: dict = {}

    async def capture_update_step(*args: object, **kwargs: object) -> None:
        captured_meta.update(kwargs.get("meta") or {})

    with patch(
        "src.graphs.ingest_graph.nodes.node_dedupe.update_step",
        new=capture_update_step,
    ):
        await node_dedupe(state, _make_config(session))

    assert captured_meta.get("result") == "unique"

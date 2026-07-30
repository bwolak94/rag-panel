"""Unit tests for node_fetch."""

from __future__ import annotations

import hashlib
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.exceptions import IngestNodeError
from src.graphs.ingest_graph.nodes.node_fetch import node_fetch
from src.graphs.ingest_graph.state import IngestState

TENANT_ID = uuid.uuid4()
DOCUMENT_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()
JOB_ID = uuid.uuid4()

RAW_BYTES = b"hello world document content"
SHA256 = hashlib.sha256(RAW_BYTES).hexdigest()


def _make_state(**kwargs: object) -> IngestState:
    return IngestState(
        document_id=DOCUMENT_ID,
        tenant_id=TENANT_ID,
        collection_id=COLLECTION_ID,
        minio_key=f"raw/{COLLECTION_ID}/{DOCUMENT_ID}/test.pdf",
        job_id=JOB_ID,
        **kwargs,
    )


def _make_config(session: object, minio: object) -> dict:
    return {"configurable": {"db": session, "minio": minio}}


def _make_session(document: object) -> AsyncMock:
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = document
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    return session


def _make_minio(raw_bytes: bytes) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = raw_bytes
    minio = MagicMock()
    minio.get_object.return_value = resp
    return minio


@pytest.mark.asyncio
async def test_fetch_success() -> None:
    """Happy path: MinIO returns bytes; SHA-256 matches document record."""
    doc = MagicMock()
    doc.minio_key = f"raw/{COLLECTION_ID}/{DOCUMENT_ID}/test.pdf"
    doc.sha256 = SHA256

    session = _make_session(doc)
    minio = _make_minio(RAW_BYTES)
    state = _make_state()

    with patch("src.graphs.ingest_graph.nodes.node_fetch.update_step", new=AsyncMock()):
        result = await node_fetch(state, _make_config(session, minio))

    assert result["raw_bytes"] == RAW_BYTES
    assert result["sha256"] == SHA256


@pytest.mark.asyncio
async def test_fetch_sha256_mismatch_raises() -> None:
    """SHA-256 mismatch between stored value and downloaded bytes → IngestNodeError."""
    doc = MagicMock()
    doc.minio_key = f"raw/{COLLECTION_ID}/{DOCUMENT_ID}/test.pdf"
    doc.sha256 = "a" * 64  # wrong hash

    session = _make_session(doc)
    minio = _make_minio(RAW_BYTES)
    state = _make_state()

    with (
        patch("src.graphs.ingest_graph.nodes.node_fetch.update_step", new=AsyncMock()),
        pytest.raises(IngestNodeError, match="SHA-256 mismatch"),
    ):
        await node_fetch(state, _make_config(session, minio))


@pytest.mark.asyncio
async def test_fetch_document_not_found_raises() -> None:
    """Document missing from DB → IngestNodeError."""
    session = _make_session(None)
    minio = _make_minio(RAW_BYTES)
    state = _make_state()

    with (
        patch("src.graphs.ingest_graph.nodes.node_fetch.update_step", new=AsyncMock()),
        pytest.raises(IngestNodeError, match="not found"),
    ):
        await node_fetch(state, _make_config(session, minio))


@pytest.mark.asyncio
async def test_fetch_minio_error_wraps_as_ingest_error() -> None:
    """MinIO connection error is wrapped in IngestNodeError."""
    doc = MagicMock()
    doc.minio_key = f"raw/{COLLECTION_ID}/{DOCUMENT_ID}/test.pdf"
    doc.sha256 = SHA256

    session = _make_session(doc)
    minio = MagicMock()
    minio.get_object.side_effect = ConnectionError("MinIO unavailable")
    state = _make_state()

    with (
        patch("src.graphs.ingest_graph.nodes.node_fetch.update_step", new=AsyncMock()),
        pytest.raises(IngestNodeError, match="fetch_error"),
    ):
        await node_fetch(state, _make_config(session, minio))


@pytest.mark.asyncio
async def test_fetch_empty_sha256_skips_verification() -> None:
    """If document.sha256 is empty, SHA-256 check is skipped (pre-upload state)."""
    doc = MagicMock()
    doc.minio_key = f"raw/{COLLECTION_ID}/{DOCUMENT_ID}/test.pdf"
    doc.sha256 = ""  # empty — no stored hash yet

    session = _make_session(doc)
    minio = _make_minio(RAW_BYTES)
    state = _make_state()

    with patch("src.graphs.ingest_graph.nodes.node_fetch.update_step", new=AsyncMock()):
        result = await node_fetch(state, _make_config(session, minio))

    assert result["sha256"] == SHA256

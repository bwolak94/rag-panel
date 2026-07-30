"""Unit tests for node_extract."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.exceptions import IngestNodeError
from src.graphs.ingest_graph.nodes.node_extract import node_extract
from src.graphs.ingest_graph.state import IngestState, Section

TENANT_ID = uuid.uuid4()
DOCUMENT_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()
JOB_ID = uuid.uuid4()

RAW_BYTES = b"Hello world.\n\nThis is a test document.\n\nAnother paragraph here."


def _make_state(raw_bytes: bytes | None = RAW_BYTES, **kwargs: object) -> IngestState:
    return IngestState(
        document_id=DOCUMENT_ID,
        tenant_id=TENANT_ID,
        collection_id=COLLECTION_ID,
        minio_key=f"raw/{COLLECTION_ID}/{DOCUMENT_ID}/test.pdf",
        job_id=JOB_ID,
        raw_bytes=raw_bytes,
        **kwargs,
    )


def _make_config(session: object, minio: object = None) -> dict:
    return {"configurable": {"db": session, "minio": minio}}


def _make_session(mime: str = "text/plain") -> AsyncMock:
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = mime
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    return session


@pytest.mark.asyncio
async def test_extracts_text_from_pdf_bytes() -> None:
    """Happy path: plain text mime type, run_in_executor returns extracted text."""
    session = _make_session(mime="text/plain")
    state = _make_state()
    expected_text = "Hello world.\n\nThis is a test document.\n\nAnother paragraph here."
    expected_sections = [
        Section(heading=None, text="Hello world.", page=None, section_index=0),
        Section(heading=None, text="This is a test document.", page=None, section_index=1),
        Section(heading=None, text="Another paragraph here.", page=None, section_index=2),
    ]

    with (
        patch("src.graphs.ingest_graph.nodes.node_extract.update_step", new=AsyncMock()),
        patch(
            "asyncio.get_running_loop",
            return_value=MagicMock(
                run_in_executor=AsyncMock(return_value=(expected_text, expected_sections))
            ),
        ),
    ):
        result = await node_extract(state, _make_config(session))

    assert result["extracted_text"] == expected_text
    assert result["extracted_sections"] == expected_sections


@pytest.mark.asyncio
async def test_empty_bytes_sets_error_status() -> None:
    """raw_bytes is None → IngestNodeError raised before extraction."""
    session = _make_session(mime="text/plain")
    state = _make_state(raw_bytes=None)  # override default RAW_BYTES

    with (
        patch("src.graphs.ingest_graph.nodes.node_extract.update_step", new=AsyncMock()),
        pytest.raises(IngestNodeError, match="raw_bytes is None"),
    ):
        await node_extract(state, _make_config(session))


@pytest.mark.asyncio
async def test_extraction_failure_raises_ingest_node_error() -> None:
    """run_in_executor raises → node wraps in IngestNodeError."""
    session = _make_session(mime="text/plain")
    state = _make_state()

    with (
        patch("src.graphs.ingest_graph.nodes.node_extract.update_step", new=AsyncMock()),
        patch(
            "asyncio.get_running_loop",
            return_value=MagicMock(
                run_in_executor=AsyncMock(side_effect=RuntimeError("extraction failed"))
            ),
        ),
        pytest.raises(IngestNodeError, match="extract_error"),
    ):
        await node_extract(state, _make_config(session))


@pytest.mark.asyncio
async def test_unsupported_mime_type_raises_ingest_node_error() -> None:
    """MIME type not in _SUPPORTED_MIMES → IngestNodeError with unsupported_mime_type."""
    session = _make_session(mime="image/png")
    state = _make_state()

    with (
        patch("src.graphs.ingest_graph.nodes.node_extract.update_step", new=AsyncMock()),
        pytest.raises(IngestNodeError, match="unsupported_mime_type"),
    ):
        await node_extract(state, _make_config(session))


@pytest.mark.asyncio
async def test_section_extraction_succeeds() -> None:
    """Sections extracted from text mime appear in state output."""
    session = _make_session(mime="text/plain")
    state = _make_state()

    sections = [
        Section(heading=None, text="First paragraph", page=None, section_index=0),
        Section(heading=None, text="Second paragraph", page=None, section_index=1),
    ]

    with (
        patch("src.graphs.ingest_graph.nodes.node_extract.update_step", new=AsyncMock()),
        patch(
            "asyncio.get_running_loop",
            return_value=MagicMock(
                run_in_executor=AsyncMock(
                    return_value=("First paragraph\n\nSecond paragraph", sections)
                )
            ),
        ),
    ):
        result = await node_extract(state, _make_config(session))

    assert len(result["extracted_sections"]) == 2
    assert result["extracted_sections"][0].text == "First paragraph"
    assert result["extracted_sections"][1].text == "Second paragraph"


@pytest.mark.asyncio
async def test_minio_put_called_when_minio_present() -> None:
    """When minio is provided in config, extracted.json is uploaded."""
    session = _make_session(mime="text/plain")
    minio = MagicMock()
    minio.put_object = MagicMock()
    state = _make_state()

    sections = [Section(heading=None, text="Hello", page=None, section_index=0)]

    loop_mock = MagicMock()
    # First call for extraction, second call for minio put
    loop_mock.run_in_executor = AsyncMock(side_effect=[("Hello", sections), None])

    with (
        patch("src.graphs.ingest_graph.nodes.node_extract.update_step", new=AsyncMock()),
        patch("asyncio.get_running_loop", return_value=loop_mock),
    ):
        result = await node_extract(state, _make_config(session, minio=minio))

    assert result["extracted_text"] == "Hello"
    # run_in_executor called twice: once for extraction, once for minio put
    assert loop_mock.run_in_executor.call_count == 2

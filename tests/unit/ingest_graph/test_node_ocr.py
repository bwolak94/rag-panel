"""Unit tests for node_ocr and route_after_extract.

All OCR sub-processes (pdf2image, pytesseract) are mocked so the tests run
without Tesseract, poppler, or any heavy dependencies installed.

GDPR / security note: none of the assertions print or log ocr_text content.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.graphs.ingest_graph.nodes.node_ocr import node_ocr
from src.graphs.ingest_graph.routing import route_after_extract
from src.graphs.ingest_graph.state import IngestState

TENANT_ID = uuid.uuid4()
DOCUMENT_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()
JOB_ID = uuid.uuid4()

# Minimal valid PDF header bytes (magic bytes only — not a real PDF).
_PDF_HEADER = b"%PDF-1.4 fake content"


def _make_state(**kwargs: object) -> IngestState:
    return IngestState(
        document_id=DOCUMENT_ID,
        tenant_id=TENANT_ID,
        collection_id=COLLECTION_ID,
        minio_key=f"raw/{COLLECTION_ID}/{DOCUMENT_ID}/scan.pdf",
        job_id=JOB_ID,
        **kwargs,
    )


def _make_collection(ocr_enabled: bool = True, ocr_lang: str = "pol+eng") -> MagicMock:
    collection = MagicMock()
    collection.chunk_config = {"ocr_enabled": ocr_enabled, "ocr_lang": ocr_lang}
    return collection


def _make_session(collection: MagicMock) -> AsyncMock:
    """Build an AsyncMock DB session that returns *collection* for get_collection()."""
    session = AsyncMock()
    scalar_result = MagicMock()
    scalar_result.scalar_one_or_none.return_value = collection
    session.execute = AsyncMock(return_value=scalar_result)
    session.commit = AsyncMock()
    return session


def _make_config(session: AsyncMock, minio: MagicMock | None = None) -> dict:
    return {"configurable": {"db": session, "minio": minio}}


# ---------------------------------------------------------------------------
# Routing tests
# ---------------------------------------------------------------------------


def test_route_after_extract_ocr_needed() -> None:
    """needs_ocr=True must route to node_ocr."""
    state = _make_state(needs_ocr=True)
    assert route_after_extract(state) == "node_ocr"


def test_route_after_extract_no_ocr() -> None:
    """needs_ocr=False must route directly to node_dedupe."""
    state = _make_state(needs_ocr=False)
    assert route_after_extract(state) == "node_dedupe"


# ---------------------------------------------------------------------------
# node_ocr — skipped when ocr_enabled=False
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_node_ocr_skipped_when_disabled() -> None:
    """When ocr_enabled=False in chunk_config the node returns an empty dict
    (no state mutation) and does NOT attempt any OCR."""
    collection = _make_collection(ocr_enabled=False)
    session = _make_session(collection)
    state = _make_state(raw_bytes=_PDF_HEADER, needs_ocr=True)
    config = _make_config(session)

    # Patch _ocr_pdf_bytes to assert it is never called.
    with patch("src.graphs.ingest_graph.nodes.node_ocr._ocr_pdf_bytes") as mock_ocr:
        result = await node_ocr(state, config)

    mock_ocr.assert_not_called()
    assert result == {}


# ---------------------------------------------------------------------------
# node_ocr — happy path with mocked OCR pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_node_ocr_sets_ocr_text() -> None:
    """When ocr_enabled=True the node rasterises the PDF and returns ocr_text,
    ocr_engine='tesseract', and ocr_page_count."""
    collection = _make_collection(ocr_enabled=True, ocr_lang="pol+eng")
    session = _make_session(collection)
    state = _make_state(raw_bytes=_PDF_HEADER, needs_ocr=True)
    config = _make_config(session)

    expected_text = "Extracted OCR text from page 1."
    expected_pages = 1

    # Patch the CPU-bound worker function that runs inside ProcessPoolExecutor.
    with patch(
        "src.graphs.ingest_graph.nodes.node_ocr._ocr_pdf_bytes",
        return_value=(expected_text, expected_pages),
    ) as mock_ocr:
        result = await node_ocr(state, config)

    # Verify the OCR worker was invoked with the correct language.
    mock_ocr.assert_called_once_with(_PDF_HEADER, "pol+eng", 300)

    # Verify state fields are populated correctly.
    assert result["ocr_engine"] == "tesseract"
    assert result["ocr_page_count"] == expected_pages
    # Verify ocr_text is set (we deliberately do NOT log/print its value).
    assert "ocr_text" in result
    assert isinstance(result["ocr_text"], str)


@pytest.mark.asyncio
async def test_node_ocr_uses_lang_from_collection() -> None:
    """ocr_lang from chunk_config must be forwarded to the OCR worker."""
    collection = _make_collection(ocr_enabled=True, ocr_lang="deu")
    session = _make_session(collection)
    state = _make_state(raw_bytes=_PDF_HEADER, needs_ocr=True)
    config = _make_config(session)

    with patch(
        "src.graphs.ingest_graph.nodes.node_ocr._ocr_pdf_bytes",
        return_value=("German text", 2),
    ) as mock_ocr:
        result = await node_ocr(state, config)

    # Language "deu" must be forwarded to the worker.
    call_args = mock_ocr.call_args
    assert call_args.args[1] == "deu"
    assert result["ocr_page_count"] == 2


@pytest.mark.asyncio
async def test_node_ocr_refetches_raw_bytes_when_none() -> None:
    """If state.raw_bytes is None, the node must fetch document bytes from MinIO."""
    collection = _make_collection(ocr_enabled=True, ocr_lang="pol+eng")
    session = _make_session(collection)

    # MinIO mock that returns PDF bytes when get_object is called.
    minio_response = MagicMock()
    minio_response.read.return_value = _PDF_HEADER
    minio_response.close = MagicMock()
    minio_response.release_conn = MagicMock()
    minio = MagicMock()
    minio.get_object.return_value = minio_response

    state = _make_state(raw_bytes=None, needs_ocr=True)
    config = _make_config(session, minio=minio)

    with patch(
        "src.graphs.ingest_graph.nodes.node_ocr._ocr_pdf_bytes",
        return_value=("Re-fetched OCR text", 1),
    ):
        result = await node_ocr(state, config)

    # MinIO must have been called to fetch the missing bytes.
    minio.get_object.assert_called_once()
    assert result["ocr_engine"] == "tesseract"


@pytest.mark.asyncio
async def test_node_ocr_raises_ingest_node_error_on_failure() -> None:
    """A failure in the OCR worker must be wrapped in IngestNodeError."""
    from src.core.exceptions import IngestNodeError

    collection = _make_collection(ocr_enabled=True)
    session = _make_session(collection)
    state = _make_state(raw_bytes=_PDF_HEADER, needs_ocr=True)
    config = _make_config(session)

    with patch(
        "src.graphs.ingest_graph.nodes.node_ocr._ocr_pdf_bytes",
        side_effect=RuntimeError("poppler not found"),
    ):
        with pytest.raises(IngestNodeError, match="ocr_error"):
            await node_ocr(state, config)

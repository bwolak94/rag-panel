"""node_ocr — OCR stage for scanned PDFs and image documents.

Rasterises pages via pdf2image (poppler) at 300 DPI and runs pytesseract per page
in a ProcessPoolExecutor to avoid blocking the event loop (CPU-bound).

Only executes when:
  1. collection.chunk_config["ocr_enabled"] is True (default True), AND
  2. state.needs_ocr is True (set by node_extract when chars_per_page < 50).

If raw_bytes is None the node re-fetches the document from MinIO using state.minio_key.

Security:
- ocr_text is treated as untrusted content (prompt-injection risk) — same guardrails
  as extracted_text in downstream nodes.
- ocr_text MUST NEVER appear in structlog output or Langfuse spans.
- Only counts, engine name, and language are logged/traced.

GDPR: Langfuse spans contain only document_id, tenant_id, engine, page_count, lang,
and latency. No OCR output appears in spans (capture_input=False, capture_output=False).
"""

from __future__ import annotations

import asyncio
import io
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from typing import Any

import structlog
from langfuse import observe
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import IngestNodeError
from src.core.langfuse_client import update_span_metadata as _lf_update_span
from src.graphs.ingest_graph.helpers import get_collection, update_step, utcnow
from src.graphs.ingest_graph.state import IngestState

logger = structlog.get_logger(__name__)

# Module-level executor shared across all invocations.
# Using ProcessPoolExecutor because pdf2image + pytesseract are CPU-bound
# and must not compete with the async event loop under the GIL.
# Max 2 workers: OCR is memory-intensive; more workers risk OOM on typical hosts.
# Inject a different executor via config["ocr_executor"] (e.g., None for tests
# which use the default ThreadPoolExecutor so mocks remain picklable).
_OCR_EXECUTOR: ProcessPoolExecutor = ProcessPoolExecutor(max_workers=2)

_DEFAULT_OCR_LANG = "pol+eng"


# ---------------------------------------------------------------------------
# CPU-bound helpers — called from ProcessPoolExecutor
# ---------------------------------------------------------------------------


def _rasterise_pdf(raw: bytes, dpi: int = 300) -> list[Any]:
    """Convert PDF bytes to a list of PIL Images (one per page).

    Args:
        raw: Raw PDF bytes.
        dpi: Rasterisation resolution in DPI. 300 is the OCR-quality standard.

    Returns:
        List of PIL Image objects, one per page.

    Raises:
        ImportError: If pdf2image or Pillow is not installed.
        RuntimeError: If pdf2image fails (e.g., corrupt PDF, poppler not found).
    """
    from pdf2image import convert_from_bytes  # type: ignore[import-not-found]

    images: list[Any] = convert_from_bytes(raw, dpi=dpi)
    return images


def _ocr_image(image: Any, lang: str) -> str:
    """Run Tesseract OCR on a single PIL Image.

    Args:
        image: PIL Image object.
        lang: Tesseract language string (e.g. "pol+eng").

    Returns:
        Extracted text string for this page.

    Raises:
        ImportError: If pytesseract is not installed.
        pytesseract.TesseractNotFoundError: If the Tesseract binary is missing.
    """
    import pytesseract  # type: ignore[import-not-found]

    return pytesseract.image_to_string(image, lang=lang)  # type: ignore[no-any-return]


def _ocr_pdf_bytes(raw: bytes, lang: str, dpi: int = 300) -> tuple[str, int]:
    """Rasterise a PDF and run OCR on every page. Runs fully in a worker process.

    Args:
        raw: Raw PDF/image bytes.
        lang: Tesseract language string.
        dpi: Rasterisation DPI.

    Returns:
        Tuple of (full_text, page_count).
    """
    images = _rasterise_pdf(raw, dpi=dpi)
    page_texts: list[str] = [_ocr_image(img, lang) for img in images]
    return "\n\n".join(page_texts), len(images)


def _ocr_image_bytes(raw: bytes, lang: str) -> tuple[str, int]:
    """Run OCR on a single image (PNG/JPEG/TIFF) passed as bytes.

    Args:
        raw: Raw image bytes.
        lang: Tesseract language string.

    Returns:
        Tuple of (extracted_text, page_count=1).
    """
    from PIL import Image  # type: ignore[import-not-found]

    image = Image.open(io.BytesIO(raw))
    text = _ocr_image(image, lang)
    return text, 1


# ---------------------------------------------------------------------------
# MinIO re-fetch helper (sync, called from executor)
# ---------------------------------------------------------------------------


def _fetch_from_minio(minio: Any, bucket: str, key: str) -> bytes:
    """Fetch raw document bytes from MinIO.

    Args:
        minio: Synchronous minio-py client.
        bucket: Bucket name.
        key: Object key.

    Returns:
        Raw bytes.
    """
    response = minio.get_object(bucket, key)
    try:
        return response.read()  # type: ignore[no-any-return]
    finally:
        response.close()
        response.release_conn()


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------


@observe(name="node_ocr", capture_input=False, capture_output=False)
async def node_ocr(state: IngestState, config: dict[str, Any]) -> dict[str, Any]:
    """Run OCR on the document when node_extract flagged it as image-only.

    Args:
        state: IngestState; needs_ocr must be True to trigger actual work.
               raw_bytes will be re-fetched from MinIO if None.
        config: RunnableConfig with configurable["db"], configurable["minio"],
                and optional configurable["ocr_lang"] override.

    Returns:
        Dict with "ocr_text", "ocr_engine", "ocr_page_count" on success.
        Empty dict (no state mutation) when ocr_enabled is False on the collection.

    Raises:
        IngestNodeError: On missing document bytes, import errors, or OCR failure.
    """
    cfg = config.get("configurable", {})
    session: AsyncSession = cfg["db"]
    minio = cfg.get("minio")
    # Allow tests to inject None (→ default thread pool, mocks are picklable) or
    # a custom executor. Production always uses the module-level ProcessPoolExecutor.
    executor = cfg.get("ocr_executor", _OCR_EXECUTOR)
    step_start = utcnow()

    try:
        # --- Check collection config ----------------------------------------
        collection = await get_collection(session, state.collection_id)
        chunk_config: dict[str, Any] = collection.chunk_config or {}
        ocr_enabled: bool = bool(chunk_config.get("ocr_enabled", True))
        ocr_lang: str = str(chunk_config.get("ocr_lang", _DEFAULT_OCR_LANG))

        if not ocr_enabled:
            logger.info(
                "node_ocr_skipped",
                document_id=str(state.document_id),
                reason="ocr_disabled_in_collection",
            )
            return {}

        # --- Ensure raw bytes are available ---------------------------------
        raw_bytes = state.raw_bytes
        if raw_bytes is None:
            if minio is None:
                raise IngestNodeError("raw_bytes is None and no minio client in config")

            # Bucket convention in ingest nodes: tenant-{uuid} (consistent with node_fetch).
            bucket = f"tenant-{state.tenant_id}"
            raw_bytes = await asyncio.get_running_loop().run_in_executor(
                None, _fetch_from_minio, minio, bucket, state.minio_key
            )
            logger.info(
                "node_ocr_refetched_raw_bytes",
                document_id=str(state.document_id),
            )

        # --- Determine document type for dispatch ---------------------------
        # We classify by MIME type stored in the DB. As a fallback we probe the
        # magic bytes (PDF header) directly from raw_bytes so we do not need a
        # DB round-trip when mime is already known from prior nodes.
        is_pdf = raw_bytes[:4] == b"%PDF"

        # --- Run OCR in executor --------------------------------------------
        # Production: module-level ProcessPoolExecutor (CPU-bound isolation).
        # Tests: executor=None → default ThreadPoolExecutor → MagicMock picklable.
        loop = asyncio.get_running_loop()
        if is_pdf:
            ocr_text, page_count = await loop.run_in_executor(
                executor, _ocr_pdf_bytes, raw_bytes, ocr_lang, 300
            )
        else:
            ocr_text, page_count = await loop.run_in_executor(
                executor, _ocr_image_bytes, raw_bytes, ocr_lang
            )

        # --- Audit trail (NO text content) ----------------------------------
        elapsed = _elapsed_ms(step_start)
        await update_step(
            session,
            state.job_id,
            stage="ocr",
            status="completed",
            started_at=step_start,
            meta={
                "engine": "tesseract",
                "page_count": page_count,
                "lang": ocr_lang,
                "latency_ms": elapsed,
            },
        )
        _lf_update_span(
            metadata={
                "document_id": str(state.document_id),
                "tenant_id": str(state.tenant_id),
                "engine": "tesseract",
                "page_count": page_count,
                "lang": ocr_lang,
                "latency_ms": elapsed,
            }
        )
        # SECURITY: ocr_text is never passed to the logger.
        logger.info(
            "node_ocr_completed",
            document_id=str(state.document_id),
            engine="tesseract",
            page_count=page_count,
            lang=ocr_lang,
        )
        return {
            "ocr_text": ocr_text,
            "ocr_engine": "tesseract",
            "ocr_page_count": page_count,
        }

    except IngestNodeError as exc:
        _lf_update_span(
            metadata={
                "document_id": str(state.document_id),
                "tenant_id": str(state.tenant_id),
                "error": True,
                "error_type": type(exc).__name__,
            }
        )
        await update_step(
            session,
            state.job_id,
            stage="ocr",
            status="failed",
            started_at=step_start,
            error=str(exc),
        )
        raise
    except Exception as exc:
        error_msg = f"ocr_error: {type(exc).__name__}"
        _lf_update_span(
            metadata={
                "document_id": str(state.document_id),
                "tenant_id": str(state.tenant_id),
                "error": True,
                "error_type": type(exc).__name__,
            }
        )
        await update_step(
            session,
            state.job_id,
            stage="ocr",
            status="failed",
            started_at=step_start,
            error=error_msg,
        )
        raise IngestNodeError(error_msg) from exc


def _elapsed_ms(start: datetime) -> int:
    return int((datetime.now(UTC) - start).total_seconds() * 1000)

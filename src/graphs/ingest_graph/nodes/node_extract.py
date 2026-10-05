"""node_extract — extract text and sections from raw document bytes.

Uses Docling for layout-aware PDF/DOCX parsing (CPU-bound, run in executor).
Falls back to simple line-splitting for TXT/MD files.
Saves extracted.json to MinIO processed/ prefix for debugging.

Security:
- Never log extracted text content.
- Docling must run in executor (never block the event loop).

GDPR: Langfuse spans contain only document_id, tenant_id, page/section/word counts,
mime_type, and latency. No extracted text or document content appears in spans.
"""

from __future__ import annotations

import asyncio
import io
import json
from datetime import UTC, datetime
from typing import Any

import structlog
from langfuse import observe
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import IngestNodeError
from src.core.langfuse_client import update_span_metadata as _lf_update_span
from src.db.models.document import Document
from src.graphs.ingest_graph.helpers import update_step, utcnow
from src.graphs.ingest_graph.state import IngestState, Section

logger = structlog.get_logger(__name__)

_SUPPORTED_MIMES = frozenset(
    [
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "text/html",
        "text/markdown",
        "text/plain",
    ]
)

_TEXT_MIMES = frozenset(["text/plain", "text/markdown"])

# MIME types eligible for OCR fallback when extracted text is sparse.
_OCR_ELIGIBLE_MIMES = frozenset(
    [
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/tiff",
    ]
)

# Chars-per-page threshold below which OCR is triggered.
_OCR_CHARS_PER_PAGE_THRESHOLD = 50


def _extract_with_docling(raw: bytes, mime: str) -> tuple[str, list[Section]]:
    """Run Docling synchronously (called from executor)."""
    try:
        from docling.document_converter import DocumentConverter  # type: ignore[import-not-found]
    except ImportError:
        return _extract_plain(raw)

    try:
        converter = DocumentConverter()
        source = io.BytesIO(raw)
        result = converter.convert(source)
        doc = result.document
        full_text: str = doc.export_to_markdown()
        sections: list[Section] = []
        for idx, item in enumerate(doc.texts):
            text = getattr(item, "text", "") or ""
            if text.strip():
                sections.append(
                    Section(
                        heading=None,
                        text=text,
                        page=getattr(item, "page_no", None),
                        section_index=idx,
                    )
                )
        return full_text, sections
    except Exception:
        return _extract_plain(raw)


def _extract_plain(raw: bytes) -> tuple[str, list[Section]]:
    """Simple fallback: decode and split by double newlines."""
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        text = raw.decode("latin-1", errors="replace")
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    sections = [
        Section(heading=None, text=para, page=None, section_index=idx)
        for idx, para in enumerate(paragraphs)
    ]
    return text, sections


@observe(name="node_extract", capture_input=False, capture_output=False)
async def node_extract(state: IngestState, config: dict[str, Any]) -> dict[str, Any]:
    """Extract text and sections from raw_bytes.

    Args:
        state: Must have raw_bytes set by node_fetch.
        config: RunnableConfig with configurable["db"] and configurable["minio"].

    Returns:
        {"extracted_text": str, "extracted_sections": list[Section]}

    Raises:
        IngestNodeError: On unsupported MIME type or extraction failure.
    """
    cfg = config.get("configurable", {})
    session: AsyncSession = cfg["db"]
    minio = cfg.get("minio")
    step_start = utcnow()

    try:
        if state.raw_bytes is None:
            raise IngestNodeError("raw_bytes is None — node_fetch must run first")

        result = await session.execute(
            select(Document.mime_type).where(
                Document.id == state.document_id,
                Document.tenant_id == state.tenant_id,
            )
        )
        mime = result.scalar_one_or_none() or "application/octet-stream"

        if mime not in _SUPPORTED_MIMES:
            raise IngestNodeError(f"unsupported_mime_type: {mime}")

        loop = asyncio.get_running_loop()
        if mime in _TEXT_MIMES:
            full_text, sections = await loop.run_in_executor(None, _extract_plain, state.raw_bytes)
        else:
            full_text, sections = await loop.run_in_executor(
                None, _extract_with_docling, state.raw_bytes, mime
            )

        word_count = len(full_text.split())
        page_set = {s.page for s in sections if s.page is not None}

        # Detect sparse extraction: if fewer than _OCR_CHARS_PER_PAGE_THRESHOLD chars per
        # page (for OCR-eligible MIME types), flag for OCR. Text/MD files are never flagged.
        needs_ocr = False
        if mime in _OCR_ELIGIBLE_MIMES:
            page_count = len(page_set) if page_set else 1
            chars_per_page = len(full_text.strip()) / page_count
            if chars_per_page < _OCR_CHARS_PER_PAGE_THRESHOLD:
                needs_ocr = True

        # Save extracted.json to MinIO processed/ prefix (for debugging, not public)
        if minio is not None:
            extracted_data = json.dumps(
                [
                    {
                        "section_index": s.section_index,
                        "page": s.page,
                        "heading": s.heading,
                    }
                    for s in sections
                ]
            ).encode()
            bucket = f"tenant-{state.tenant_id}"
            key = f"processed/{state.document_id}/extracted.json"

            def _put() -> None:
                minio.put_object(
                    bucket,
                    key,
                    io.BytesIO(extracted_data),
                    length=len(extracted_data),
                    content_type="application/json",
                )

            await loop.run_in_executor(None, _put)

        elapsed = _elapsed_ms(step_start)
        await update_step(
            session,
            state.job_id,
            stage="extract",
            status="completed",
            started_at=step_start,
            meta={
                "page_count": len(page_set),
                "word_count": word_count,
                "section_count": len(sections),
                "needs_ocr": needs_ocr,
                "latency_ms": elapsed,
            },
        )
        _lf_update_span(
            metadata={
                "document_id": str(state.document_id),
                "tenant_id": str(state.tenant_id),
                "mime_type": mime,
                "page_count": len(page_set),
                "section_count": len(sections),
                "word_count": word_count,
                "needs_ocr": needs_ocr,
                "latency_ms": elapsed,
            }
        )
        logger.info(
            "node_extract_completed",
            document_id=str(state.document_id),
            section_count=len(sections),
            word_count=word_count,
            needs_ocr=needs_ocr,
        )
        return {
            "extracted_text": full_text,
            "extracted_sections": sections,
            "needs_ocr": needs_ocr,
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
            stage="extract",
            status="failed",
            started_at=step_start,
            error=str(exc),
        )
        raise
    except Exception as exc:
        error_msg = f"extract_error: {type(exc).__name__}"
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
            stage="extract",
            status="failed",
            started_at=step_start,
            error=error_msg,
        )
        raise IngestNodeError(error_msg) from exc


def _elapsed_ms(start: datetime) -> int:
    return int((datetime.now(UTC) - start).total_seconds() * 1000)

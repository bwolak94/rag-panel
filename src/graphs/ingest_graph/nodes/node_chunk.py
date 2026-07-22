"""node_chunk — split extracted text into overlapping chunks with deterministic point IDs.

Strategy is read from collections.chunk_config:
  - "recursive" (default): RecursiveCharacterTextSplitter with token counting
  - "by_section": one chunk per Section (may still split large sections)
  - "semantic": falls back to recursive (full semantic chunking is TASK-014)

Deterministic point_id: SHA-256(f"{document_id}:{chunk_index}")[:32] as UUID.
SHA-256 is the canonical algorithm per rag-conventions.md — UUID5/SHA-1 is NOT used.

Security:
- Chunk text stored in ChunkData for pipeline use; it IS logged as a count (not content).
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import IngestNodeError
from src.graphs.ingest_graph.helpers import get_collection, update_step, utcnow
from src.graphs.ingest_graph.state import ChunkData, IngestState, Section

logger = structlog.get_logger(__name__)


def _token_count(text: str) -> int:
    """Estimate token count using tiktoken; fall back to word count."""
    try:
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        return len(text.split())


def _make_point_id(document_id: uuid.UUID, chunk_index: int) -> uuid.UUID:
    """Deterministic SHA-256-derived point ID per rag-conventions.md."""
    digest = hashlib.sha256(f"{document_id}:{chunk_index}".encode()).hexdigest()
    return uuid.UUID(digest[:32])


def _chunk_by_section(
    sections: list[Section],
    chunk_size: int,
    overlap: int,
) -> list[tuple[str, int | None, str | None]]:
    """Each section becomes one chunk (may exceed chunk_size for large sections)."""
    return [(s.text, s.page, s.heading) for s in sections if s.text.strip()]


def _chunk_recursive(
    text: str,
    chunk_size: int,
    overlap: int,
) -> list[tuple[str, int | None, str | None]]:
    """Recursive character text splitting with token length function."""
    try:
        from langchain_text_splitters import RecursiveCharacterTextSplitter

        splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=overlap,
            length_function=_token_count,
        )
        chunks = splitter.split_text(text)
    except ImportError:
        # Fallback: simple word-based splitting
        words = text.split()
        chunks = []
        i = 0
        while i < len(words):
            chunk_words = words[i : i + chunk_size]
            chunks.append(" ".join(chunk_words))
            i += chunk_size - overlap
    return [(c, None, None) for c in chunks if c.strip()]


async def node_chunk(state: IngestState, config: dict[str, Any]) -> dict[str, Any]:
    """Split extracted text into overlapping chunks with deterministic point IDs.

    Args:
        state: Must have extracted_text and optionally extracted_sections.
        config: RunnableConfig with configurable["db"].

    Returns:
        {"chunks": list[ChunkData]}

    Raises:
        IngestNodeError: If no text available or chunking fails.
    """
    cfg = config.get("configurable", {})
    session: AsyncSession = cfg["db"]
    step_start = utcnow()

    try:
        if not state.extracted_text:
            raise IngestNodeError("extracted_text is empty — node_extract must run first")

        collection = await get_collection(session, state.collection_id)
        chunk_config = collection.chunk_config or {}
        strategy = chunk_config.get("strategy", "recursive")
        chunk_size = chunk_config.get("chunk_size", 512)
        overlap = chunk_config.get("overlap", 64)

        # Per-document-type size overrides
        doc_type = state.validation_result.document_type if state.validation_result else None
        overrides = chunk_config.get("document_type_overrides", {})
        if doc_type and doc_type in overrides:
            chunk_size = overrides[doc_type].get("chunk_size", chunk_size)
            overlap = overrides[doc_type].get("overlap", overlap)

        if strategy == "by_section" and state.extracted_sections:
            raw_chunks = _chunk_by_section(state.extracted_sections, chunk_size, overlap)
        else:
            raw_chunks = _chunk_recursive(state.extracted_text, chunk_size, overlap)

        chunks: list[ChunkData] = []
        for idx, (text, page, section) in enumerate(raw_chunks):
            chunks.append(
                ChunkData(
                    chunk_index=idx,
                    text=text,
                    page=page,
                    section=section,
                    token_count=_token_count(text),
                    point_id=_make_point_id(state.document_id, idx),
                )
            )

        if not chunks:
            raise IngestNodeError("chunking produced 0 chunks — document may be empty")

        total_tokens = sum(c.token_count for c in chunks)
        avg_tokens = total_tokens // len(chunks)

        await update_step(
            session,
            state.job_id,
            stage="chunk",
            status="completed",
            started_at=step_start,
            meta={
                "strategy": strategy,
                "chunk_count": len(chunks),
                "avg_token_count": avg_tokens,
                "latency_ms": _elapsed_ms(step_start),
            },
        )
        logger.info(
            "node_chunk_completed",
            document_id=str(state.document_id),
            chunk_count=len(chunks),
            strategy=strategy,
        )
        return {"chunks": chunks}

    except IngestNodeError as exc:
        await update_step(
            session,
            state.job_id,
            stage="chunk",
            status="failed",
            started_at=step_start,
            error=str(exc),
        )
        raise
    except Exception as exc:
        error_msg = f"chunk_error: {type(exc).__name__}"
        await update_step(
            session,
            state.job_id,
            stage="chunk",
            status="failed",
            started_at=step_start,
            error=error_msg,
        )
        raise IngestNodeError(error_msg) from exc


def _elapsed_ms(start: datetime) -> int:
    return int((datetime.now(UTC) - start).total_seconds() * 1000)

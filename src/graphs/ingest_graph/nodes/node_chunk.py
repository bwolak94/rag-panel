"""node_chunk — split extracted text into overlapping chunks with deterministic point IDs.

Strategy is read from collections.chunk_config JSONB.  Supported keys:

  "strategy": "recursive" | "section_aware" | "sentence" | "row"
      Collection-level default strategy. Falls back to "recursive" when absent.

  "strategy_by_category": {"<category>": "<strategy>", ...}
      Per-document-category override; takes precedence over "strategy".
      Category comes from validation_result.category (classify_intent step).
      Example: {"lab_results": "row", "clinical_note": "sentence",
                 "medical_guideline": "section_aware"}

  "chunk_size": int   — token count per chunk (default 512, applies to recursive /
                         section_aware strategies)
  "overlap": int      — token overlap between chunks (default 64)
  "min_chunk_size": int — chunks below this token count are dropped (default 64)

  "sentences_per_chunk": int — for sentence strategy (default 4)
  "overlap_sentences":  int — sentence overlap (default 1)
  "rows_per_chunk":     int — for row strategy (default 10)

  "document_type_overrides": {"<doc_type>": {"chunk_size": int, "overlap": int}}
      Overrides chunk_size/overlap for a specific document_type value.

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
from langfuse import observe
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import IngestNodeError
from src.core.langfuse_client import update_span_metadata as _lf_update_span
from src.graphs.ingest_graph.chunking import (
    ChunkStrategy,
    chunk_recursive,
    chunk_row,
    chunk_section_aware,
    chunk_sentence,
    select_strategy,
)
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


def _elapsed_ms(start: datetime) -> int:
    return int((datetime.now(UTC) - start).total_seconds() * 1000)


@observe(name="node_chunk", capture_input=False, capture_output=False)
async def node_chunk(state: IngestState, config: dict[str, Any]) -> dict[str, Any]:
    """Split extracted text into overlapping chunks with deterministic point IDs.

    Reads chunk_config from the collection record.  Dispatches to the appropriate
    chunking strategy (recursive / section_aware / sentence / row) based on:
      1. chunk_config["strategy_by_category"][validation_result.category]
      2. chunk_config["strategy"]
      3. built-in default "recursive"

    Args:
        state: Must have extracted_text and optionally extracted_sections /
               validation_result populated by earlier nodes.
        config: RunnableConfig with configurable["db"] holding an AsyncSession.

    Returns:
        {"chunks": list[ChunkData]}

    Raises:
        IngestNodeError: If no text available, 0 chunks produced, or any error.
    """
    cfg = config.get("configurable", {})
    session: AsyncSession = cfg["db"]
    step_start = utcnow()

    try:
        if not state.extracted_text:
            raise IngestNodeError("extracted_text is empty — node_extract must run first")

        collection = await get_collection(session, state.collection_id)
        chunk_config: dict[str, Any] = collection.chunk_config or {}

        # --- resolve strategy ---
        category: str | None = state.validation_result.category if state.validation_result else None
        strategy = select_strategy(chunk_config, category)

        # --- resolve size parameters ---
        chunk_size: int = chunk_config.get("chunk_size", 512)
        overlap: int = chunk_config.get("overlap", 64)

        # Per-document-type size overrides (unchanged from previous version)
        doc_type: str | None = (
            state.validation_result.document_type if state.validation_result else None
        )
        overrides: dict[str, Any] = chunk_config.get("document_type_overrides", {})
        if doc_type and doc_type in overrides:
            chunk_size = overrides[doc_type].get("chunk_size", chunk_size)
            overlap = overrides[doc_type].get("overlap", overlap)

        sentences_per_chunk: int = chunk_config.get("sentences_per_chunk", 4)
        overlap_sentences: int = chunk_config.get("overlap_sentences", 1)
        rows_per_chunk: int = chunk_config.get("rows_per_chunk", 10)

        logger.info(
            "node_chunk_strategy_selected",
            document_id=str(state.document_id),
            strategy=strategy.value,
            category=category,
        )

        # --- dispatch ---
        sections: list[Section] | None = state.extracted_sections
        text: str = state.extracted_text

        if strategy == ChunkStrategy.SECTION_AWARE:
            raw_chunks = chunk_section_aware(sections, text, chunk_size, overlap)
        elif strategy == ChunkStrategy.SENTENCE:
            raw_chunks = chunk_sentence(text, sentences_per_chunk, overlap_sentences)
        elif strategy == ChunkStrategy.ROW:
            raw_chunks = chunk_row(text, rows_per_chunk)
        else:
            # RECURSIVE (default) and any unrecognised values
            raw_chunks = chunk_recursive(text, chunk_size, overlap)

        # --- assemble ChunkData objects ---
        chunks: list[ChunkData] = []
        for idx, (chunk_text, page, section) in enumerate(raw_chunks):
            chunks.append(
                ChunkData(
                    chunk_index=idx,
                    text=chunk_text,
                    page=page,
                    section=section,
                    token_count=_token_count(chunk_text),
                    point_id=_make_point_id(state.document_id, idx),
                )
            )

        if not chunks:
            raise IngestNodeError("chunking produced 0 chunks — document may be empty")

        total_tokens = sum(c.token_count for c in chunks)
        avg_tokens = total_tokens // len(chunks)

        elapsed = _elapsed_ms(step_start)
        await update_step(
            session,
            state.job_id,
            stage="chunk",
            status="completed",
            started_at=step_start,
            meta={
                "strategy": strategy.value,
                "category": category,
                "chunk_count": len(chunks),
                "avg_token_count": avg_tokens,
                "latency_ms": elapsed,
            },
        )
        _lf_update_span(
            metadata={
                "document_id": str(state.document_id),
                "tenant_id": str(state.tenant_id),
                "strategy": strategy.value,
                "chunk_count": len(chunks),
                "total_tokens": total_tokens,
                "avg_token_count": avg_tokens,
                "latency_ms": elapsed,
            }
        )
        logger.info(
            "node_chunk_completed",
            document_id=str(state.document_id),
            chunk_count=len(chunks),
            strategy=strategy.value,
        )
        return {"chunks": chunks}

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
            stage="chunk",
            status="failed",
            started_at=step_start,
            error=str(exc),
        )
        raise
    except Exception as exc:
        error_msg = f"chunk_error: {type(exc).__name__}"
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
            stage="chunk",
            status="failed",
            started_at=step_start,
            error=error_msg,
        )
        raise IngestNodeError(error_msg) from exc

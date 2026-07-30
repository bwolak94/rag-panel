"""Adaptive chunking strategies for the ingest pipeline.

Each strategy is a pure function that takes text/sections and config parameters,
and returns a list of (text, page, section_heading) tuples for downstream assembly.

Strategy selection order in node_chunk:
  1. chunk_config["strategy_by_category"][validation_result.category]  (most specific)
  2. chunk_config["strategy"]                                           (collection default)
  3. "recursive"                                                         (built-in fallback)

Security: chunk text is untrusted data — never executed, never logged as content.
"""

from __future__ import annotations

import enum
import re
from typing import Any

from src.graphs.ingest_graph.state import Section


class ChunkStrategy(enum.StrEnum):
    """Supported chunking strategies.

    RECURSIVE       — RecursiveCharacterTextSplitter; default for any document type.
    SECTION_AWARE   — Split on Section objects first (Docling-extracted headings), then
                      apply recursive splitting within each section that still exceeds
                      chunk_size. Falls back to RECURSIVE when no sections are available.
    SENTENCE        — Split on sentence boundaries (.!?), group sentences_per_chunk
                      sentences per chunk with overlap_sentences overlap. Good for
                      clinical notes and short reports.
    ROW             — Split on newlines, group rows_per_chunk rows per chunk. Good for
                      lab results and tabular exports.
    """

    RECURSIVE = "recursive"
    SECTION_AWARE = "section_aware"
    SENTENCE = "sentence"
    ROW = "row"


# ---------------------------------------------------------------------------
# Internal type alias
# ---------------------------------------------------------------------------

# Each chunker returns a list of (text, page, section_heading) 3-tuples.
_RawChunk = tuple[str, int | None, str | None]


# ---------------------------------------------------------------------------
# Strategy implementations
# ---------------------------------------------------------------------------


def chunk_recursive(
    text: str,
    chunk_size: int = 512,
    chunk_overlap: int = 64,
) -> list[_RawChunk]:
    """Split text with RecursiveCharacterTextSplitter using token length counting.

    Args:
        text: Full document text.
        chunk_size: Maximum token count per chunk.
        chunk_overlap: Token overlap between consecutive chunks.

    Returns:
        List of (chunk_text, None, None) tuples.
    """
    try:
        from langchain_text_splitters import RecursiveCharacterTextSplitter

        from src.graphs.ingest_graph.nodes.node_chunk import _token_count

        splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            length_function=_token_count,
        )
        chunks = splitter.split_text(text)
    except ImportError:
        # Fallback: simple word-based splitting — same fallback as node_chunk
        words = text.split()
        chunks = []
        i = 0
        while i < len(words):
            chunks.append(" ".join(words[i : i + chunk_size]))
            i += chunk_size - chunk_overlap

    return [(c, None, None) for c in chunks if c.strip()]


def chunk_section_aware(
    sections: list[Section] | None,
    text: str,
    chunk_size: int = 512,
    chunk_overlap: int = 64,
) -> list[_RawChunk]:
    """One chunk per Section; large sections are split recursively within their boundary.

    When no sections are available the function falls back to ``chunk_recursive``.

    Args:
        sections: Section objects produced by node_extract (Docling). May be None.
        text: Full document text — used only if sections is None or empty.
        chunk_size: Maximum token count per chunk.
        chunk_overlap: Token overlap used when a section is split recursively.

    Returns:
        List of (chunk_text, page, section_heading) tuples.
    """
    if not sections:
        return chunk_recursive(text, chunk_size, chunk_overlap)

    try:
        from src.graphs.ingest_graph.nodes.node_chunk import _token_count
    except ImportError:

        def _token_count(t: str) -> int:  # type: ignore[misc]
            return len(t.split())

    result: list[_RawChunk] = []
    for section in sections:
        section_text = section.text.strip()
        if not section_text:
            continue
        if _token_count(section_text) <= chunk_size:
            result.append((section_text, section.page, section.heading))
        else:
            # Section exceeds chunk_size — split recursively, preserve metadata
            sub_chunks = chunk_recursive(section_text, chunk_size, chunk_overlap)
            for sub_text, _, _ in sub_chunks:
                result.append((sub_text, section.page, section.heading))

    return result


def chunk_sentence(
    text: str,
    sentences_per_chunk: int = 4,
    overlap_sentences: int = 1,
) -> list[_RawChunk]:
    """Split text into sentence groups.

    Sentences are split on ``.``, ``!``, ``?`` boundaries (with trailing space or
    end-of-string). Each chunk contains ``sentences_per_chunk`` sentences; consecutive
    chunks share ``overlap_sentences`` sentences at the boundary.

    Args:
        text: Full document text.
        sentences_per_chunk: Target number of sentences per chunk (3–5 recommended).
        overlap_sentences: Number of trailing sentences from the previous chunk to
            prepend to the next chunk (acts as context window).

    Returns:
        List of (chunk_text, None, None) tuples.
    """
    # Split preserving delimiter by using a lookahead
    raw_sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    sentences = [s.strip() for s in raw_sentences if s.strip()]

    if not sentences:
        return []

    result: list[_RawChunk] = []
    i = 0
    while i < len(sentences):
        window = sentences[i : i + sentences_per_chunk]
        chunk_text = " ".join(window)
        if chunk_text:
            result.append((chunk_text, None, None))
        i += sentences_per_chunk - overlap_sentences
        # Guard: always advance at least 1 sentence to avoid infinite loop
        if sentences_per_chunk <= overlap_sentences:
            i = i + 1 if window else len(sentences)

    return result


def chunk_row(
    text: str,
    rows_per_chunk: int = 10,
) -> list[_RawChunk]:
    """Split text on newlines, grouping rows_per_chunk lines per chunk.

    Designed for tabular content (lab result exports, CSV-like text, spreadsheets).
    Empty lines are filtered before grouping.

    Args:
        text: Full document text with newline-separated rows.
        rows_per_chunk: Number of non-empty lines per chunk.

    Returns:
        List of (chunk_text, None, None) tuples.
    """
    rows = [r.strip() for r in text.splitlines() if r.strip()]

    if not rows:
        return []

    result: list[_RawChunk] = []
    for i in range(0, len(rows), rows_per_chunk):
        group = rows[i : i + rows_per_chunk]
        chunk_text = "\n".join(group)
        if chunk_text:
            result.append((chunk_text, None, None))

    return result


# ---------------------------------------------------------------------------
# Public dispatch helper
# ---------------------------------------------------------------------------


def select_strategy(
    chunk_config: dict[str, Any],
    category: str | None,
) -> ChunkStrategy:
    """Resolve the effective ChunkStrategy for this document.

    Category-specific override in ``chunk_config["strategy_by_category"]`` takes
    precedence over the collection-level ``chunk_config["strategy"]`` default.

    Args:
        chunk_config: The collection's chunk_config JSONB dict.
        category: Document category from ValidationResult (may be None).

    Returns:
        The resolved ChunkStrategy enum member.
    """
    # Category override has highest priority
    by_category: dict[str, str] = chunk_config.get("strategy_by_category", {})
    if category and category in by_category:
        raw = by_category[category]
        try:
            return ChunkStrategy(raw)
        except ValueError:
            pass  # unknown value — fall through to collection default

    # Collection-level default
    raw_default: str = chunk_config.get("strategy", ChunkStrategy.RECURSIVE.value)
    try:
        return ChunkStrategy(raw_default)
    except ValueError:
        return ChunkStrategy.RECURSIVE

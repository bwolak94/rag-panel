"""Unit tests for node_chunk and the adaptive chunking strategies."""

from __future__ import annotations

import hashlib
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.graphs.ingest_graph.chunking import (
    ChunkStrategy,
    chunk_recursive,
    chunk_row,
    chunk_section_aware,
    chunk_sentence,
    select_strategy,
)
from src.graphs.ingest_graph.nodes.node_chunk import _make_point_id, node_chunk
from src.graphs.ingest_graph.state import IngestState, Section, ValidationResult

TENANT_ID = uuid.uuid4()
DOCUMENT_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()
JOB_ID = uuid.uuid4()

_LONG_TEXT = " ".join([f"word{i}" for i in range(2000)])

_SENTENCE_TEXT = (
    "Patient presents with chest pain. "
    "ECG shows sinus rhythm. "
    "Blood pressure is elevated at 160/100. "
    "Troponin levels are within normal range. "
    "Recommend cardiology referral. "
    "Follow-up in two weeks."
)

_ROW_TEXT = "\n".join([f"Row {i}: value{i}" for i in range(25)])


def _make_state(**kwargs: object) -> IngestState:
    defaults: dict[str, object] = {
        "extracted_text": _LONG_TEXT,
    }
    defaults.update(kwargs)
    return IngestState(
        document_id=DOCUMENT_ID,
        tenant_id=TENANT_ID,
        collection_id=COLLECTION_ID,
        minio_key=f"raw/{COLLECTION_ID}/{DOCUMENT_ID}/doc.pdf",
        job_id=JOB_ID,
        **defaults,  # type: ignore[arg-type]
    )


def _make_collection(config: dict) -> MagicMock:
    coll = MagicMock()
    coll.chunk_config = config
    return coll


def _make_config(session: object) -> dict:
    return {"configurable": {"db": session}}


def _make_session() -> AsyncMock:
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock())
    session.commit = AsyncMock()
    return session


# ---------------------------------------------------------------------------
# Existing tests preserved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recursive_strategy_produces_chunks() -> None:
    """Recursive strategy splits long text into multiple chunks."""
    session = _make_session()
    state = _make_state()
    config = {"strategy": "recursive", "chunk_size": 100, "overlap": 10}

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_chunk.get_collection",
            new=AsyncMock(return_value=_make_collection(config)),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_chunk.update_step",
            new=AsyncMock(),
        ),
    ):
        result = await node_chunk(state, _make_config(session))

    chunks = result["chunks"]
    assert len(chunks) > 1
    assert all(c.text for c in chunks)


@pytest.mark.asyncio
async def test_by_section_strategy_uses_sections() -> None:
    """section_aware strategy creates one chunk per section (no large-section split needed)."""
    session = _make_session()
    sections = [
        Section(heading=None, text=f"Section {i} text content here", page=i, section_index=i)
        for i in range(3)
    ]
    state = _make_state(extracted_sections=sections)
    config = {"strategy": "section_aware", "chunk_size": 512, "overlap": 64}

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_chunk.get_collection",
            new=AsyncMock(return_value=_make_collection(config)),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_chunk.update_step",
            new=AsyncMock(),
        ),
    ):
        result = await node_chunk(state, _make_config(session))

    assert len(result["chunks"]) == 3


@pytest.mark.asyncio
async def test_deterministic_point_ids() -> None:
    """Same document_id + chunk_index always produces the same point_id."""
    doc_id = uuid.uuid4()
    id_a = _make_point_id(doc_id, 0)
    id_b = _make_point_id(doc_id, 0)
    assert id_a == id_b


@pytest.mark.asyncio
async def test_point_id_is_sha256_derived() -> None:
    """point_id is derived from SHA-256, not UUID5 (SHA-1)."""
    doc_id = uuid.uuid4()
    point_id = _make_point_id(doc_id, 0)
    expected = uuid.UUID(hashlib.sha256(f"{doc_id}:0".encode()).hexdigest()[:32])
    assert point_id == expected


@pytest.mark.asyncio
async def test_chunk_index_increments() -> None:
    """chunk_index on each ChunkData increments from 0."""
    session = _make_session()
    state = _make_state()
    config = {"strategy": "recursive", "chunk_size": 50, "overlap": 5}

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_chunk.get_collection",
            new=AsyncMock(return_value=_make_collection(config)),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_chunk.update_step",
            new=AsyncMock(),
        ),
    ):
        result = await node_chunk(state, _make_config(session))

    chunks = result["chunks"]
    for i, chunk in enumerate(chunks):
        assert chunk.chunk_index == i


@pytest.mark.asyncio
async def test_document_type_override_applied() -> None:
    """chunk_config.document_type_overrides is applied for matching document_type."""
    session = _make_session()
    vr = ValidationResult(document_type="table", quality_score=0.9)
    state = _make_state(validation_result=vr)
    config = {
        "strategy": "recursive",
        "chunk_size": 512,
        "overlap": 64,
        "document_type_overrides": {"table": {"chunk_size": 50, "overlap": 5}},
    }

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_chunk.get_collection",
            new=AsyncMock(return_value=_make_collection(config)),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_chunk.update_step",
            new=AsyncMock(),
        ),
    ):
        result = await node_chunk(state, _make_config(session))

    # Smaller chunk_size=50 should produce more chunks than default 512
    assert len(result["chunks"]) > 1


# ---------------------------------------------------------------------------
# New adaptive strategy tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recursive_strategy_is_default() -> None:
    """When chunk_config has no 'strategy' key, recursive is selected."""
    session = _make_session()
    state = _make_state()
    # config intentionally omits "strategy"
    config: dict = {"chunk_size": 100, "overlap": 10}

    captured_strategy: list[str] = []

    async def capture_step(*args: object, **kwargs: object) -> None:
        meta = kwargs.get("meta") or {}
        if "strategy" in meta:
            captured_strategy.append(meta["strategy"])

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_chunk.get_collection",
            new=AsyncMock(return_value=_make_collection(config)),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_chunk.update_step",
            new=capture_step,
        ),
    ):
        result = await node_chunk(state, _make_config(session))

    assert result["chunks"]
    assert captured_strategy == [ChunkStrategy.RECURSIVE.value]


@pytest.mark.asyncio
async def test_section_aware_strategy_uses_sections() -> None:
    """section_aware strategy uses Section objects and preserves heading metadata."""
    session = _make_session()
    sections = [
        Section(
            heading=f"Heading {i}",
            text=f"This is the content of section {i} with enough text.",
            page=i,
            section_index=i,
        )
        for i in range(4)
    ]
    state = _make_state(extracted_sections=sections)
    config = {"strategy": "section_aware", "chunk_size": 512, "overlap": 64}

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_chunk.get_collection",
            new=AsyncMock(return_value=_make_collection(config)),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_chunk.update_step",
            new=AsyncMock(),
        ),
    ):
        result = await node_chunk(state, _make_config(session))

    chunks = result["chunks"]
    assert len(chunks) == 4
    # Heading metadata is preserved on each chunk
    for chunk in chunks:
        assert chunk.section is not None
        assert chunk.section.startswith("Heading")


@pytest.mark.asyncio
async def test_section_aware_falls_back_to_recursive_without_sections() -> None:
    """section_aware falls back to recursive when extracted_sections is None."""
    session = _make_session()
    # No sections provided — extracted_sections defaults to None
    state = _make_state()
    config = {"strategy": "section_aware", "chunk_size": 100, "overlap": 10}

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_chunk.get_collection",
            new=AsyncMock(return_value=_make_collection(config)),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_chunk.update_step",
            new=AsyncMock(),
        ),
    ):
        result = await node_chunk(state, _make_config(session))

    # Should still produce chunks via recursive fallback
    assert len(result["chunks"]) > 1


@pytest.mark.asyncio
async def test_sentence_strategy_chunks_by_sentence() -> None:
    """sentence strategy groups sentences per chunk and respects overlap."""
    session = _make_session()
    state = _make_state(extracted_text=_SENTENCE_TEXT)
    config = {
        "strategy": "sentence",
        "sentences_per_chunk": 3,
        "overlap_sentences": 1,
    }

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_chunk.get_collection",
            new=AsyncMock(return_value=_make_collection(config)),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_chunk.update_step",
            new=AsyncMock(),
        ),
    ):
        result = await node_chunk(state, _make_config(session))

    chunks = result["chunks"]
    assert len(chunks) >= 2
    # Each chunk should contain sentence-level text (no lone words)
    for chunk in chunks:
        assert len(chunk.text) > 0


@pytest.mark.asyncio
async def test_row_strategy_chunks_by_row() -> None:
    """row strategy groups N lines per chunk."""
    session = _make_session()
    state = _make_state(extracted_text=_ROW_TEXT)
    config = {"strategy": "row", "rows_per_chunk": 10}

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_chunk.get_collection",
            new=AsyncMock(return_value=_make_collection(config)),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_chunk.update_step",
            new=AsyncMock(),
        ),
    ):
        result = await node_chunk(state, _make_config(session))

    chunks = result["chunks"]
    # 25 rows / 10 per chunk → 3 chunks (10 + 10 + 5)
    assert len(chunks) == 3
    # First chunk has exactly 10 rows
    assert chunks[0].text.count("\n") == 9  # 10 rows = 9 newlines


@pytest.mark.asyncio
async def test_category_override_takes_precedence_over_default_strategy() -> None:
    """strategy_by_category overrides the collection-level strategy."""
    session = _make_session()
    vr = ValidationResult(category="lab_results", quality_score=0.95)
    state = _make_state(extracted_text=_ROW_TEXT, validation_result=vr)
    config = {
        "strategy": "recursive",  # collection default — should NOT be used
        "strategy_by_category": {"lab_results": "row"},
        "rows_per_chunk": 10,
    }

    captured_strategy: list[str] = []

    async def capture_step(*args: object, **kwargs: object) -> None:
        meta = kwargs.get("meta") or {}
        if "strategy" in meta:
            captured_strategy.append(meta["strategy"])

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_chunk.get_collection",
            new=AsyncMock(return_value=_make_collection(config)),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_chunk.update_step",
            new=capture_step,
        ),
    ):
        result = await node_chunk(state, _make_config(session))

    # row strategy was used (not recursive)
    assert captured_strategy == [ChunkStrategy.ROW.value]
    # row output: 25 rows / 10 per chunk → 3 chunks
    assert len(result["chunks"]) == 3


# ---------------------------------------------------------------------------
# Unit tests for chunking.py pure functions
# ---------------------------------------------------------------------------


def test_select_strategy_returns_recursive_by_default() -> None:
    strategy = select_strategy({}, category=None)
    assert strategy == ChunkStrategy.RECURSIVE


def test_select_strategy_respects_collection_default() -> None:
    strategy = select_strategy({"strategy": "sentence"}, category=None)
    assert strategy == ChunkStrategy.SENTENCE


def test_select_strategy_category_override_wins() -> None:
    config = {
        "strategy": "recursive",
        "strategy_by_category": {"clinical_note": "sentence"},
    }
    strategy = select_strategy(config, category="clinical_note")
    assert strategy == ChunkStrategy.SENTENCE


def test_select_strategy_unknown_category_falls_back_to_collection_default() -> None:
    config = {
        "strategy": "row",
        "strategy_by_category": {"clinical_note": "sentence"},
    }
    strategy = select_strategy(config, category="unknown_type")
    assert strategy == ChunkStrategy.ROW


def test_select_strategy_unknown_value_falls_back_to_recursive() -> None:
    strategy = select_strategy({"strategy": "nonexistent_strategy"}, category=None)
    assert strategy == ChunkStrategy.RECURSIVE


def test_chunk_recursive_produces_multiple_chunks_for_long_text() -> None:
    chunks = chunk_recursive(_LONG_TEXT, chunk_size=100, chunk_overlap=10)
    assert len(chunks) > 1
    for text, page, section in chunks:
        assert text.strip()
        assert page is None
        assert section is None


def test_chunk_section_aware_preserves_metadata() -> None:
    sections = [
        Section(heading="Intro", text="Introduction text here.", page=1, section_index=0),
        Section(heading="Method", text="Method description goes here.", page=2, section_index=1),
    ]
    chunks = chunk_section_aware(sections, text="", chunk_size=512, chunk_overlap=64)
    assert len(chunks) == 2
    assert chunks[0][2] == "Intro"
    assert chunks[0][1] == 1
    assert chunks[1][2] == "Method"
    assert chunks[1][1] == 2


def test_chunk_section_aware_falls_back_when_no_sections() -> None:
    chunks = chunk_section_aware(None, text=_LONG_TEXT, chunk_size=100, chunk_overlap=10)
    assert len(chunks) > 1
    for _, page, section in chunks:
        assert page is None
        assert section is None


def test_chunk_sentence_groups_sentences() -> None:
    chunks = chunk_sentence(_SENTENCE_TEXT, sentences_per_chunk=3, overlap_sentences=1)
    assert len(chunks) >= 2
    for text, page, section in chunks:
        assert text.strip()
        assert page is None
        assert section is None


def test_chunk_sentence_single_sentence_text() -> None:
    chunks = chunk_sentence("Only one sentence here.", sentences_per_chunk=3, overlap_sentences=1)
    assert len(chunks) == 1
    assert chunks[0][0] == "Only one sentence here."


def test_chunk_row_groups_rows() -> None:
    chunks = chunk_row(_ROW_TEXT, rows_per_chunk=10)
    assert len(chunks) == 3
    assert chunks[0][0].count("\n") == 9  # 10 rows → 9 newlines


def test_chunk_row_single_chunk_when_rows_fit() -> None:
    short_text = "Row 1\nRow 2\nRow 3"
    chunks = chunk_row(short_text, rows_per_chunk=10)
    assert len(chunks) == 1
    assert "Row 1" in chunks[0][0]


def test_chunk_row_skips_empty_lines() -> None:
    text_with_blanks = "Row A\n\n\nRow B\n\nRow C"
    chunks = chunk_row(text_with_blanks, rows_per_chunk=10)
    assert len(chunks) == 1
    assert "Row A" in chunks[0][0]
    assert "Row B" in chunks[0][0]
    assert "Row C" in chunks[0][0]

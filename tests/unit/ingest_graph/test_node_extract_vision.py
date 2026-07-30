"""Unit tests for node_extract_vision.

Coverage:
- test_vision_extraction_disabled_skips_node
- test_vision_extraction_with_mocked_llm_produces_chunks
- test_vision_extraction_failure_is_nonfatal
- test_vision_chunks_appended_to_existing_chunks
- test_no_image_sections_returns_unchanged_state
- test_no_pii_in_langfuse_logs
- test_vision_model_id_from_chunk_config_used
- test_text_sections_skipped
- test_section_without_image_b64_skipped
- test_parse_vision_response_invalid_content_type_defaults_to_image
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.graphs.ingest_graph.nodes.node_extract_vision import (
    _parse_vision_response,
    node_extract_vision,
)
from src.graphs.ingest_graph.state import ChunkData, IngestState, Section

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TENANT_ID = uuid.uuid4()
DOCUMENT_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()
JOB_ID = uuid.uuid4()

_FAKE_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_point_id(idx: int) -> uuid.UUID:
    digest = hashlib.sha256(f"{DOCUMENT_ID}:{idx}".encode()).hexdigest()
    return uuid.UUID(digest[:32])


def _make_chunk(idx: int = 0) -> ChunkData:
    return ChunkData(
        chunk_index=idx,
        text=f"Text chunk {idx}.",
        page=idx + 1,
        section=None,
        token_count=5,
        point_id=_make_point_id(idx),
    )


def _make_image_section(section_index: int = 0) -> Section:
    return Section(
        heading="Figure 1",
        text="",
        page=2,
        section_index=section_index,
        section_type="image",
        image_b64=_FAKE_B64,
    )


def _make_table_section(section_index: int = 1) -> Section:
    return Section(
        heading="Table 1 — Lab results",
        text="",
        page=3,
        section_index=section_index,
        section_type="table",
        image_b64=_FAKE_B64,
    )


def _make_text_section(section_index: int = 2) -> Section:
    return Section(
        heading=None,
        text="Patient presented with chest pain.",
        page=1,
        section_index=section_index,
        section_type="text",
        image_b64=None,
    )


def _make_state(**kwargs: Any) -> IngestState:
    defaults: dict[str, Any] = {
        "document_id": DOCUMENT_ID,
        "tenant_id": TENANT_ID,
        "collection_id": COLLECTION_ID,
        "minio_key": f"raw/{COLLECTION_ID}/{DOCUMENT_ID}/doc.pdf",
        "job_id": JOB_ID,
        "chunks": [_make_chunk(0)],
        "extracted_sections": [_make_image_section(0)],
    }
    defaults.update(kwargs)
    return IngestState(**defaults)


def _make_session() -> AsyncMock:
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock())
    session.commit = AsyncMock()
    session.flush = AsyncMock()
    return session


def _make_collection(
    vision_extraction_enabled: bool = True,
    vision_model_id: str | None = None,
) -> MagicMock:
    collection = MagicMock()
    cfg: dict[str, Any] = {"vision_extraction_enabled": vision_extraction_enabled}
    if vision_model_id is not None:
        cfg["vision_model_id"] = vision_model_id
    collection.chunk_config = cfg
    collection.embedding_model_id = uuid.uuid4()
    return collection


def _make_model(model_id: str = "llava:13b") -> MagicMock:
    model = MagicMock()
    model.model_id = model_id
    model.endpoint_url = "http://ollama:11434/v1"
    return model


def _make_llm_response(description: str, content_type: str = "image") -> MagicMock:
    """Build a mock LLM response containing a valid vision JSON payload."""
    payload = json.dumps(
        {
            "description": description,
            "content_type": content_type,
            "key_values": ["HR 72 bpm", "BP 120/80 mmHg"],
        }
    )
    msg = MagicMock()
    msg.content = payload
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


def _make_config(session: Any, llm: Any) -> dict[str, Any]:
    return {"configurable": {"db": session, "llm": llm}}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vision_extraction_disabled_skips_node() -> None:
    """When vision_extraction_enabled=False, the node returns unchanged state without LLM calls."""
    session = _make_session()
    llm = AsyncMock()
    llm.chat_completion = AsyncMock()

    collection = _make_collection(vision_extraction_enabled=False)

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_vision.get_collection",
            new=AsyncMock(return_value=collection),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_vision.update_step",
            new=AsyncMock(),
        ),
    ):
        state = _make_state()
        result = await node_extract_vision(state, _make_config(session, llm))

    assert result["vision_chunks_count"] == 0
    assert result["chunks"] == state.chunks
    llm.chat_completion.assert_not_called()


@pytest.mark.asyncio
async def test_vision_extraction_with_mocked_llm_produces_chunks() -> None:
    """The node appends a ChunkData for each visual section when the LLM succeeds."""
    session = _make_session()
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(
        return_value=_make_llm_response("Chest X-ray showing bilateral infiltrates.")
    )

    collection = _make_collection(vision_extraction_enabled=True)
    model = _make_model()
    original_chunks = [_make_chunk(0)]

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_vision.get_collection",
            new=AsyncMock(return_value=collection),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_vision.get_model",
            new=AsyncMock(return_value=model),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_vision.update_step",
            new=AsyncMock(),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_vision._load_prompt",
            return_value="You are a medical image analyst.",
        ),
    ):
        state = _make_state(chunks=original_chunks, extracted_sections=[_make_image_section(0)])
        result = await node_extract_vision(state, _make_config(session, llm))

    assert result["vision_chunks_count"] == 1
    chunks = result["chunks"]
    # Original chunk + 1 new vision chunk
    assert len(chunks) == 2
    vision_chunk = chunks[1]
    assert isinstance(vision_chunk, ChunkData)
    assert "IMAGE" in vision_chunk.text or "image" in vision_chunk.text.lower()
    assert "Chest X-ray" in vision_chunk.text
    # Key values are appended
    assert "HR 72 bpm" in vision_chunk.text


@pytest.mark.asyncio
async def test_vision_extraction_failure_is_nonfatal() -> None:
    """When the LLM raises for all sections, the node returns unchanged chunks without raising."""
    session = _make_session()
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(side_effect=RuntimeError("vision model offline"))

    collection = _make_collection(vision_extraction_enabled=True)
    model = _make_model()
    original_chunks = [_make_chunk(0), _make_chunk(1)]

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_vision.get_collection",
            new=AsyncMock(return_value=collection),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_vision.get_model",
            new=AsyncMock(return_value=model),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_vision.update_step",
            new=AsyncMock(),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_vision._load_prompt",
            return_value="prompt",
        ),
    ):
        state = _make_state(
            chunks=original_chunks,
            extracted_sections=[_make_image_section(0), _make_table_section(1)],
        )
        # Must NOT raise — vision extraction is non-fatal enrichment
        result = await node_extract_vision(state, _make_config(session, llm))

    assert result["vision_chunks_count"] == 0
    # Original chunks must be returned unchanged
    assert result["chunks"] == original_chunks


@pytest.mark.asyncio
async def test_vision_chunks_appended_to_existing_chunks() -> None:
    """Vision chunks are appended AFTER existing text chunks; indices are contiguous."""
    session = _make_session()
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(
        return_value=_make_llm_response("MRI sagittal brain showing lesion.", "image")
    )

    collection = _make_collection(vision_extraction_enabled=True)
    model = _make_model()
    existing = [_make_chunk(0), _make_chunk(1), _make_chunk(2)]

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_vision.get_collection",
            new=AsyncMock(return_value=collection),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_vision.get_model",
            new=AsyncMock(return_value=model),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_vision.update_step",
            new=AsyncMock(),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_vision._load_prompt",
            return_value="prompt",
        ),
    ):
        state = _make_state(chunks=existing, extracted_sections=[_make_image_section(0)])
        result = await node_extract_vision(state, _make_config(session, llm))

    chunks = result["chunks"]
    assert len(chunks) == 4
    # First 3 chunks are the originals (unchanged)
    for i, original in enumerate(existing):
        assert chunks[i].chunk_index == original.chunk_index
    # The vision chunk is appended at index 3
    assert chunks[3].chunk_index == 3


@pytest.mark.asyncio
async def test_no_image_sections_returns_unchanged_state() -> None:
    """When extracted_sections has no visual sections with image_b64, no LLM call is made."""
    session = _make_session()
    llm = AsyncMock()
    llm.chat_completion = AsyncMock()

    collection = _make_collection(vision_extraction_enabled=True)
    model = _make_model()
    original_chunks = [_make_chunk(0)]

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_vision.get_collection",
            new=AsyncMock(return_value=collection),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_vision.get_model",
            new=AsyncMock(return_value=model),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_vision.update_step",
            new=AsyncMock(),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_vision._load_prompt",
            return_value="prompt",
        ),
    ):
        # Only text sections — no images
        state = _make_state(
            chunks=original_chunks,
            extracted_sections=[_make_text_section(0)],
        )
        result = await node_extract_vision(state, _make_config(session, llm))

    assert result["vision_chunks_count"] == 0
    assert result["chunks"] == original_chunks
    llm.chat_completion.assert_not_called()


@pytest.mark.asyncio
async def test_no_pii_in_langfuse_logs(caplog: pytest.LogCaptureFixture) -> None:
    """Image base64 data and clinical descriptions must not appear in log output."""
    session = _make_session()
    llm = AsyncMock()

    sensitive_description = "X-ray of patient John Doe shows pneumonia."
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(sensitive_description, "image"))

    collection = _make_collection(vision_extraction_enabled=True)
    model = _make_model()

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_vision.get_collection",
            new=AsyncMock(return_value=collection),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_vision.get_model",
            new=AsyncMock(return_value=model),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_vision.update_step",
            new=AsyncMock(),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_vision._load_prompt",
            return_value="prompt",
        ),
        caplog.at_level(logging.DEBUG),
    ):
        state = _make_state(extracted_sections=[_make_image_section(0)])
        await node_extract_vision(state, _make_config(session, llm))

    # The base64 image data must not appear in any log record
    for record in caplog.records:
        assert _FAKE_B64 not in record.getMessage(), (
            f"image_b64 leaked into log: {record.getMessage()!r}"
        )
    # Patient name from LLM response must not appear in logs
    # (LLM response content is not logged by the node — only counts/IDs are)
    assert "John Doe" not in caplog.text
    # The raw description string is embedded in chunk text — but chunk text must not be logged
    assert sensitive_description not in caplog.text


@pytest.mark.asyncio
async def test_vision_model_id_from_chunk_config_used() -> None:
    """When chunk_config.vision_model_id is set, get_model is called with that UUID."""
    vision_model_uuid = uuid.uuid4()
    session = _make_session()
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(
        return_value=_make_llm_response("Fundus photograph of the retina.")
    )

    collection = _make_collection(
        vision_extraction_enabled=True,
        vision_model_id=str(vision_model_uuid),
    )
    model = _make_model("llava:34b")

    mock_get_model = AsyncMock(return_value=model)

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_vision.get_collection",
            new=AsyncMock(return_value=collection),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_vision.get_model",
            new=mock_get_model,
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_vision.update_step",
            new=AsyncMock(),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_vision._load_prompt",
            return_value="prompt",
        ),
    ):
        state = _make_state(extracted_sections=[_make_image_section(0)])
        await node_extract_vision(state, _make_config(session, llm))

    # get_model must have been called with the UUID from chunk_config
    mock_get_model.assert_called_once_with(session, vision_model_uuid)


@pytest.mark.asyncio
async def test_text_sections_skipped() -> None:
    """Text sections (section_type='text') are ignored even when vision is enabled."""
    session = _make_session()
    llm = AsyncMock()
    llm.chat_completion = AsyncMock()

    collection = _make_collection(vision_extraction_enabled=True)
    model = _make_model()
    original_chunks = [_make_chunk(0)]

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_vision.get_collection",
            new=AsyncMock(return_value=collection),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_vision.get_model",
            new=AsyncMock(return_value=model),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_vision.update_step",
            new=AsyncMock(),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_vision._load_prompt",
            return_value="prompt",
        ),
    ):
        state = _make_state(
            chunks=original_chunks,
            extracted_sections=[
                _make_text_section(0),
                _make_text_section(1),
            ],
        )
        result = await node_extract_vision(state, _make_config(session, llm))

    assert result["vision_chunks_count"] == 0
    llm.chat_completion.assert_not_called()


@pytest.mark.asyncio
async def test_section_without_image_b64_skipped() -> None:
    """A section with section_type='image' but no image_b64 is silently skipped."""
    session = _make_session()
    llm = AsyncMock()
    llm.chat_completion = AsyncMock()

    collection = _make_collection(vision_extraction_enabled=True)
    model = _make_model()
    original_chunks = [_make_chunk(0)]

    # image section that has no base64 data (e.g. not yet extracted by Docling)
    image_no_data = Section(
        heading="Figure 2",
        text="",
        page=4,
        section_index=0,
        section_type="image",
        image_b64=None,  # no data available
    )

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_vision.get_collection",
            new=AsyncMock(return_value=collection),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_vision.get_model",
            new=AsyncMock(return_value=model),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_vision.update_step",
            new=AsyncMock(),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_vision._load_prompt",
            return_value="prompt",
        ),
    ):
        state = _make_state(chunks=original_chunks, extracted_sections=[image_no_data])
        result = await node_extract_vision(state, _make_config(session, llm))

    assert result["vision_chunks_count"] == 0
    assert result["chunks"] == original_chunks
    llm.chat_completion.assert_not_called()


# ---------------------------------------------------------------------------
# Unit tests for _parse_vision_response (pure function)
# ---------------------------------------------------------------------------


def test_parse_vision_response_valid() -> None:
    """Valid JSON with all fields is parsed correctly."""
    raw = json.dumps(
        {
            "description": "Chest CT showing ground-glass opacities.",
            "content_type": "image",
            "key_values": ["HU -650", "lesion diameter 1.5 cm"],
        }
    )
    result = _parse_vision_response(raw)
    assert result is not None
    assert result["description"] == "Chest CT showing ground-glass opacities."
    assert result["content_type"] == "image"
    assert "HU -650" in result["key_values"]


def test_parse_vision_response_invalid_content_type_defaults_to_image() -> None:
    """An unrecognized content_type is silently replaced with 'image'."""
    raw = json.dumps(
        {
            "description": "Some medical figure.",
            "content_type": "photograph",  # not in _VALID_CONTENT_TYPES
            "key_values": [],
        }
    )
    result = _parse_vision_response(raw)
    assert result is not None
    assert result["content_type"] == "image"


def test_parse_vision_response_invalid_json_returns_none() -> None:
    """Unparseable JSON returns None."""
    result = _parse_vision_response("not json at all")
    assert result is None


def test_parse_vision_response_missing_description_returns_none() -> None:
    """A response without a non-empty description is rejected."""
    raw = json.dumps({"content_type": "table", "key_values": []})
    result = _parse_vision_response(raw)
    assert result is None


def test_parse_vision_response_empty_description_returns_none() -> None:
    """A response with an empty description string is rejected."""
    raw = json.dumps({"description": "   ", "content_type": "image", "key_values": []})
    result = _parse_vision_response(raw)
    assert result is None


def test_parse_vision_response_missing_key_values_defaults_to_empty_list() -> None:
    """When key_values is absent, an empty list is returned."""
    raw = json.dumps({"description": "Brain MRI.", "content_type": "diagram"})
    result = _parse_vision_response(raw)
    assert result is not None
    assert result["key_values"] == []

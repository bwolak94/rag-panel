"""Unit tests for node_validate."""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.exceptions import IngestNodeError
from src.graphs.ingest_graph.nodes.node_validate import node_validate
from src.graphs.ingest_graph.state import IngestState, ValidationResult

TENANT_ID = uuid.uuid4()
DOCUMENT_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()
JOB_ID = uuid.uuid4()
EMBEDDING_MODEL_ID = uuid.uuid4()


def _make_state(
    extracted_text: str = "This is a sample document for validation.",
    **kwargs: object,
) -> IngestState:
    return IngestState(
        document_id=DOCUMENT_ID,
        tenant_id=TENANT_ID,
        collection_id=COLLECTION_ID,
        minio_key=f"raw/{COLLECTION_ID}/{DOCUMENT_ID}/test.pdf",
        job_id=JOB_ID,
        extracted_text=extracted_text,
        **kwargs,
    )


def _make_config(session: object, llm: object) -> dict:
    return {"configurable": {"db": session, "llm": llm}}


def _make_session() -> AsyncMock:
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    return session


def _make_collection(embedding_model_id: uuid.UUID = EMBEDDING_MODEL_ID) -> MagicMock:
    collection = MagicMock()
    collection.embedding_model_id = embedding_model_id
    collection.validation_config = {}
    return collection


def _make_model_record(
    model_id: str = "llama3.1", endpoint_url: str = "http://ollama:11434"
) -> MagicMock:
    model = MagicMock()
    model.model_id = model_id
    model.endpoint_url = endpoint_url
    return model


def _make_llm_response(payload: dict) -> MagicMock:
    message = MagicMock()
    message.content = json.dumps(payload)
    choice = MagicMock()
    choice.message = message
    response = MagicMock()
    response.choices = [choice]
    return response


@pytest.mark.asyncio
async def test_populates_validation_result() -> None:
    """LLM returns valid JSON → validation_result populated with category/quality/confidence."""
    session = _make_session()
    llm = MagicMock()
    llm.chat_completion = AsyncMock(
        return_value=_make_llm_response(
            {
                "category": "medical",
                "quality_score": 0.85,
                "confidence": 0.9,
                "document_type": "report",
                "language": "en",
            }
        )
    )
    state = _make_state()

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_validate.get_collection",
            new=AsyncMock(return_value=_make_collection()),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_validate.get_model",
            new=AsyncMock(return_value=_make_model_record()),
        ),
        patch("src.graphs.ingest_graph.nodes.node_validate.update_step", new=AsyncMock()),
        patch(
            "src.graphs.ingest_graph.nodes.node_validate._load_prompt",
            return_value="You are a document classifier.",
        ),
    ):
        result = await node_validate(state, _make_config(session, llm))

    vr: ValidationResult = result["validation_result"]
    assert vr.category == "medical"
    assert vr.quality_score == pytest.approx(0.85)
    assert vr.confidence == pytest.approx(0.9)
    assert vr.document_type == "report"
    assert vr.language == "en"


@pytest.mark.asyncio
async def test_low_quality_score_sets_needs_review() -> None:
    """quality_score < 0.3 → status set to needs_review and halt=True."""
    session = _make_session()
    llm = MagicMock()
    llm.chat_completion = AsyncMock(
        return_value=_make_llm_response(
            {
                "category": "unknown",
                "quality_score": 0.1,
                "confidence": 0.5,
            }
        )
    )
    state = _make_state()

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_validate.get_collection",
            new=AsyncMock(return_value=_make_collection()),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_validate.get_model",
            new=AsyncMock(return_value=_make_model_record()),
        ),
        patch("src.graphs.ingest_graph.nodes.node_validate.update_step", new=AsyncMock()),
        patch(
            "src.graphs.ingest_graph.nodes.node_validate._load_prompt",
            return_value="You are a document classifier.",
        ),
    ):
        result = await node_validate(state, _make_config(session, llm))

    assert result["status"] == "needs_review"
    assert result["halt"] is True
    assert result["validation_result"].quality_score == pytest.approx(0.1)


@pytest.mark.asyncio
async def test_llm_timeout_raises_ingest_node_error() -> None:
    """LLM raises TimeoutError → IngestNodeError raised."""
    session = _make_session()
    llm = MagicMock()
    llm.chat_completion = AsyncMock(side_effect=TimeoutError("LLM timed out"))
    state = _make_state()

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_validate.get_collection",
            new=AsyncMock(return_value=_make_collection()),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_validate.get_model",
            new=AsyncMock(return_value=_make_model_record()),
        ),
        patch("src.graphs.ingest_graph.nodes.node_validate.update_step", new=AsyncMock()),
        patch(
            "src.graphs.ingest_graph.nodes.node_validate._load_prompt",
            return_value="You are a document classifier.",
        ),
        pytest.raises(IngestNodeError, match="validate_error"),
    ):
        await node_validate(state, _make_config(session, llm))


@pytest.mark.asyncio
async def test_only_text_sample_sent_to_llm() -> None:
    """extracted_text with >2000 words → LLM receives a truncated sample."""
    session = _make_session()

    captured_messages: list = []

    async def capture_chat(**kwargs: object) -> MagicMock:
        captured_messages.extend(kwargs.get("messages", []))
        return _make_llm_response({"category": "general", "quality_score": 0.8, "confidence": 0.7})

    llm = MagicMock()
    llm.chat_completion = capture_chat

    long_text = " ".join([f"word{i}" for i in range(20000)])
    state = _make_state(long_text)

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_validate.get_collection",
            new=AsyncMock(return_value=_make_collection()),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_validate.get_model",
            new=AsyncMock(return_value=_make_model_record()),
        ),
        patch("src.graphs.ingest_graph.nodes.node_validate.update_step", new=AsyncMock()),
        patch(
            "src.graphs.ingest_graph.nodes.node_validate._load_prompt",
            return_value="You are a document classifier.",
        ),
    ):
        await node_validate(state, _make_config(session, llm))

    # Find user message
    user_message = next(m for m in captured_messages if m["role"] == "user")
    # Sample is limited to 2000 words; 20000 word text → content should be shorter
    sample_word_count = len(user_message["content"].split())
    # The user message wraps sample with <document> tags and whitespace — count words inside
    # A 2000-word sample will have ~2000 words plus XML tags
    assert sample_word_count <= 2010, f"Expected truncated sample, got {sample_word_count} words"


@pytest.mark.asyncio
async def test_validates_json_response_from_llm() -> None:
    """LLM returns malformed JSON → IngestNodeError with validation_parse_error."""
    session = _make_session()

    message = MagicMock()
    message.content = "not valid json at all {{{"
    choice = MagicMock()
    choice.message = message
    response = MagicMock()
    response.choices = [choice]

    llm = MagicMock()
    llm.chat_completion = AsyncMock(return_value=response)
    state = _make_state()

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_validate.get_collection",
            new=AsyncMock(return_value=_make_collection()),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_validate.get_model",
            new=AsyncMock(return_value=_make_model_record()),
        ),
        patch("src.graphs.ingest_graph.nodes.node_validate.update_step", new=AsyncMock()),
        patch(
            "src.graphs.ingest_graph.nodes.node_validate._load_prompt",
            return_value="You are a document classifier.",
        ),
        pytest.raises(IngestNodeError, match="validation_parse_error"),
    ):
        await node_validate(state, _make_config(session, llm))

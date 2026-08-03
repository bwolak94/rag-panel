"""Unit tests for node_detect_language.

Tests cover:
- Polish text → detected as "pol", response_language follows pipeline config
- English text → detected as "eng"
- Low confidence fallback → "pol"
- Detection failure fallback → "pol"
- Forced response_language via prompt_config
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.graphs.query_graph.nodes.node_detect_language import node_detect_language
from src.graphs.query_graph.state import QueryState

TENANT_ID = uuid.uuid4()
PIPELINE_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()


def _make_state(
    question: str = "Jakie są wymagania?",
    prompt_config: dict[str, Any] | None = None,
) -> QueryState:
    return QueryState(
        question=question,
        conversation_id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        allowed_collection_ids=[COLLECTION_ID],
        pipeline_id=PIPELINE_ID,
        llm_model_id=uuid.uuid4(),
        collection_ids=[COLLECTION_ID],
        prompt_config=prompt_config or {},
    )


def _fake_config() -> dict[str, Any]:
    return {"configurable": {}}


def _make_lang_result(lang: str, prob: float) -> MagicMock:
    r = MagicMock()
    r.lang = lang
    r.prob = prob
    return r


@pytest.mark.asyncio
async def test_polish_query_detected_as_pol() -> None:
    """Polish text → detected_language=pol, response_language=pol (auto mode)."""
    state = _make_state("Jakie są procedury przyjęcia pacjenta?", {"response_language": "auto"})
    mock_result = [_make_lang_result("pl", 0.95)]

    with patch("langdetect.detect_langs", return_value=mock_result):
        result = await node_detect_language(state, _fake_config())

    assert result["detected_language"] == "pol"
    assert result["response_language"] == "pol"


@pytest.mark.asyncio
async def test_english_query_detected_as_eng() -> None:
    """English text → detected_language=eng, response_language=eng (auto mode)."""
    state = _make_state(
        "What are the patient admission requirements?",
        {"response_language": "auto"},
    )
    mock_result = [_make_lang_result("en", 0.98)]

    with patch("langdetect.detect_langs", return_value=mock_result):
        result = await node_detect_language(state, _fake_config())

    assert result["detected_language"] == "eng"
    assert result["response_language"] == "eng"


@pytest.mark.asyncio
async def test_low_confidence_falls_back_to_pol() -> None:
    """Detection below confidence threshold → fallback to 'pol'."""
    state = _make_state("ok", {"response_language": "auto"})
    mock_result = [_make_lang_result("en", 0.5)]  # below 0.8 threshold

    with patch("langdetect.detect_langs", return_value=mock_result):
        result = await node_detect_language(state, _fake_config())

    assert result["detected_language"] == "pol"
    assert result["response_language"] == "pol"


@pytest.mark.asyncio
async def test_detection_exception_falls_back_to_pol() -> None:
    """langdetect raises exception → fallback to 'pol'."""
    state = _make_state("???", {"response_language": "auto"})

    with patch("langdetect.detect_langs", side_effect=Exception("lang detect error")):
        result = await node_detect_language(state, _fake_config())

    assert result["detected_language"] == "pol"
    assert result["response_language"] == "pol"


@pytest.mark.asyncio
async def test_forced_response_language_overrides_detected() -> None:
    """Forced response_language='pol' overrides detected language."""
    state = _make_state(
        "What are the patient admission requirements?",
        {"response_language": "pol"},
    )
    mock_result = [_make_lang_result("en", 0.99)]

    with patch("langdetect.detect_langs", return_value=mock_result):
        result = await node_detect_language(state, _fake_config())

    assert result["detected_language"] == "eng"
    assert result["response_language"] == "pol"  # forced


@pytest.mark.asyncio
async def test_no_response_language_in_config_uses_auto() -> None:
    """Empty prompt_config (no response_language key) → treated as 'auto' → uses detected."""
    state = _make_state("What are the requirements?", prompt_config={})
    mock_result = [_make_lang_result("en", 0.99)]

    with patch("langdetect.detect_langs", return_value=mock_result):
        result = await node_detect_language(state, _fake_config())

    assert result["detected_language"] == "eng"
    assert result["response_language"] == "eng"

"""Unit tests for query graph routing functions.

Tests route_after_classify, route_after_detect_language, and route_after_grade.
"""

from __future__ import annotations

import uuid

from src.graphs.query_graph.routing import (
    route_after_classify,
    route_after_detect_language,
    route_after_grade,
)
from src.graphs.query_graph.state import QueryState

TENANT_ID = uuid.uuid4()
LLM_MODEL_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()
PIPELINE_ID = uuid.uuid4()


def _make_state(**kwargs: object) -> QueryState:
    defaults: dict[str, object] = {
        "question": "Jakie są procedury?",
        "conversation_id": uuid.uuid4(),
        "tenant_id": TENANT_ID,
        "allowed_collection_ids": [COLLECTION_ID],
        "pipeline_id": PIPELINE_ID,
        "llm_model_id": LLM_MODEL_ID,
        "collection_ids": [COLLECTION_ID],
    }
    defaults.update(kwargs)
    return QueryState(**defaults)  # type: ignore[arg-type]


# ── route_after_classify ──────────────────────────────────────────────────────


def test_route_after_classify_topical_goes_to_rewrite() -> None:
    """Topical intent routes to node_rewrite_query."""
    state = _make_state(intent="topical", halt=False)
    assert route_after_classify(state) == "node_rewrite_query"


def test_route_after_classify_chitchat_goes_to_guardrails() -> None:
    """Chitchat intent routes to node_guardrails_output."""
    state = _make_state(intent="chitchat", halt=True)
    assert route_after_classify(state) == "node_guardrails_output"


def test_route_after_classify_out_of_scope_goes_to_guardrails() -> None:
    """out_of_scope intent routes to node_guardrails_output."""
    state = _make_state(intent="out_of_scope", halt=True)
    assert route_after_classify(state) == "node_guardrails_output"


def test_route_after_classify_none_intent_goes_to_guardrails() -> None:
    """None intent (unexpected) routes to node_guardrails_output (not 'topical')."""
    state = _make_state(intent=None, halt=False)
    assert route_after_classify(state) == "node_guardrails_output"


# ── route_after_grade ─────────────────────────────────────────────────────────


def test_route_after_grade_with_chunks_goes_to_generate() -> None:
    """Non-empty graded_chunks routes to node_generate."""
    chunk = {
        "point_id": str(uuid.uuid4()),
        "document_id": str(uuid.uuid4()),
        "score": 0.9,
        "page_number": 1,
        "highlight_text": "some text",
        "collection_id": str(COLLECTION_ID),
        "payload": {},
    }
    state = _make_state(graded_chunks=[chunk], no_results=False)
    assert route_after_grade(state) == "node_generate"


def test_route_after_grade_empty_chunks_goes_to_guardrails() -> None:
    """Empty graded_chunks routes to node_guardrails_output."""
    state = _make_state(graded_chunks=[], no_results=True)
    assert route_after_grade(state) == "node_guardrails_output"


# ── route_after_detect_language ───────────────────────────────────────────────


def test_route_after_detect_language_english_routes_to_translate() -> None:
    """Non-Polish detected language → node_translate_query."""
    state = _make_state(detected_language="eng", cross_language_retrieval=False)
    assert route_after_detect_language(state) == "node_translate_query"


def test_route_after_detect_language_polish_routes_to_retrieve() -> None:
    """Polish detected → skip translation, go to node_retrieve."""
    state = _make_state(detected_language="pol", cross_language_retrieval=False)
    assert route_after_detect_language(state) == "node_retrieve"


def test_route_after_detect_language_already_cross_routes_to_retrieve() -> None:
    """cross_language_retrieval already True → skip translate."""
    state = _make_state(detected_language="eng", cross_language_retrieval=True)
    assert route_after_detect_language(state) == "node_retrieve"


def test_route_after_detect_language_none_routes_to_retrieve() -> None:
    """No detected language (None) → go directly to node_retrieve."""
    state = _make_state(detected_language=None, cross_language_retrieval=False)
    assert route_after_detect_language(state) == "node_retrieve"


def test_route_after_grade_multiple_chunks_goes_to_generate() -> None:
    """Multiple graded chunks correctly route to node_generate."""
    chunks = [
        {
            "point_id": str(uuid.uuid4()),
            "document_id": str(uuid.uuid4()),
            "score": 0.85,
            "page_number": None,
            "highlight_text": "chunk text",
            "collection_id": str(COLLECTION_ID),
            "payload": {},
        }
        for _ in range(5)
    ]
    state = _make_state(graded_chunks=chunks, no_results=False)
    assert route_after_grade(state) == "node_generate"

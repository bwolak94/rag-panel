"""Unit tests for node_generate.

Tests cover:
- Normal generation: LLM returns valid JSON with answer and citations
- Citation mapping: chunk fields are correctly mapped to MessageSourceOut-compatible dict
- Invalid chunk_index in citation (out of range) → citation skipped
- LLM returns JSON without "answer" key → answer defaults to ""
- LLM returns invalid JSON → QueryNodeError raised
- LLM raises a generic exception → QueryNodeError raised
- Model not found in DB → QueryNodeError raised
- Token counts are extracted from response.usage when present
- Token budget not exceeded: all chunks included
- Token budget exceeded: lowest-score chunks dropped
- Token budget=None in state but guardrails_config override applies
- _count_tokens returns correct value
- _trim_chunks_to_budget preserves original chunk order
- prompt_config.max_context_tokens overrides global default (ADR-021)
- context_token_budget runtime override beats prompt_config.max_context_tokens
- context_tokens_used returned in node result (ADR-021)
- Early stopping: all chunks exceed budget → no_results=True, LLM not called (ADR-021)
- Early stopping not triggered when graded_chunks already empty before trimming
- ADR-019: shadow task scheduled when ab_test.enabled and session_factory present
- ADR-019: shadow task skipped when session_factory absent
- ADR-019: shadow task skipped when should_run_shadow returns False
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.exceptions import QueryNodeError
from src.graphs.query_graph.nodes.node_generate import (
    _count_tokens,
    _trim_chunks_to_budget,
    node_generate,
)
from src.graphs.query_graph.state import QueryState

TENANT_ID = uuid.uuid4()
LLM_MODEL_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()
PIPELINE_ID = uuid.uuid4()
DOCUMENT_ID = str(uuid.uuid4())
CHUNK_ID = str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _make_state(
    graded_chunks: list[dict] | None = None,
    question: str = "Jakie są procedury przyjęcia pacjenta?",
    conversation_history: list[dict[str, str]] | None = None,
    context_token_budget: int | None = None,
    guardrails_config: dict | None = None,
    prompt_config: dict | None = None,
) -> QueryState:
    return QueryState(
        question=question,
        conversation_id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        allowed_collection_ids=[COLLECTION_ID],
        pipeline_id=PIPELINE_ID,
        llm_model_id=LLM_MODEL_ID,
        collection_ids=[COLLECTION_ID],
        graded_chunks=graded_chunks or [],
        conversation_history=conversation_history or [],
        context_token_budget=context_token_budget,
        guardrails_config=guardrails_config or {},
        prompt_config=prompt_config or {},
    )


def _make_chunk(
    document_id: str = DOCUMENT_ID,
    collection_id: str | None = None,
    score: float = 0.9,
    highlight_text: str = "Pacjent musi wypełnić formularz rejestracyjny.",
    page_number: int = 2,
    chunk_id: str = CHUNK_ID,
    document_title: str = "Procedury kliniki",
) -> dict:
    coll_id = collection_id or str(COLLECTION_ID)
    return {
        "document_id": document_id,
        "collection_id": coll_id,
        "score": score,
        "highlight_text": highlight_text,
        "page_number": page_number,
        "chunk_id": chunk_id,
        "payload": {
            "document_title": document_title,
            "collection_id": coll_id,
        },
    }


def _make_model_record(model_id: str = "llama3.2") -> MagicMock:
    record = MagicMock()
    record.id = LLM_MODEL_ID
    record.model_id = model_id
    record.endpoint_url = "http://ollama:11434/v1"
    return record


def _make_llm_response(
    content: str,
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
) -> MagicMock:
    choice = MagicMock()
    choice.message.content = content
    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    response = MagicMock()
    response.choices = [choice]
    response.usage = usage
    return response


def _make_db(model_record: object) -> AsyncMock:
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = model_record
    db.execute = AsyncMock(return_value=result)
    return db


def _make_config(llm: object, db: object) -> dict:
    return {"configurable": {"llm": llm, "db": db}}


# ---------------------------------------------------------------------------
# Helper: patch the prompt loader so tests don't depend on the file on disk
# ---------------------------------------------------------------------------

FAKE_PROMPT = (
    "Odpowiedz na pytanie: {{QUESTION}}\n"
    "Kontekst: {{CONTEXT_CHUNKS}}\n"
    "Historia: {{CONVERSATION_HISTORY}}\n"
    'Zwróć JSON {"answer": ..., "citations": [...]}'
)

# Pre-computed overhead for FAKE_PROMPT with default _make_state() parameters.
# node_generate renders FAKE_PROMPT with {{CONTEXT_CHUNKS}}="" to measure overhead
# before trimming chunks (ADR-021 §2b). Tests that set tight budgets must add this
# overhead so that chunk_budget = total_budget - FAKE_PROMPT_OVERHEAD_TOKENS >= 0.
_DEFAULT_QUESTION = "Jakie są procedury przyjęcia pacjenta?"
_DEFAULT_HISTORY_RENDERED = "(brak historii)"  # _format_history([]) output
FAKE_PROMPT_OVERHEAD_TOKENS: int = _count_tokens(
    FAKE_PROMPT.replace("{{QUESTION}}", _DEFAULT_QUESTION)
    .replace("{{CONTEXT_CHUNKS}}", "")
    .replace("{{CONVERSATION_HISTORY}}", _DEFAULT_HISTORY_RENDERED)
    .replace("{{RESPONSE_LANGUAGE}}", "Polish")
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_normal_generation_returns_answer_and_citations() -> None:
    """LLM returns valid JSON → answer and citations populated, token counts set."""
    chunk = _make_chunk()
    state = _make_state(graded_chunks=[chunk])

    llm_json = json.dumps(
        {"answer": "Pacjent wypełnia formularz.", "citations": [{"chunk_index": 1}]}
    )
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(
        return_value=_make_llm_response(llm_json, prompt_tokens=200, completion_tokens=80)
    )

    with patch("src.graphs.query_graph.nodes.node_generate._load_prompt", return_value=FAKE_PROMPT):
        result = await node_generate(state, _make_config(llm, db))

    assert result["answer"] == "Pacjent wypełnia formularz."
    assert len(result["citations"]) == 1
    assert result["prompt_tokens"] == 200
    assert result["completion_tokens"] == 80


@pytest.mark.asyncio
async def test_citation_mapping_populates_correct_fields() -> None:
    """Citation at chunk_index=1 maps all source fields from the graded chunk."""
    doc_id = str(uuid.uuid4())
    coll_id = str(uuid.uuid4())
    chunk_id = str(uuid.uuid4())
    chunk = {
        "document_id": doc_id,
        "collection_id": coll_id,
        "score": 0.95,
        "highlight_text": "Fragment dokumentu.",
        "page_number": 5,
        "chunk_id": chunk_id,
        "payload": {
            "document_title": "Regulamin kliniki",
            "collection_id": coll_id,
        },
    }
    state = _make_state(graded_chunks=[chunk])

    llm_json = json.dumps({"answer": "Odpowiedź.", "citations": [{"chunk_index": 1}]})
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(llm_json))

    with patch("src.graphs.query_graph.nodes.node_generate._load_prompt", return_value=FAKE_PROMPT):
        result = await node_generate(state, _make_config(llm, db))

    assert len(result["citations"]) == 1
    cit = result["citations"][0]
    assert cit["document_id"] == doc_id
    assert cit["collection_id"] == coll_id
    assert cit["document_title"] == "Regulamin kliniki"
    assert cit["page_number"] == 5
    assert cit["highlight_text"] == "Fragment dokumentu."
    assert cit["chunk_id"] == chunk_id
    assert cit["relevance_score"] == pytest.approx(0.95)


@pytest.mark.asyncio
async def test_invalid_chunk_index_citation_is_skipped() -> None:
    """Citation with chunk_index=99 for a 2-chunk list is silently dropped."""
    chunks = [_make_chunk(), _make_chunk(document_id=str(uuid.uuid4()))]
    state = _make_state(graded_chunks=chunks)

    llm_json = json.dumps(
        {
            "answer": "Odpowiedź.",
            "citations": [{"chunk_index": 99}, {"chunk_index": 1}],
        }
    )
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(llm_json))

    with patch("src.graphs.query_graph.nodes.node_generate._load_prompt", return_value=FAKE_PROMPT):
        result = await node_generate(state, _make_config(llm, db))

    # Only the valid citation at index 1 survives
    assert len(result["citations"]) == 1
    assert result["citations"][0]["document_id"] == chunks[0]["document_id"]


@pytest.mark.asyncio
async def test_zero_chunk_index_citation_is_skipped() -> None:
    """Citation with chunk_index=0 (below 1) is silently dropped."""
    chunk = _make_chunk()
    state = _make_state(graded_chunks=[chunk])

    llm_json = json.dumps({"answer": "Odpowiedź.", "citations": [{"chunk_index": 0}]})
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(llm_json))

    with patch("src.graphs.query_graph.nodes.node_generate._load_prompt", return_value=FAKE_PROMPT):
        result = await node_generate(state, _make_config(llm, db))

    assert result["citations"] == []


@pytest.mark.asyncio
async def test_json_without_answer_key_defaults_to_empty_string() -> None:
    """LLM returns JSON without 'answer' key → answer defaults to empty string."""
    chunk = _make_chunk()
    state = _make_state(graded_chunks=[chunk])

    # JSON has "citations" but no "answer"
    llm_json = json.dumps({"citations": [{"chunk_index": 1}]})
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(llm_json))

    with patch("src.graphs.query_graph.nodes.node_generate._load_prompt", return_value=FAKE_PROMPT):
        result = await node_generate(state, _make_config(llm, db))

    assert result["answer"] == ""
    # Citations are still mapped
    assert len(result["citations"]) == 1


@pytest.mark.asyncio
async def test_invalid_json_raises_query_node_error() -> None:
    """LLM returns non-JSON string → QueryNodeError with parse error message."""
    state = _make_state(graded_chunks=[_make_chunk()])

    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response("To nie jest JSON {broken"))

    with (
        patch("src.graphs.query_graph.nodes.node_generate._load_prompt", return_value=FAKE_PROMPT),
        pytest.raises(QueryNodeError, match="generate_parse_error"),
    ):
        await node_generate(state, _make_config(llm, db))


@pytest.mark.asyncio
async def test_llm_generic_exception_raises_query_node_error() -> None:
    """LLM raises an unexpected exception → QueryNodeError with llm_error message."""
    state = _make_state(graded_chunks=[_make_chunk()])

    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(side_effect=RuntimeError("connection refused"))

    with (
        patch("src.graphs.query_graph.nodes.node_generate._load_prompt", return_value=FAKE_PROMPT),
        pytest.raises(QueryNodeError, match="generate_llm_error"),
    ):
        await node_generate(state, _make_config(llm, db))


@pytest.mark.asyncio
async def test_model_not_found_raises_query_node_error() -> None:
    """DB returns None for model record → QueryNodeError before LLM call."""
    state = _make_state()

    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result_mock)

    llm = AsyncMock()

    with pytest.raises(QueryNodeError, match="LLM model not found"):
        await node_generate(state, _make_config(llm, db))

    # LLM must never be called when model lookup fails
    llm.chat_completion.assert_not_called()


@pytest.mark.asyncio
async def test_token_counts_extracted_from_response_usage() -> None:
    """Token counts from response.usage are returned in the result dict."""
    state = _make_state(graded_chunks=[_make_chunk()])

    llm_json = json.dumps({"answer": "Odpowiedź.", "citations": []})
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(
        return_value=_make_llm_response(llm_json, prompt_tokens=512, completion_tokens=128)
    )

    with patch("src.graphs.query_graph.nodes.node_generate._load_prompt", return_value=FAKE_PROMPT):
        result = await node_generate(state, _make_config(llm, db))

    assert result["prompt_tokens"] == 512
    assert result["completion_tokens"] == 128


@pytest.mark.asyncio
async def test_missing_usage_defaults_token_counts_to_zero() -> None:
    """When response.usage is None, token counts default to 0."""
    state = _make_state(graded_chunks=[_make_chunk()])

    llm_json = json.dumps({"answer": "Odpowiedź.", "citations": []})
    model_record = _make_model_record()
    db = _make_db(model_record)

    choice = MagicMock()
    choice.message.content = llm_json
    response = MagicMock()
    response.choices = [choice]
    response.usage = None

    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=response)

    with patch("src.graphs.query_graph.nodes.node_generate._load_prompt", return_value=FAKE_PROMPT):
        result = await node_generate(state, _make_config(llm, db))

    assert result["prompt_tokens"] == 0
    assert result["completion_tokens"] == 0


@pytest.mark.asyncio
async def test_multiple_citations_all_valid_are_included() -> None:
    """Multiple valid citations all map correctly and are included in the result."""
    chunk_a = _make_chunk(document_id=str(uuid.uuid4()), highlight_text="Tekst A.", page_number=1)
    chunk_b = _make_chunk(document_id=str(uuid.uuid4()), highlight_text="Tekst B.", page_number=3)
    state = _make_state(graded_chunks=[chunk_a, chunk_b])

    llm_json = json.dumps(
        {
            "answer": "Odpowiedź łączona.",
            "citations": [{"chunk_index": 1}, {"chunk_index": 2}],
        }
    )
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(llm_json))

    with patch("src.graphs.query_graph.nodes.node_generate._load_prompt", return_value=FAKE_PROMPT):
        result = await node_generate(state, _make_config(llm, db))

    assert len(result["citations"]) == 2
    assert result["citations"][0]["page_number"] == 1
    assert result["citations"][1]["page_number"] == 3


@pytest.mark.asyncio
async def test_no_chunks_and_no_citations_returns_empty_citations() -> None:
    """Empty graded_chunks list with no citations → empty citations list returned."""
    state = _make_state(graded_chunks=[])

    llm_json = json.dumps({"answer": "Brak kontekstu.", "citations": []})
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(llm_json))

    with patch("src.graphs.query_graph.nodes.node_generate._load_prompt", return_value=FAKE_PROMPT):
        result = await node_generate(state, _make_config(llm, db))

    assert result["answer"] == "Brak kontekstu."
    assert result["citations"] == []


@pytest.mark.asyncio
async def test_collection_id_falls_back_to_payload_when_missing_on_chunk() -> None:
    """When chunk has no top-level collection_id, payload.collection_id is used."""
    coll_id_in_payload = str(uuid.uuid4())
    chunk = {
        "document_id": DOCUMENT_ID,
        # no "collection_id" at top level
        "score": 0.88,
        "highlight_text": "Treść fragmentu.",
        "page_number": 7,
        "chunk_id": CHUNK_ID,
        "payload": {
            "document_title": "Dokument testowy",
            "collection_id": coll_id_in_payload,
        },
    }
    state = _make_state(graded_chunks=[chunk])

    llm_json = json.dumps({"answer": "Odpowiedź.", "citations": [{"chunk_index": 1}]})
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(llm_json))

    with patch("src.graphs.query_graph.nodes.node_generate._load_prompt", return_value=FAKE_PROMPT):
        result = await node_generate(state, _make_config(llm, db))

    assert result["citations"][0]["collection_id"] == coll_id_in_payload


@pytest.mark.asyncio
async def test_llm_called_with_correct_model_and_base_url() -> None:
    """LLM chat_completion is called with model_id and endpoint_url from DB record."""
    state = _make_state(graded_chunks=[_make_chunk()])

    llm_json = json.dumps({"answer": "OK.", "citations": []})
    model_record = _make_model_record(model_id="mistral-7b")
    model_record.endpoint_url = "http://vllm:8000/v1"
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(llm_json))

    with patch("src.graphs.query_graph.nodes.node_generate._load_prompt", return_value=FAKE_PROMPT):
        await node_generate(state, _make_config(llm, db))

    call_kwargs = llm.chat_completion.call_args.kwargs
    assert call_kwargs["model"] == "mistral-7b"
    assert call_kwargs["base_url"] == "http://vllm:8000/v1"
    assert call_kwargs["temperature"] == 0.0
    assert call_kwargs["response_format"] == {"type": "json_object"}


# ---------------------------------------------------------------------------
# Token budget helpers — pure-function tests (no LLM needed)
# ---------------------------------------------------------------------------


def test_count_tokens_returns_positive_integer_for_nonempty_text() -> None:
    """_count_tokens returns a positive integer for any non-empty string."""
    count = _count_tokens("Pacjent musi wypełnić formularz rejestracyjny.")
    assert isinstance(count, int)
    assert count > 0


def test_count_tokens_returns_zero_for_empty_string() -> None:
    """_count_tokens returns 0 for an empty string."""
    assert _count_tokens("") == 0


def test_trim_chunks_budget_not_exceeded_keeps_all_chunks() -> None:
    """When total tokens fit within budget, all chunks are returned unchanged."""
    chunks = [
        _make_chunk(highlight_text="Short text A.", score=0.9),
        _make_chunk(highlight_text="Short text B.", score=0.8),
    ]
    # Use a large budget so nothing is trimmed
    kept, dropped, tokens_used = _trim_chunks_to_budget(chunks, budget=100_000)
    assert dropped == 0
    assert len(kept) == 2


def test_trim_chunks_budget_exceeded_drops_lowest_score_chunks() -> None:
    """When budget is very small, the lowest-scoring chunk is dropped first."""
    high_score_chunk = _make_chunk(highlight_text="High score text.", score=0.95)
    low_score_chunk = _make_chunk(highlight_text="Low score text.", score=0.10)
    chunks = [high_score_chunk, low_score_chunk]

    # Budget that fits exactly one short chunk but not two
    single_chunk_tokens = _count_tokens("High score text.")
    budget = single_chunk_tokens  # fits exactly the high-score chunk

    kept, dropped, tokens_used = _trim_chunks_to_budget(chunks, budget=budget)
    assert dropped == 1
    assert len(kept) == 1
    # The high-score chunk must be retained
    assert kept[0]["score"] == pytest.approx(0.95)


def test_trim_chunks_preserves_original_order_of_kept_chunks() -> None:
    """Chunks kept after trimming appear in their original list order."""
    chunk_a = _make_chunk(highlight_text="Alpha text.", score=0.7, document_id="aaa")
    chunk_b = _make_chunk(highlight_text="Beta text.", score=0.9, document_id="bbb")
    chunk_c = _make_chunk(highlight_text="Gamma text.", score=0.5, document_id="ccc")
    chunks = [chunk_a, chunk_b, chunk_c]

    # Budget that allows two chunks (b + a) but not c
    budget = _count_tokens("Alpha text.") + _count_tokens("Beta text.")

    kept, dropped, tokens_used = _trim_chunks_to_budget(chunks, budget=budget)
    assert dropped == 1
    # Original order: a (idx 0) then b (idx 1)
    assert kept[0]["document_id"] == "aaa"
    assert kept[1]["document_id"] == "bbb"


def test_trim_chunks_empty_list_returns_empty_with_zero_dropped() -> None:
    """Empty chunk list returns empty list with 0 dropped."""
    kept, dropped, tokens_used = _trim_chunks_to_budget([], budget=1000)
    assert kept == []
    assert dropped == 0
    assert tokens_used == 0


# ---------------------------------------------------------------------------
# Token budget integration — node_generate end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_not_exceeded_all_chunks_included() -> None:
    """When chunks fit within budget, all are passed to LLM and chunks_trimmed==0."""
    chunks = [
        _make_chunk(highlight_text="A.", score=0.9),
        _make_chunk(highlight_text="B.", score=0.8),
    ]
    # Budget large enough for both tiny chunks
    state = _make_state(graded_chunks=chunks, context_token_budget=100_000)

    llm_json = json.dumps({"answer": "Odpowiedź.", "citations": []})
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(llm_json))

    with patch("src.graphs.query_graph.nodes.node_generate._load_prompt", return_value=FAKE_PROMPT):
        result = await node_generate(state, _make_config(llm, db))

    assert result["chunks_trimmed"] == 0
    # Both chunks appear in the prompt — check that context contains both doc markers
    call_args = llm.chat_completion.call_args.kwargs
    context_in_prompt = call_args["messages"][0]["content"]
    assert 'index="1"' in context_in_prompt
    assert 'index="2"' in context_in_prompt


@pytest.mark.asyncio
async def test_budget_exceeded_lowest_score_chunk_dropped() -> None:
    """When budget is tight, the lowest-score chunk is dropped and chunks_trimmed==1."""
    high_chunk = _make_chunk(
        highlight_text="Important clinical note.",
        score=0.95,
        document_id="high-doc",
    )
    low_chunk = _make_chunk(
        highlight_text="Less relevant text here.",
        score=0.10,
        document_id="low-doc",
    )
    chunks = [high_chunk, low_chunk]

    # Budget = overhead + exactly enough for the high-score chunk only
    budget = FAKE_PROMPT_OVERHEAD_TOKENS + _count_tokens("Important clinical note.")
    state = _make_state(graded_chunks=chunks, context_token_budget=budget)

    llm_json = json.dumps({"answer": "Odpowiedź.", "citations": []})
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(llm_json))

    with patch("src.graphs.query_graph.nodes.node_generate._load_prompt", return_value=FAKE_PROMPT):
        result = await node_generate(state, _make_config(llm, db))

    assert result["chunks_trimmed"] == 1
    # Low-score chunk must not appear in the prompt as a document_id attribute
    call_args = llm.chat_completion.call_args.kwargs
    prompt_content = call_args["messages"][0]["content"]
    assert 'document_id="low-doc"' not in prompt_content
    assert 'document_id="high-doc"' in prompt_content


@pytest.mark.asyncio
async def test_budget_none_falls_back_to_global_default() -> None:
    """When state.context_token_budget is None and no guardrails override, the global
    default (patched to a large value) is used so no trimming occurs."""
    chunks = [_make_chunk(highlight_text="Tekst A.", score=0.9)]
    # context_token_budget=None (default) and empty guardrails_config
    state = _make_state(graded_chunks=chunks, context_token_budget=None, guardrails_config={})

    llm_json = json.dumps({"answer": "Odpowiedź.", "citations": []})
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(llm_json))

    # Patch the global default to a large value to confirm no trimming
    with (
        patch("src.graphs.query_graph.nodes.node_generate._load_prompt", return_value=FAKE_PROMPT),
        patch("src.graphs.query_graph.nodes.node_generate.settings") as mock_settings,
    ):
        mock_settings.DEFAULT_CONTEXT_TOKEN_BUDGET = 100_000
        result = await node_generate(state, _make_config(llm, db))

    assert result["chunks_trimmed"] == 0


@pytest.mark.asyncio
async def test_guardrails_config_budget_overrides_global_default() -> None:
    """Per-pipeline guardrails_config["context_token_budget"] takes precedence over
    the global default when state.context_token_budget is None."""
    high_chunk = _make_chunk(highlight_text="Critical info.", score=0.95, document_id="h")
    low_chunk = _make_chunk(highlight_text="Noise.", score=0.05, document_id="l")
    chunks = [high_chunk, low_chunk]

    # Budget in guardrails_config = overhead + exactly enough for the high-score chunk
    pipeline_budget = FAKE_PROMPT_OVERHEAD_TOKENS + _count_tokens("Critical info.")
    state = _make_state(
        graded_chunks=chunks,
        context_token_budget=None,
        guardrails_config={"context_token_budget": pipeline_budget},
    )

    llm_json = json.dumps({"answer": "Odpowiedź.", "citations": []})
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(llm_json))

    with patch("src.graphs.query_graph.nodes.node_generate._load_prompt", return_value=FAKE_PROMPT):
        result = await node_generate(state, _make_config(llm, db))

    assert result["chunks_trimmed"] == 1
    call_args = llm.chat_completion.call_args.kwargs
    prompt_content = call_args["messages"][0]["content"]
    # document_id="h" is present; document_id="l" is absent from the XML chunks
    assert 'document_id="h"' in prompt_content
    assert 'document_id="l"' not in prompt_content


# ---------------------------------------------------------------------------
# ADR-021: new tests — prompt_config.max_context_tokens and early stopping
# ---------------------------------------------------------------------------


def test_trim_chunks_returns_tokens_used() -> None:
    """_trim_chunks_to_budget third return value is the token count of kept chunks."""
    chunk = _make_chunk(highlight_text="Hello world.", score=0.9)
    expected_tokens = _count_tokens("Hello world.")

    kept, dropped, tokens_used = _trim_chunks_to_budget([chunk], budget=100_000)
    assert dropped == 0
    assert tokens_used == expected_tokens


def test_trim_chunks_tokens_used_counts_only_kept_chunks() -> None:
    """tokens_used reflects only the chunks that fit the budget, not dropped ones."""
    high_chunk = _make_chunk(highlight_text="Kept text here.", score=0.9, document_id="kept")
    low_chunk = _make_chunk(highlight_text="Dropped text here.", score=0.1, document_id="drop")
    budget = _count_tokens("Kept text here.")

    kept, dropped, tokens_used = _trim_chunks_to_budget([high_chunk, low_chunk], budget=budget)
    assert dropped == 1
    assert tokens_used == _count_tokens("Kept text here.")


@pytest.mark.asyncio
async def test_prompt_config_max_context_tokens_overrides_global_default() -> None:
    """prompt_config['max_context_tokens'] takes precedence over the global default."""
    high_chunk = _make_chunk(highlight_text="Essential info.", score=0.9, document_id="kept")
    low_chunk = _make_chunk(highlight_text="Irrelevant filler.", score=0.1, document_id="drop")
    chunks = [high_chunk, low_chunk]

    # Set max_context_tokens = overhead + exactly enough for the high-score chunk only
    prompt_budget = FAKE_PROMPT_OVERHEAD_TOKENS + _count_tokens("Essential info.")
    state = _make_state(
        graded_chunks=chunks,
        context_token_budget=None,
        guardrails_config={},
        prompt_config={"max_context_tokens": prompt_budget},
    )

    llm_json = json.dumps({"answer": "Wynik.", "citations": []})
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(llm_json))

    with patch("src.graphs.query_graph.nodes.node_generate._load_prompt", return_value=FAKE_PROMPT):
        result = await node_generate(state, _make_config(llm, db))

    assert result["chunks_trimmed"] == 1
    prompt_content = llm.chat_completion.call_args.kwargs["messages"][0]["content"]
    assert 'document_id="kept"' in prompt_content
    assert 'document_id="drop"' not in prompt_content


@pytest.mark.asyncio
async def test_context_token_budget_overrides_prompt_config_max_context_tokens() -> None:
    """state.context_token_budget (runtime override) beats prompt_config.max_context_tokens."""
    chunk = _make_chunk(highlight_text="Single chunk.", score=0.9)
    state = _make_state(
        graded_chunks=[chunk],
        # runtime override large enough to keep the chunk
        context_token_budget=100_000,
        # prompt_config has a small but valid value (256 min); state override wins
        prompt_config={"max_context_tokens": 256},
    )

    llm_json = json.dumps({"answer": "Wynik.", "citations": []})
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(llm_json))

    with patch("src.graphs.query_graph.nodes.node_generate._load_prompt", return_value=FAKE_PROMPT):
        result = await node_generate(state, _make_config(llm, db))

    assert result["chunks_trimmed"] == 0


@pytest.mark.asyncio
async def test_node_generate_returns_context_tokens_used() -> None:
    """node_generate result includes context_tokens_used > 0 when chunks are included."""
    chunk = _make_chunk(highlight_text="Context text.", score=0.9)
    state = _make_state(graded_chunks=[chunk], context_token_budget=100_000)

    llm_json = json.dumps({"answer": "Wynik.", "citations": []})
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(llm_json))

    with patch("src.graphs.query_graph.nodes.node_generate._load_prompt", return_value=FAKE_PROMPT):
        result = await node_generate(state, _make_config(llm, db))

    assert "context_tokens_used" in result
    assert result["context_tokens_used"] == _count_tokens("Context text.")


@pytest.mark.asyncio
async def test_early_stopping_when_all_chunks_exceed_budget() -> None:
    """ADR-021: when budget is too small for any chunk, node_generate returns
    no_results=True without calling the LLM."""
    chunk = _make_chunk(highlight_text="Some medical information.", score=0.9)
    # Budget of 1 token — too small for any real chunk
    state = _make_state(graded_chunks=[chunk], context_token_budget=1)

    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock()

    with patch("src.graphs.query_graph.nodes.node_generate._load_prompt", return_value=FAKE_PROMPT):
        result = await node_generate(state, _make_config(llm, db))

    assert result["no_results"] is True
    # LLM must not be called — no context means no generation
    llm.chat_completion.assert_not_called()


@pytest.mark.asyncio
async def test_early_stopping_not_triggered_when_graded_chunks_already_empty() -> None:
    """Early stopping must NOT fire when graded_chunks was already empty before trimming.
    (That case is the normal no-results path handled by node_grade_documents.)"""
    state = _make_state(graded_chunks=[], context_token_budget=1)

    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm_json = json.dumps({"answer": "Nie znalazłem.", "citations": []})
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(llm_json))

    with patch("src.graphs.query_graph.nodes.node_generate._load_prompt", return_value=FAKE_PROMPT):
        result = await node_generate(state, _make_config(llm, db))

    # With empty input chunks the early stop guard must not trigger;
    # node_generate proceeds to call LLM (empty context is intentional here).
    assert result.get("no_results") is not True
    llm.chat_completion.assert_called_once()


# ---------------------------------------------------------------------------
# ADR-019: AB testing shadow path integration tests
# ---------------------------------------------------------------------------

_AB_CONFIG = {
    "enabled": True,
    "shadow_prompt_version": "v2",
    "traffic_split": 1.0,  # always run shadow
    "experiment_id": "exp_001",
}


def _make_config_with_session_factory(
    llm: object, db: object, session_factory: object
) -> dict:
    return {"configurable": {"llm": llm, "db": db, "session_factory": session_factory}}


@pytest.mark.asyncio
async def test_shadow_task_scheduled_when_ab_test_enabled_and_session_factory_present() -> None:
    """ADR-019: schedule_shadow_task is called when ab_test.enabled=True and session_factory set."""
    state = _make_state(
        graded_chunks=[_make_chunk()],
        prompt_config={"ab_test": _AB_CONFIG},
    )
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm_json = json.dumps({"answer": "Odpowiedź.", "citations": []})
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(llm_json))
    session_factory = MagicMock()

    with (
        patch(
            "src.graphs.query_graph.nodes.node_generate._load_prompt",
            return_value=FAKE_PROMPT,
        ),
        patch(
            "src.graphs.query_graph.nodes.node_generate.should_run_shadow",
            return_value=True,
        ),
        patch(
            "src.graphs.query_graph.nodes.node_generate.schedule_shadow_task"
        ) as mock_schedule,
    ):
        await node_generate(state, _make_config_with_session_factory(llm, db, session_factory))

    mock_schedule.assert_called_once()
    call_kwargs = mock_schedule.call_args.kwargs
    assert call_kwargs["session_factory"] is session_factory
    assert call_kwargs["state"] is state


@pytest.mark.asyncio
async def test_shadow_task_not_scheduled_when_session_factory_absent() -> None:
    """ADR-019: shadow is skipped when session_factory is not in configurable (no DB access).

    Python short-circuits on `session_factory is not None` before ever calling
    should_run_shadow — so mock_should_run must also assert_not_called().
    """
    state = _make_state(
        graded_chunks=[_make_chunk()],
        prompt_config={"ab_test": _AB_CONFIG},
    )
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm_json = json.dumps({"answer": "Odpowiedź.", "citations": []})
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(llm_json))

    with (
        patch(
            "src.graphs.query_graph.nodes.node_generate._load_prompt",
            return_value=FAKE_PROMPT,
        ),
        patch(
            "src.graphs.query_graph.nodes.node_generate.should_run_shadow",
        ) as mock_should_run,
        patch(
            "src.graphs.query_graph.nodes.node_generate.schedule_shadow_task"
        ) as mock_schedule,
    ):
        # No session_factory in config — gate short-circuits before should_run_shadow
        await node_generate(state, _make_config(llm, db))

    mock_should_run.assert_not_called()
    mock_schedule.assert_not_called()


@pytest.mark.asyncio
async def test_shadow_task_not_scheduled_when_should_run_shadow_false() -> None:
    """ADR-019: shadow is skipped when traffic-split coin-flip returns False."""
    state = _make_state(
        graded_chunks=[_make_chunk()],
        prompt_config={"ab_test": _AB_CONFIG},
    )
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm_json = json.dumps({"answer": "Odpowiedź.", "citations": []})
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(llm_json))
    session_factory = MagicMock()

    with (
        patch(
            "src.graphs.query_graph.nodes.node_generate._load_prompt",
            return_value=FAKE_PROMPT,
        ),
        patch(
            "src.graphs.query_graph.nodes.node_generate.should_run_shadow",
            return_value=False,  # coin-flip says no
        ),
        patch(
            "src.graphs.query_graph.nodes.node_generate.schedule_shadow_task"
        ) as mock_schedule,
    ):
        await node_generate(state, _make_config_with_session_factory(llm, db, session_factory))

    mock_schedule.assert_not_called()


@pytest.mark.asyncio
async def test_shadow_task_not_scheduled_when_ab_test_absent() -> None:
    """ADR-019: shadow is skipped when prompt_config has no 'ab_test' key.

    When ab_config is an empty dict (falsy), the guard short-circuits before
    evaluating session_factory or should_run_shadow.
    """
    state = _make_state(
        graded_chunks=[_make_chunk()],
        prompt_config={},  # no ab_test key
    )
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm_json = json.dumps({"answer": "Odpowiedź.", "citations": []})
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(llm_json))
    session_factory = MagicMock()

    with (
        patch(
            "src.graphs.query_graph.nodes.node_generate._load_prompt",
            return_value=FAKE_PROMPT,
        ),
        patch(
            "src.graphs.query_graph.nodes.node_generate.should_run_shadow",
        ) as mock_should_run,
        patch(
            "src.graphs.query_graph.nodes.node_generate.schedule_shadow_task"
        ) as mock_schedule,
    ):
        await node_generate(state, _make_config_with_session_factory(llm, db, session_factory))

    mock_should_run.assert_not_called()
    mock_schedule.assert_not_called()

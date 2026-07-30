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
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.exceptions import QueryNodeError
from src.graphs.query_graph.nodes.node_generate import node_generate
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

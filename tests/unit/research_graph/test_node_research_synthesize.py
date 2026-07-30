"""Unit tests for node_research_synthesize.

Tests cover:
- Normal synthesis: LLM returns valid JSON with answer and citations
- All accumulated chunks across iterations are present in the LLM context
- Citations are correctly mapped to source metadata dicts
- Invalid JSON response → QueryNodeError with parse error
- LLM generic exception → QueryNodeError with llm error
- Model not found in DB → QueryNodeError before LLM call
- Token counts are extracted from response.usage
- Empty all_retrieved_chunks → answer generated with "(brak kontekstu)" context
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.exceptions import QueryNodeError
from src.graphs.research_graph.nodes.node_research_synthesize import (
    node_research_synthesize,
)
from src.graphs.research_graph.state import ResearchState

TENANT_ID = uuid.uuid4()
PIPELINE_ID = uuid.uuid4()
LLM_MODEL_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()
DOCUMENT_ID_A = str(uuid.uuid4())
DOCUMENT_ID_B = str(uuid.uuid4())

FAKE_SYNTHESIZE_PROMPT = (
    "Synthesise answer for: {{ORIGINAL_QUESTION}}\n"
    "History: {{CONVERSATION_HISTORY}}\n"
    "Context: {{CONTEXT_CHUNKS}}\n"
    "Steps taken: {{STEPS_TAKEN}}\n"
    'Return JSON {"answer": ..., "citations": [...]}'
)


def _make_state(
    all_retrieved_chunks: list | None = None,
    steps_taken: int = 2,
) -> ResearchState:
    return ResearchState(
        original_question="Jakie jest leczenie cukrzycy typu 2 z CKD?",
        pipeline_id=PIPELINE_ID,
        tenant_id=TENANT_ID,
        collection_ids=[COLLECTION_ID],
        allowed_collection_ids=[COLLECTION_ID],
        llm_model_id=LLM_MODEL_ID,
        all_retrieved_chunks=all_retrieved_chunks or [],
        steps_taken=steps_taken,
    )


def _make_chunk(
    document_id: str = DOCUMENT_ID_A,
    score: float = 0.9,
    page_number: int = 1,
    highlight_text: str = "Clinical evidence text.",
    collection_id: str | None = None,
) -> dict:
    coll_id = collection_id or str(COLLECTION_ID)
    return {
        "point_id": str(uuid.uuid4()),
        "document_id": document_id,
        "score": score,
        "page_number": page_number,
        "highlight_text": highlight_text,
        "collection_id": coll_id,
        "payload": {
            "document_title": "Clinical Guidelines 2024",
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
    prompt_tokens: int = 500,
    completion_tokens: int = 150,
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


@pytest.mark.asyncio
async def test_node_research_synthesize_normal_flow_returns_answer_and_citations() -> None:
    """Normal flow: LLM returns valid JSON → final_answer and citations populated."""
    chunks = [_make_chunk(document_id=DOCUMENT_ID_A), _make_chunk(document_id=DOCUMENT_ID_B)]
    state = _make_state(all_retrieved_chunks=chunks, steps_taken=2)

    llm_json = json.dumps(
        {
            "answer": "Metformin powinien być stosowany ostrożnie w stadium 3 CKD.",
            "citations": [{"chunk_index": 1}, {"chunk_index": 2}],
        }
    )
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(
        return_value=_make_llm_response(llm_json, prompt_tokens=500, completion_tokens=150)
    )

    with patch(
        "src.graphs.research_graph.nodes.node_research_synthesize._load_prompt",
        return_value=FAKE_SYNTHESIZE_PROMPT,
    ):
        result = await node_research_synthesize(state, _make_config(llm, db))

    assert result["final_answer"] == "Metformin powinien być stosowany ostrożnie w stadium 3 CKD."
    assert len(result["citations"]) == 2
    assert result["prompt_tokens"] == 500
    assert result["completion_tokens"] == 150


@pytest.mark.asyncio
async def test_node_research_synthesize_all_chunks_present_in_context() -> None:
    """All accumulated chunks appear in the prompt passed to the LLM."""
    chunk_a = _make_chunk(document_id=DOCUMENT_ID_A, highlight_text="Text A.")
    chunk_b = _make_chunk(document_id=DOCUMENT_ID_B, highlight_text="Text B.")
    state = _make_state(all_retrieved_chunks=[chunk_a, chunk_b])

    llm_json = json.dumps({"answer": "Combined answer.", "citations": []})
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(llm_json))

    with patch(
        "src.graphs.research_graph.nodes.node_research_synthesize._load_prompt",
        return_value=FAKE_SYNTHESIZE_PROMPT,
    ):
        await node_research_synthesize(state, _make_config(llm, db))

    call_kwargs = llm.chat_completion.call_args.kwargs
    prompt_content = call_kwargs["messages"][0]["content"]

    # Both documents must be referenced in the rendered context
    assert DOCUMENT_ID_A in prompt_content
    assert DOCUMENT_ID_B in prompt_content
    assert 'index="1"' in prompt_content
    assert 'index="2"' in prompt_content


@pytest.mark.asyncio
async def test_node_research_synthesize_citation_mapping_correct_fields() -> None:
    """Citation at chunk_index=1 maps all source fields from the chunk."""
    coll_id = str(uuid.uuid4())
    chunk = _make_chunk(
        document_id=DOCUMENT_ID_A,
        score=0.95,
        page_number=5,
        highlight_text="Important clinical finding.",
        collection_id=coll_id,
    )
    chunk["payload"]["document_title"] = "Clinical Guidelines 2024"
    state = _make_state(all_retrieved_chunks=[chunk])

    llm_json = json.dumps(
        {
            "answer": "Odpowiedź.",
            "citations": [{"chunk_index": 1}],
        }
    )
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(llm_json))

    with patch(
        "src.graphs.research_graph.nodes.node_research_synthesize._load_prompt",
        return_value=FAKE_SYNTHESIZE_PROMPT,
    ):
        result = await node_research_synthesize(state, _make_config(llm, db))

    assert len(result["citations"]) == 1
    cit = result["citations"][0]
    assert cit["document_id"] == DOCUMENT_ID_A
    assert cit["collection_id"] == coll_id
    assert cit["page_number"] == 5
    assert cit["document_title"] == "Clinical Guidelines 2024"
    assert cit["relevance_score"] == pytest.approx(0.95)


@pytest.mark.asyncio
async def test_node_research_synthesize_invalid_json_raises_query_node_error() -> None:
    """LLM returns non-JSON → QueryNodeError with parse error."""
    state = _make_state(all_retrieved_chunks=[_make_chunk()])
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response("Not valid JSON {broken"))

    with (
        patch(
            "src.graphs.research_graph.nodes.node_research_synthesize._load_prompt",
            return_value=FAKE_SYNTHESIZE_PROMPT,
        ),
        pytest.raises(QueryNodeError, match="research_synthesize_parse_error"),
    ):
        await node_research_synthesize(state, _make_config(llm, db))


@pytest.mark.asyncio
async def test_node_research_synthesize_llm_exception_raises_query_node_error() -> None:
    """LLM raises generic exception → QueryNodeError with llm error."""
    state = _make_state(all_retrieved_chunks=[_make_chunk()])
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(side_effect=RuntimeError("model timeout"))

    with (
        patch(
            "src.graphs.research_graph.nodes.node_research_synthesize._load_prompt",
            return_value=FAKE_SYNTHESIZE_PROMPT,
        ),
        pytest.raises(QueryNodeError, match="research_synthesize_llm_error"),
    ):
        await node_research_synthesize(state, _make_config(llm, db))


@pytest.mark.asyncio
async def test_node_research_synthesize_model_not_found_raises_before_llm_call() -> None:
    """DB returns None for model record → QueryNodeError; LLM never called."""
    state = _make_state(all_retrieved_chunks=[_make_chunk()])

    db = AsyncMock()
    db_result = MagicMock()
    db_result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=db_result)

    llm = AsyncMock()

    with pytest.raises(QueryNodeError, match="LLM model not found"):
        await node_research_synthesize(state, _make_config(llm, db))

    llm.chat_completion.assert_not_called()


@pytest.mark.asyncio
async def test_node_research_synthesize_empty_chunks_uses_no_context_placeholder() -> None:
    """Empty all_retrieved_chunks → LLM prompt contains the no-context placeholder."""
    state = _make_state(all_retrieved_chunks=[])

    llm_json = json.dumps(
        {
            "answer": "Na podstawie dostępnych dokumentów nie znaleziono odpowiedzi.",
            "citations": [],
        }
    )
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(llm_json))

    with patch(
        "src.graphs.research_graph.nodes.node_research_synthesize._load_prompt",
        return_value=FAKE_SYNTHESIZE_PROMPT,
    ):
        result = await node_research_synthesize(state, _make_config(llm, db))

    call_kwargs = llm.chat_completion.call_args.kwargs
    prompt_content = call_kwargs["messages"][0]["content"]
    assert "(brak kontekstu)" in prompt_content
    # Answer is still set from LLM response
    assert result["final_answer"] != ""


@pytest.mark.asyncio
async def test_node_research_synthesize_out_of_range_citation_skipped() -> None:
    """Citation with chunk_index out of range for the accumulated chunks is silently dropped."""
    chunks = [_make_chunk(document_id=DOCUMENT_ID_A)]
    state = _make_state(all_retrieved_chunks=chunks)

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

    with patch(
        "src.graphs.research_graph.nodes.node_research_synthesize._load_prompt",
        return_value=FAKE_SYNTHESIZE_PROMPT,
    ):
        result = await node_research_synthesize(state, _make_config(llm, db))

    # Only the valid citation at index 1 survives
    assert len(result["citations"]) == 1
    assert result["citations"][0]["document_id"] == DOCUMENT_ID_A


@pytest.mark.asyncio
async def test_node_research_synthesize_missing_usage_defaults_token_counts_to_zero() -> None:
    """When response.usage is None, token counts default to 0."""
    state = _make_state(all_retrieved_chunks=[_make_chunk()])

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

    with patch(
        "src.graphs.research_graph.nodes.node_research_synthesize._load_prompt",
        return_value=FAKE_SYNTHESIZE_PROMPT,
    ):
        result = await node_research_synthesize(state, _make_config(llm, db))

    assert result["prompt_tokens"] == 0
    assert result["completion_tokens"] == 0

"""Unit tests for node_rewrite_query.

Tests cover:
- Normal rewrite: LLM returns valid JSON with rewritten_query → returned in result
- Invalid JSON from LLM → falls back to original state.question
- LLM returns JSON without rewritten_query key (empty string) → falls back to original question
- LLM raises a generic exception → falls back to original question (NOT QueryNodeError)
- Model not found in DB (scalar_one_or_none returns None) → QueryNodeError raised
- Conversation history is formatted into prompt (verified via LLM call args)
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.exceptions import QueryNodeError
from src.graphs.query_graph.nodes.node_rewrite_query import node_rewrite_query
from src.graphs.query_graph.state import QueryState

TENANT_ID = uuid.uuid4()
LLM_MODEL_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()
PIPELINE_ID = uuid.uuid4()


def _make_state(
    question: str = "Jakie dokumenty potrzebne są do wizyty?",
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
        conversation_history=conversation_history or [],
    )


def _make_model_record() -> MagicMock:
    record = MagicMock()
    record.id = LLM_MODEL_ID
    record.model_id = "llama3.2"
    record.endpoint_url = "http://ollama:11434/v1"
    return record


def _make_llm_response(rewritten_query: str) -> MagicMock:
    content = json.dumps({"rewritten_query": rewritten_query})
    choice = MagicMock()
    choice.message.content = content
    response = MagicMock()
    response.choices = [choice]
    return response


def _make_config(llm: object, db: object) -> dict:
    return {"configurable": {"llm": llm, "db": db}}


def _make_db(model_record: object) -> AsyncMock:
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = model_record
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.mark.asyncio
async def test_valid_rewrite_returned_in_result() -> None:
    """LLM returns valid JSON with rewritten_query → node returns that rewritten query."""
    model_record = _make_model_record()
    db = _make_db(model_record)
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(
        return_value=_make_llm_response("dokumenty wymagane wizyta lekarska rejestracja poradnia")
    )

    state = _make_state("Jakie dokumenty potrzebne są do wizyty?")
    result = await node_rewrite_query(state, _make_config(llm, db))

    assert result["rewritten_query"] == "dokumenty wymagane wizyta lekarska rejestracja poradnia"


@pytest.mark.asyncio
async def test_invalid_json_falls_back_to_original_question() -> None:
    """LLM returns non-JSON text → node falls back to the original question."""
    model_record = _make_model_record()
    db = _make_db(model_record)

    choice = MagicMock()
    choice.message.content = "This is definitely not JSON { broken"
    response = MagicMock()
    response.choices = [choice]

    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=response)

    state = _make_state("Jakie dokumenty potrzebne są do wizyty?")
    result = await node_rewrite_query(state, _make_config(llm, db))

    assert result["rewritten_query"] == state.question


@pytest.mark.asyncio
async def test_json_missing_rewritten_query_key_falls_back_to_original_question() -> None:
    """LLM returns valid JSON but without the rewritten_query key → falls back to original."""
    model_record = _make_model_record()
    db = _make_db(model_record)

    choice = MagicMock()
    choice.message.content = json.dumps({"some_other_key": "some value"})
    response = MagicMock()
    response.choices = [choice]

    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=response)

    state = _make_state("Jakie dokumenty potrzebne są do wizyty?")
    result = await node_rewrite_query(state, _make_config(llm, db))

    assert result["rewritten_query"] == state.question


@pytest.mark.asyncio
async def test_json_with_empty_rewritten_query_falls_back_to_original_question() -> None:
    """LLM returns JSON with rewritten_query set to empty string → falls back to original."""
    model_record = _make_model_record()
    db = _make_db(model_record)

    choice = MagicMock()
    choice.message.content = json.dumps({"rewritten_query": "   "})
    response = MagicMock()
    response.choices = [choice]

    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=response)

    state = _make_state("Jakie dokumenty potrzebne są do wizyty?")
    result = await node_rewrite_query(state, _make_config(llm, db))

    assert result["rewritten_query"] == state.question


@pytest.mark.asyncio
async def test_generic_llm_exception_falls_back_to_original_question() -> None:
    """A runtime exception from chat_completion → node falls back gracefully, no raise."""
    model_record = _make_model_record()
    db = _make_db(model_record)

    llm = AsyncMock()
    llm.chat_completion = AsyncMock(side_effect=RuntimeError("connection timeout"))

    state = _make_state("Jakie dokumenty potrzebne są do wizyty?")
    result = await node_rewrite_query(state, _make_config(llm, db))

    assert result["rewritten_query"] == state.question


@pytest.mark.asyncio
async def test_generic_llm_exception_does_not_raise_query_node_error() -> None:
    """A plain exception from LLM must NOT propagate as QueryNodeError — pipeline continues."""
    model_record = _make_model_record()
    db = _make_db(model_record)

    llm = AsyncMock()
    llm.chat_completion = AsyncMock(side_effect=OSError("network unreachable"))

    state = _make_state("Jakie dokumenty potrzebne są do wizyty?")
    # Must not raise anything — just fall back
    result = await node_rewrite_query(state, _make_config(llm, db))
    assert "rewritten_query" in result


@pytest.mark.asyncio
async def test_model_not_found_raises_query_node_error() -> None:
    """When DB returns None for the model record, QueryNodeError must be raised."""
    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result_mock)

    llm = AsyncMock()
    state = _make_state()

    with pytest.raises(QueryNodeError, match="LLM model not found"):
        await node_rewrite_query(state, _make_config(llm, db))


@pytest.mark.asyncio
async def test_model_not_found_error_message_contains_model_id() -> None:
    """QueryNodeError message includes the missing llm_model_id for traceability."""
    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result_mock)

    llm = AsyncMock()
    state = _make_state()

    with pytest.raises(QueryNodeError, match=str(state.llm_model_id)):
        await node_rewrite_query(state, _make_config(llm, db))


@pytest.mark.asyncio
async def test_conversation_history_included_in_prompt() -> None:
    """Conversation history is formatted and injected into the system prompt sent to LLM."""
    model_record = _make_model_record()
    db = _make_db(model_record)

    llm = AsyncMock()
    llm.chat_completion = AsyncMock(
        return_value=_make_llm_response("dokumenty przed operacją zabiegu chirurgicznego")
    )

    history = [
        {"role": "user", "content": "mam zaplanowaną operację"},
        {"role": "assistant", "content": "Rozumiem, postaram się pomóc."},
    ]
    state = _make_state(
        question="jakie dokumenty muszę dostarczyć?",
        conversation_history=history,
    )

    await node_rewrite_query(state, _make_config(llm, db))

    llm.chat_completion.assert_awaited_once()
    call_kwargs = llm.chat_completion.call_args
    messages = call_kwargs.kwargs.get("messages") or call_kwargs.args[1]
    system_prompt = messages[0]["content"]

    assert "user: mam zaplanowaną operację" in system_prompt
    assert "assistant: Rozumiem, postaram się pomóc." in system_prompt


@pytest.mark.asyncio
async def test_empty_conversation_history_uses_placeholder() -> None:
    """Empty history is rendered as the '(brak historii)' placeholder in the prompt."""
    model_record = _make_model_record()
    db = _make_db(model_record)

    llm = AsyncMock()
    llm.chat_completion = AsyncMock(
        return_value=_make_llm_response("godziny otwarcia poradni kardiologicznej")
    )

    state = _make_state(
        question="Jakie są godziny pracy poradni kardiologicznej?",
        conversation_history=[],
    )

    await node_rewrite_query(state, _make_config(llm, db))

    call_kwargs = llm.chat_completion.call_args
    messages = call_kwargs.kwargs.get("messages") or call_kwargs.args[1]
    system_prompt = messages[0]["content"]

    assert "(brak historii)" in system_prompt


@pytest.mark.asyncio
async def test_user_question_included_in_prompt() -> None:
    """The original question is substituted into the {{USER_QUESTION}} placeholder."""
    model_record = _make_model_record()
    db = _make_db(model_record)

    llm = AsyncMock()
    llm.chat_completion = AsyncMock(
        return_value=_make_llm_response("skierowanie do poradni specjalistycznej NFZ")
    )

    question = "Czy potrzebuję skierowania do specjalisty?"
    state = _make_state(question=question)

    await node_rewrite_query(state, _make_config(llm, db))

    call_kwargs = llm.chat_completion.call_args
    messages = call_kwargs.kwargs.get("messages") or call_kwargs.args[1]
    system_prompt = messages[0]["content"]

    assert question in system_prompt


@pytest.mark.asyncio
async def test_llm_called_with_correct_model_and_endpoint() -> None:
    """The node passes model_id and endpoint_url from the DB record to the LLM client."""
    model_record = _make_model_record()
    model_record.model_id = "mistral-7b"
    model_record.endpoint_url = "http://vllm:8000/v1"
    db = _make_db(model_record)

    llm = AsyncMock()
    llm.chat_completion = AsyncMock(
        return_value=_make_llm_response("procedury rejestracji pacjenta")
    )

    state = _make_state()
    await node_rewrite_query(state, _make_config(llm, db))

    llm.chat_completion.assert_awaited_once()
    call_kwargs = llm.chat_completion.call_args
    assert call_kwargs.kwargs.get("model") == "mistral-7b"
    assert call_kwargs.kwargs.get("base_url") == "http://vllm:8000/v1"


@pytest.mark.asyncio
async def test_llm_called_with_json_response_format() -> None:
    """The node requests structured JSON output from the LLM."""
    model_record = _make_model_record()
    db = _make_db(model_record)

    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response("procedury przyjęcia pacjenta"))

    state = _make_state()
    await node_rewrite_query(state, _make_config(llm, db))

    call_kwargs = llm.chat_completion.call_args
    assert call_kwargs.kwargs.get("response_format") == {"type": "json_object"}
    assert call_kwargs.kwargs.get("temperature") == 0.0


@pytest.mark.asyncio
async def test_result_dict_contains_only_rewritten_query_key() -> None:
    """Node returns a dict with exactly the rewritten_query key (no extra side-effects)."""
    model_record = _make_model_record()
    db = _make_db(model_record)

    llm = AsyncMock()
    llm.chat_completion = AsyncMock(
        return_value=_make_llm_response("dokumenty rejestracja pacjent NFZ")
    )

    state = _make_state()
    result = await node_rewrite_query(state, _make_config(llm, db))

    assert set(result.keys()) == {"rewritten_query"}


@pytest.mark.asyncio
async def test_rewritten_query_is_stripped_of_whitespace() -> None:
    """Leading/trailing whitespace in the LLM rewritten_query value is stripped."""
    model_record = _make_model_record()
    db = _make_db(model_record)

    choice = MagicMock()
    choice.message.content = json.dumps({"rewritten_query": "  dokumenty wymagane wizyta  "})
    response = MagicMock()
    response.choices = [choice]

    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=response)

    state = _make_state()
    result = await node_rewrite_query(state, _make_config(llm, db))

    assert result["rewritten_query"] == "dokumenty wymagane wizyta"

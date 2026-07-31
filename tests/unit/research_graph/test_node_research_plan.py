"""Unit tests for node_research_plan.

Tests cover:
- Normal flow: LLM returns valid JSON, new iteration is appended with correct fields
- sufficient=True in LLM response → iteration has sufficient=True
- LLM returns invalid JSON → QueryNodeError with parse error
- LLM raises generic exception → QueryNodeError with llm error
- Model not found in DB → QueryNodeError before LLM call
- steps_taken is set to the current step index
- sub_query and reasoning are stored on the iteration (GDPR: never logged, but stored)
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.exceptions import QueryNodeError
from src.graphs.research_graph.nodes.node_research_plan import node_research_plan
from src.graphs.research_graph.state import ResearchIteration, ResearchState

TENANT_ID = uuid.uuid4()
PIPELINE_ID = uuid.uuid4()
LLM_MODEL_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()

FAKE_PLAN_PROMPT = (
    "Plan a sub-query for: {{ORIGINAL_QUESTION}}\n"
    "Previous: {{PREVIOUS_ITERATIONS}}\n"
    "Step: {{CURRENT_STEP}} / {{MAX_STEPS}}\n"
    'Return JSON {"sub_query": ..., "reasoning": ..., "sufficient": ...}'
)


def _make_state(
    iterations: list[ResearchIteration] | None = None,
    max_steps: int = 3,
) -> ResearchState:
    return ResearchState(
        original_question="Jakie jest leczenie cukrzycy typu 2 z CKD?",
        pipeline_id=PIPELINE_ID,
        tenant_id=TENANT_ID,
        collection_ids=[COLLECTION_ID],
        allowed_collection_ids=[COLLECTION_ID],
        llm_model_id=LLM_MODEL_ID,
        iterations=iterations or [],
        max_steps=max_steps,
    )


def _make_model_record(model_id: str = "llama3.2") -> MagicMock:
    record = MagicMock()
    record.id = LLM_MODEL_ID
    record.model_id = model_id
    record.endpoint_url = "http://ollama:11434/v1"
    return record


def _make_llm_response(content: str) -> MagicMock:
    choice = MagicMock()
    choice.message.content = content
    response = MagicMock()
    response.choices = [choice]
    usage = MagicMock()
    usage.prompt_tokens = 100
    usage.completion_tokens = 40
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
async def test_node_research_plan_appends_new_iteration() -> None:
    """Normal flow: LLM returns valid JSON → new iteration appended to state.iterations."""
    state = _make_state()
    model_record = _make_model_record()
    db = _make_db(model_record)

    plan_json = json.dumps(
        {
            "sub_query": "metformin CKD stage 3 dose adjustment",
            "reasoning": "Need to find dose adjustment guidelines for metformin in CKD.",
            "sufficient": False,
        }
    )
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(plan_json))

    with patch(
        "src.graphs.research_graph.nodes.node_research_plan._load_prompt",
        return_value=FAKE_PLAN_PROMPT,
    ):
        result = await node_research_plan(state, _make_config(llm, db))

    iterations = result["iterations"]
    assert len(iterations) == 1
    assert iterations[0].step == 1
    assert iterations[0].query == "metformin CKD stage 3 dose adjustment"
    assert iterations[0].sufficient is False
    assert iterations[0].reasoning != ""
    assert result["steps_taken"] == 1


@pytest.mark.asyncio
async def test_node_research_plan_sufficient_true_is_stored() -> None:
    """LLM returns sufficient=True → iteration.sufficient is True."""
    state = _make_state()
    model_record = _make_model_record()
    db = _make_db(model_record)

    plan_json = json.dumps(
        {
            "sub_query": "type 2 diabetes treatment options",
            "reasoning": "Enough evidence has been accumulated.",
            "sufficient": True,
        }
    )
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(plan_json))

    with patch(
        "src.graphs.research_graph.nodes.node_research_plan._load_prompt",
        return_value=FAKE_PLAN_PROMPT,
    ):
        result = await node_research_plan(state, _make_config(llm, db))

    assert result["iterations"][0].sufficient is True


@pytest.mark.asyncio
async def test_node_research_plan_subsequent_step_increments_step_counter() -> None:
    """Second call appends iteration with step=2 and steps_taken=2."""
    existing_iter = ResearchIteration(
        step=1,
        query="first sub-query",
        retrieved_chunks=[],
        reasoning="first reasoning",
        sufficient=False,
    )
    state = _make_state(iterations=[existing_iter])
    model_record = _make_model_record()
    db = _make_db(model_record)

    plan_json = json.dumps(
        {
            "sub_query": "drug interactions metformin",
            "reasoning": "Still need drug interaction data.",
            "sufficient": False,
        }
    )
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(plan_json))

    with patch(
        "src.graphs.research_graph.nodes.node_research_plan._load_prompt",
        return_value=FAKE_PLAN_PROMPT,
    ):
        result = await node_research_plan(state, _make_config(llm, db))

    assert len(result["iterations"]) == 2
    assert result["iterations"][1].step == 2
    assert result["steps_taken"] == 2


@pytest.mark.asyncio
async def test_node_research_plan_invalid_json_raises_query_node_error() -> None:
    """LLM returns non-JSON → QueryNodeError with parse error."""
    state = _make_state()
    model_record = _make_model_record()
    db = _make_db(model_record)

    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response("This is not JSON {broken"))

    with (
        patch(
            "src.graphs.research_graph.nodes.node_research_plan._load_prompt",
            return_value=FAKE_PLAN_PROMPT,
        ),
        pytest.raises(QueryNodeError, match="research_plan_parse_error"),
    ):
        await node_research_plan(state, _make_config(llm, db))


@pytest.mark.asyncio
async def test_node_research_plan_llm_exception_raises_query_node_error() -> None:
    """LLM raises generic exception → QueryNodeError with llm error."""
    state = _make_state()
    model_record = _make_model_record()
    db = _make_db(model_record)

    llm = AsyncMock()
    llm.chat_completion = AsyncMock(side_effect=RuntimeError("ollama unreachable"))

    with (
        patch(
            "src.graphs.research_graph.nodes.node_research_plan._load_prompt",
            return_value=FAKE_PLAN_PROMPT,
        ),
        pytest.raises(QueryNodeError, match="research_plan_llm_error"),
    ):
        await node_research_plan(state, _make_config(llm, db))


@pytest.mark.asyncio
async def test_node_research_plan_model_not_found_raises_before_llm_call() -> None:
    """DB returns None for model record → QueryNodeError; LLM never called."""
    state = _make_state()

    db = AsyncMock()
    db_result = MagicMock()
    db_result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=db_result)

    llm = AsyncMock()

    with pytest.raises(QueryNodeError, match="LLM model not found"):
        await node_research_plan(state, _make_config(llm, db))

    llm.chat_completion.assert_not_called()


@pytest.mark.asyncio
async def test_node_research_plan_preserves_existing_iterations() -> None:
    """Existing iterations are kept; only the new one is appended at the end."""
    existing = [
        ResearchIteration(
            step=1, query="q1", retrieved_chunks=[], reasoning="r1", sufficient=False
        ),
        ResearchIteration(
            step=2, query="q2", retrieved_chunks=[], reasoning="r2", sufficient=False
        ),
    ]
    state = _make_state(iterations=existing)
    model_record = _make_model_record()
    db = _make_db(model_record)

    plan_json = json.dumps({"sub_query": "q3", "reasoning": "r3", "sufficient": False})
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(plan_json))

    with patch(
        "src.graphs.research_graph.nodes.node_research_plan._load_prompt",
        return_value=FAKE_PLAN_PROMPT,
    ):
        result = await node_research_plan(state, _make_config(llm, db))

    assert len(result["iterations"]) == 3
    assert result["iterations"][0].query == "q1"
    assert result["iterations"][1].query == "q2"
    assert result["iterations"][2].query == "q3"

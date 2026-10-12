"""Unit tests for research graph entry-point constraints (invoke_research_graph).

Tests cover:
- Multi-collection pipeline raises QueryNodeError (BLOCKER-2 / ADR-026 constraint)
- Single-collection pipeline passes the constraint check
- TenantIsolationError raised when pipeline collections not in allowed_collection_ids
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.exceptions import QueryNodeError, TenantIsolationError
from src.domain.auth import UserContext
from src.graphs.research_graph.graph import invoke_research_graph

TENANT_ID = uuid.uuid4()
USER_ID = uuid.uuid4()
PIPELINE_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()
LLM_MODEL_ID = uuid.uuid4()


def _make_pipeline(collection_ids: list[uuid.UUID]) -> MagicMock:
    pipeline = MagicMock()
    pipeline.id = PIPELINE_ID
    pipeline.llm_model_id = LLM_MODEL_ID
    pipeline.collection_ids = collection_ids
    pipeline.prompt_config = {}
    return pipeline


def _make_ctx(allowed_collection_ids: frozenset[uuid.UUID] = frozenset()) -> UserContext:
    return UserContext(
        user_id=USER_ID,
        keycloak_sub="kc-user",
        email="admin@example.com",
        display_name="Admin",
        tenant_id=TENANT_ID,
        roles=frozenset({"admin"}),
        permissions=frozenset({"chat:query"}),
        # Empty allowed_collection_ids = admin (no restriction)
        allowed_collection_ids=allowed_collection_ids,
        writable_collection_ids=frozenset(),
    )


@pytest.mark.asyncio
async def test_multi_collection_pipeline_raises_query_node_error() -> None:
    """Pipeline with 2 collection_ids raises QueryNodeError before invoking the graph."""
    pipeline = _make_pipeline(collection_ids=[uuid.uuid4(), uuid.uuid4()])
    ctx = _make_ctx()

    with pytest.raises(QueryNodeError, match="exactly one collection_id"):
        await invoke_research_graph(
            question="test question",
            pipeline=pipeline,
            ctx=ctx,
            db=AsyncMock(),
            llm=MagicMock(),
            retrieval=MagicMock(),
            conversation_history=[],
        )


@pytest.mark.asyncio
async def test_zero_collection_pipeline_raises_query_node_error() -> None:
    """Pipeline with 0 collection_ids raises QueryNodeError."""
    pipeline = _make_pipeline(collection_ids=[])
    ctx = _make_ctx()

    with pytest.raises(QueryNodeError, match="exactly one collection_id"):
        await invoke_research_graph(
            question="test question",
            pipeline=pipeline,
            ctx=ctx,
            db=AsyncMock(),
            llm=MagicMock(),
            retrieval=MagicMock(),
            conversation_history=[],
        )


@pytest.mark.asyncio
async def test_unauthorized_collection_raises_tenant_isolation_error() -> None:
    """Non-admin user with collection not in allowed_collection_ids raises TenantIsolationError.

    The TenantIsolationError check runs before the single-collection constraint,
    so a forbidden collection raises TenantIsolationError, not QueryNodeError.
    """
    forbidden_collection = uuid.uuid4()
    allowed_collection = uuid.uuid4()
    pipeline = _make_pipeline(collection_ids=[forbidden_collection])
    ctx = _make_ctx(allowed_collection_ids=frozenset({allowed_collection}))

    with pytest.raises(TenantIsolationError):
        await invoke_research_graph(
            question="test question",
            pipeline=pipeline,
            ctx=ctx,
            db=AsyncMock(),
            llm=MagicMock(),
            retrieval=MagicMock(),
            conversation_history=[],
        )


@pytest.mark.asyncio
async def test_single_collection_passes_constraint_and_runs_graph() -> None:
    """Single-collection pipeline passes the constraint; graph is compiled and invoked."""
    pipeline = _make_pipeline(collection_ids=[COLLECTION_ID])
    ctx = _make_ctx()

    mock_graph = AsyncMock()
    mock_graph.ainvoke = AsyncMock(
        return_value={
            "final_answer": "Research answer.",
            "citations": [],
            "prompt_tokens": 100,
            "completion_tokens": 40,
            "steps_taken": 1,
            "all_retrieved_chunks": [],
        }
    )

    with patch(
        "src.graphs.research_graph.graph.build_research_graph", return_value=mock_graph
    ):
        answer, citations, prompt_tokens, completion_tokens = await invoke_research_graph(
            question="complex medical question",
            pipeline=pipeline,
            ctx=ctx,
            db=AsyncMock(),
            llm=MagicMock(),
            retrieval=MagicMock(),
            conversation_history=[],
        )

    assert answer == "Research answer."
    assert citations == []
    mock_graph.ainvoke.assert_called_once()

"""Unit tests for ChatService research_mode dispatch (ADR-026).

Tests cover:
- research_mode=False → _invoke_graph called, _invoke_research_graph not called
- research_mode=True → _invoke_research_graph called, _invoke_graph not called
- cache hit with research_mode=True → cache is bypassed, research graph still invoked
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.domain.chat import ChatService

TENANT_ID = uuid.uuid4()
USER_ID = uuid.uuid4()
PIPELINE_ID = uuid.uuid4()
CONV_ID = uuid.uuid4()
LLM_MODEL_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()
MESSAGE_ID = uuid.uuid4()


def _make_pipeline(research_mode: bool = False, cache_responses: bool = False) -> MagicMock:
    pipeline = MagicMock()
    pipeline.id = PIPELINE_ID
    pipeline.llm_model_id = LLM_MODEL_ID
    pipeline.collection_ids = [COLLECTION_ID]
    pipeline.prompt_config = {
        "research_mode": research_mode,
        "cache_responses": cache_responses,
    }
    pipeline.guardrails = {}
    return pipeline


def _make_conversation() -> MagicMock:
    conv = MagicMock()
    conv.id = CONV_ID
    conv.tenant_id = TENANT_ID
    conv.user_id = USER_ID
    conv.is_deleted = False
    return conv


def _make_db() -> AsyncMock:
    db = AsyncMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    # _load_conversation_history uses scalars().all()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = []
    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock
    db.execute = AsyncMock(return_value=result_mock)
    return db


def _make_ctx() -> MagicMock:
    from src.domain.auth import UserContext

    return UserContext(
        user_id=USER_ID,
        keycloak_sub="kc-user",
        email="user@example.com",
        display_name="User",
        tenant_id=TENANT_ID,
        roles=frozenset({"viewer"}),
        permissions=frozenset({"chat:query"}),
        allowed_collection_ids=frozenset({COLLECTION_ID}),
        writable_collection_ids=frozenset(),
    )


def _make_messages() -> list:
    from src.domain.schemas.chat import ChatMessage

    return [ChatMessage(role="user", content="Jakie są interakcje metforminy z ibuprofenem?")]


@pytest.mark.asyncio
async def test_standard_pipeline_dispatches_to_query_graph() -> None:
    """research_mode=False → _invoke_graph is called; _invoke_research_graph is not."""
    svc = ChatService()
    db = _make_db()
    ctx = _make_ctx()
    pipeline = _make_pipeline(research_mode=False)
    conversation = _make_conversation()

    with (
        patch.object(
            svc,
            "_invoke_graph",
            new_callable=AsyncMock,
            return_value=("answer", [], False, 100, 50),
        ) as mock_graph,
        patch.object(
            svc,
            "_invoke_research_graph",
            new_callable=AsyncMock,
        ) as mock_research,
        patch.object(
            ChatService,
            "_build_llm_client",
            return_value=MagicMock(),
        ),
        patch.object(
            ChatService,
            "_build_retrieval_service",
            new_callable=AsyncMock,
            return_value=MagicMock(),
        ),
    ):
        await svc.complete(
            db=db,
            ctx=ctx,
            pipeline=pipeline,
            conversation=conversation,
            messages=_make_messages(),
        )

    mock_graph.assert_called_once()
    mock_research.assert_not_called()


@pytest.mark.asyncio
async def test_research_pipeline_dispatches_to_research_graph() -> None:
    """research_mode=True → _invoke_research_graph is called; _invoke_graph is not."""
    svc = ChatService()
    db = _make_db()
    ctx = _make_ctx()
    pipeline = _make_pipeline(research_mode=True)
    conversation = _make_conversation()

    with (
        patch.object(
            svc,
            "_invoke_graph",
            new_callable=AsyncMock,
        ) as mock_graph,
        patch.object(
            svc,
            "_invoke_research_graph",
            new_callable=AsyncMock,
            return_value=("research answer", [], 200, 80),
        ) as mock_research,
        patch.object(
            ChatService,
            "_build_llm_client",
            return_value=MagicMock(),
        ),
        patch.object(
            ChatService,
            "_build_retrieval_service",
            new_callable=AsyncMock,
            return_value=MagicMock(),
        ),
    ):
        await svc.complete(
            db=db,
            ctx=ctx,
            pipeline=pipeline,
            conversation=conversation,
            messages=_make_messages(),
        )

    mock_research.assert_called_once()
    mock_graph.assert_not_called()


@pytest.mark.asyncio
async def test_research_mode_bypasses_cache_even_when_cache_enabled() -> None:
    """research_mode=True with cache_responses=True → cache is NOT consulted."""
    svc = ChatService()
    db = _make_db()
    ctx = _make_ctx()
    # Both research_mode and cache_responses are True
    pipeline = _make_pipeline(research_mode=True, cache_responses=True)
    conversation = _make_conversation()

    with (
        patch.object(
            svc,
            "_invoke_graph",
            new_callable=AsyncMock,
        ) as mock_graph,
        patch.object(
            svc,
            "_invoke_research_graph",
            new_callable=AsyncMock,
            return_value=("research answer", [], 200, 80),
        ) as mock_research,
        patch.object(
            ChatService,
            "_build_llm_client",
            return_value=MagicMock(),
        ),
        patch.object(
            ChatService,
            "_build_retrieval_service",
            new_callable=AsyncMock,
            return_value=MagicMock(),
        ),
        # Patch the cache module import to confirm it is never reached
        patch("src.core.cache.get_rag_cache") as mock_cache_factory,
    ):
        result = await svc.complete(
            db=db,
            ctx=ctx,
            pipeline=pipeline,
            conversation=conversation,
            messages=_make_messages(),
        )

    # Cache factory must NOT be called for research-mode pipelines
    mock_cache_factory.assert_not_called()
    # Research graph must be invoked despite cache being "enabled"
    mock_research.assert_called_once()
    mock_graph.assert_not_called()
    assert result.answer == "research answer"

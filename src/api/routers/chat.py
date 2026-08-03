"""Chat router — OpenAI-compatible completions and model listing.

GET  /v1/models             — list active RAG pipelines as OpenAI models
POST /v1/chat/completions   — run RAG query graph

Security:
- Requires `chat:query` permission.
- `model` field resolves to rag_pipelines.name within the user's tenant only.
- Message content is never logged.
- LLMUnavailableError → HTTP 503 with Retry-After: 60.
- Stub model "stub-rag" is served when no pipelines are seeded in DB (dev/test only).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from fastapi_limiter.depends import RateLimiter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies.auth import get_current_ctx, require_permission
from src.api.dependencies.pipelines import resolve_pipeline_by_model
from src.api.schemas.chat import (
    ChatCompletionChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatCompletionUsage,
    ChatMessage,
)
from src.core.clients.redis_client import get_redis_client
from src.core.database import get_db_session
from src.core.exceptions import LLMUnavailableError
from src.db.models.rag_pipeline import RagPipeline
from src.domain.auth import UserContext
from src.domain.chat import _STUB_ANSWER, ChatService, CompletionResult

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["chat"])

_require_chat = require_permission("chat:query")
_rate_limiter = RateLimiter(times=20, seconds=60)

# Shown in GET /v1/models when no pipelines are seeded. Allows Open WebUI to start
# without any DB seed data; removed once real pipelines are configured.
_STUB_MODEL_ID = "stub-rag"
_STUB_MODEL_ENTRY = {
    "id": _STUB_MODEL_ID,
    "object": "model",
    "owned_by": "system",
    "created": 0,
}


def _get_service() -> ChatService:
    return ChatService()


@router.get("/models", summary="List RAG pipelines as OpenAI-compatible models")
async def list_models(
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _: Annotated[None, Depends(_require_chat)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, object]:
    """Return active pipelines for this tenant. Falls back to a stub entry in dev."""
    rows = list(
        await session.scalars(
            select(RagPipeline).where(
                RagPipeline.tenant_id == ctx.tenant_id,
                RagPipeline.is_active == True,  # noqa: E712
            )
        )
    )
    data = [
        {
            "id": p.name,
            "object": "model",
            "owned_by": str(p.tenant_id),
            "created": int(p.created_at.timestamp()),
        }
        for p in rows
    ]
    if not data:
        data = [_STUB_MODEL_ENTRY]

    return {"object": "list", "data": data}


@router.post(
    "/chat/completions",
    response_model=None,  # Union with StreamingResponse not representable as a Pydantic model
    summary="OpenAI-compatible chat completion (LangGraph RAG query graph)",
    dependencies=[Depends(_rate_limiter)],
)
async def chat_completions(
    body: ChatCompletionRequest,
    request: Request,  # noqa: ARG001 — reserved for future rate-limiting / IP logging
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _: Annotated[None, Depends(_require_chat)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ChatCompletionResponse | StreamingResponse:
    # ── Stub mode: model == "stub-rag" → no DB, no pipeline resolution ───────
    if body.model == _STUB_MODEL_ID:
        return await _stub_response(body)

    # ── Real mode ─────────────────────────────────────────────────────────────
    # Check monthly query quota before doing any expensive work
    from src.domain.quota_service import QuotaService

    await QuotaService(session, get_redis_client()).check_query(ctx.tenant_id)

    # resolve_pipeline_by_model raises HTTPException(404) when the pipeline is not
    # found within this tenant — that propagates as-is to the client.
    pipeline = await resolve_pipeline_by_model(body.model, ctx, session)

    svc = _get_service()
    conversation_id = uuid.UUID(body.conversation_id) if body.conversation_id else None

    # ConversationNotFoundError (domain exception) propagates to the global handler
    # registered in exception_handlers.py → HTTP 404 {"detail": {"code": "CONVERSATION_NOT_FOUND"}}
    conversation = await svc.get_or_create_conversation(
        db=session,
        ctx=ctx,
        pipeline_id=pipeline.id,
        conversation_id=conversation_id,
    )

    if body.stream:
        return StreamingResponse(
            svc.stream_completion(
                db=session,
                ctx=ctx,
                pipeline=pipeline,
                conversation=conversation,
                messages=body.messages,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        result = await svc.complete(
            db=session,
            ctx=ctx,
            pipeline=pipeline,
            conversation=conversation,
            messages=body.messages,
        )
    except LLMUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "llm_unavailable", "message": str(exc)},
            headers={"Retry-After": "60"},
        ) from exc

    await session.commit()

    return _build_response(result, body.model, str(conversation.id))


# ── helpers ───────────────────────────────────────────────────────────────────


async def _stub_response(body: ChatCompletionRequest) -> ChatCompletionResponse | StreamingResponse:
    """Return a mock response without touching the DB or any real pipeline."""
    message_id = str(uuid.uuid4())
    conversation_id = body.conversation_id or str(uuid.uuid4())

    if body.stream:
        return StreamingResponse(
            _stub_stream(message_id, conversation_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return _build_response(
        CompletionResult(
            message_id=uuid.UUID(message_id),
            answer=_STUB_ANSWER,
            prompt_tokens=10,
            completion_tokens=len(_STUB_ANSWER.split()),
            citations=[],
        ),
        _STUB_MODEL_ID,
        conversation_id,
    )


async def _stub_stream(message_id: str, conversation_id: str) -> AsyncGenerator[str, None]:
    chunk = {
        "id": message_id,
        "object": "chat.completion.chunk",
        "choices": [
            {
                "delta": {"role": "assistant", "content": _STUB_ANSWER},
                "index": 0,
                "finish_reason": None,
            }
        ],
    }
    yield f"data: {json.dumps(chunk)}\n\n"

    final = {
        "id": message_id,
        "object": "chat.completion.chunk",
        "choices": [{"delta": {}, "index": 0, "finish_reason": "stop"}],
        "message_sources": [],
        "conversation_id": conversation_id,
    }
    yield f"data: {json.dumps(final)}\n\n"
    yield "data: [DONE]\n\n"


def _build_response(
    result: CompletionResult, model: str, conversation_id: str
) -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id=str(result.message_id),
        model=model,
        choices=[
            ChatCompletionChoice(
                message=ChatMessage(role="assistant", content=result.answer),
                finish_reason="stop",
            )
        ],
        usage=ChatCompletionUsage(
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            total_tokens=result.prompt_tokens + result.completion_tokens,
        ),
        message_sources=result.citations,
        conversation_id=conversation_id,
    )

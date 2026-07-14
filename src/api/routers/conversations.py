"""Conversations router — CRUD for conversation history.

GET    /conversations            — list current user's conversations (paginated)
POST   /conversations            — create a conversation linked to a pipeline
GET    /conversations/{id}       — detail with messages and sources
DELETE /conversations/{id}       — soft-delete (is_deleted=True) + audit log

Security:
- Filters by BOTH tenant_id AND user_id on every query.
- Soft-delete only — full message history preserved for audit.
- No message content in any log statement.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies.auth import get_current_ctx, require_permission
from src.api.dependencies.pagination import PaginationParams, get_pagination
from src.api.schemas.conversations import (
    ConversationCreate,
    ConversationDetailResponse,
    ConversationResponse,
    MessageResponse,
    MessageSourceResponse,
)
from src.core.database import get_db_session
from src.db.models.conversation import Conversation
from src.db.models.message import Message, MessageSource
from src.db.models.rag_pipeline import RagPipeline
from src.domain.audit_service import AuditService
from src.domain.auth import UserContext

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/conversations", tags=["conversations"])

_require_chat = require_permission("chat:query")


@router.post(
    "",
    response_model=ConversationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a conversation linked to a pipeline",
)
async def create_conversation(
    body: ConversationCreate,
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _: Annotated[None, Depends(_require_chat)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ConversationResponse:
    pipeline = await session.get(RagPipeline, body.pipeline_id)
    if pipeline is None or pipeline.tenant_id != ctx.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "PIPELINE_NOT_FOUND"},
        )

    conv = Conversation(
        id=uuid.uuid4(),
        tenant_id=ctx.tenant_id,
        user_id=ctx.user_id,
        pipeline_id=body.pipeline_id,
        title=body.title,
    )
    session.add(conv)
    await session.commit()
    await session.refresh(conv)

    logger.info("conversation_created", conversation_id=str(conv.id), tenant_id=str(ctx.tenant_id))
    return ConversationResponse.model_validate(conv)


@router.get(
    "",
    response_model=list[ConversationResponse],
    summary="List the current user's conversations",
)
async def list_conversations(
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _: Annotated[None, Depends(_require_chat)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    pagination: Annotated[PaginationParams, Depends(get_pagination)],
) -> list[ConversationResponse]:
    rows = list(
        await session.scalars(
            select(Conversation)
            .where(
                Conversation.tenant_id == ctx.tenant_id,
                Conversation.user_id == ctx.user_id,
                Conversation.is_deleted == False,  # noqa: E712
            )
            .order_by(Conversation.updated_at.desc())
            .offset(pagination.offset)
            .limit(pagination.page_size)
        )
    )
    return [ConversationResponse.model_validate(c) for c in rows]


@router.get(
    "/{conversation_id}",
    response_model=ConversationDetailResponse,
    summary="Get a conversation with messages and sources",
)
async def get_conversation(
    conversation_id: uuid.UUID,
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _: Annotated[None, Depends(_require_chat)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ConversationDetailResponse:
    conv = await session.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.tenant_id == ctx.tenant_id,
            Conversation.user_id == ctx.user_id,
            Conversation.is_deleted == False,  # noqa: E712
        )
    )
    if conv is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "CONVERSATION_NOT_FOUND"},
        )

    messages = list(
        await session.scalars(
            select(Message)
            .where(Message.conversation_id == conv.id)
            .order_by(Message.created_at.asc())
        )
    )

    sources: list[MessageSource] = []
    if messages:
        msg_ids = [m.id for m in messages]
        sources = list(
            await session.scalars(
                select(MessageSource).where(MessageSource.message_id.in_(msg_ids))
            )
        )

    sources_by_msg: dict[uuid.UUID, list[MessageSource]] = defaultdict(list)
    for src in sources:
        sources_by_msg[src.message_id].append(src)

    message_responses = [
        MessageResponse(
            id=m.id,
            role=m.role,
            content=m.content,
            model_id=m.model_id,
            prompt_tokens=m.prompt_tokens,
            completion_tokens=m.completion_tokens,
            latency_ms=m.latency_ms,
            created_at=m.created_at,
            sources=[MessageSourceResponse.model_validate(s) for s in sources_by_msg[m.id]],
        )
        for m in messages
    ]

    return ConversationDetailResponse(
        id=conv.id,
        tenant_id=conv.tenant_id,
        pipeline_id=conv.pipeline_id,
        title=conv.title,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
        messages=message_responses,
    )


@router.delete(
    "/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft-delete a conversation (audit log written)",
)
async def delete_conversation(
    conversation_id: uuid.UUID,
    request: Request,
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _: Annotated[None, Depends(_require_chat)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    # NOTE: scoped to tenant_id to prevent IDOR timing attacks
    conv = await session.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.tenant_id == ctx.tenant_id,
        )
    )
    if conv is None or conv.user_id != ctx.user_id or conv.is_deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "CONVERSATION_NOT_FOUND"},
        )

    conv.is_deleted = True

    ip = request.client.host if request.client else None
    await AuditService(session).log(
        ctx=ctx,
        action="conversation.deleted",
        resource_type="conversation",
        resource_id=conversation_id,
        details={},
        ip=ip,
    )
    await session.commit()
    logger.info("conversation_deleted", conversation_id=str(conversation_id))

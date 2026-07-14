"""Messages router — feedback on assistant messages.

POST /messages/{id}/feedback — submit or update thumbs up/down rating

Security:
- Verifies message belongs to user's tenant via conversation join (no IDOR).
- Unique constraint (message_id, user_id) — subsequent calls update rating.
- No message content in any log statement.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies.auth import get_current_ctx, require_permission
from src.api.schemas.conversations import FeedbackCreate, FeedbackResponse
from src.core.database import get_db_session
from src.db.models.conversation import Conversation
from src.db.models.feedback import Feedback
from src.db.models.message import Message
from src.domain.audit_service import AuditService
from src.domain.auth import UserContext

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/messages", tags=["messages"])

_require_chat = require_permission("chat:query")


@router.post(
    "/{message_id}/feedback",
    response_model=FeedbackResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit or update feedback for an assistant message",
)
async def submit_feedback(
    message_id: uuid.UUID,
    body: FeedbackCreate,
    request: Request,
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _: Annotated[None, Depends(_require_chat)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> FeedbackResponse:
    # Resource-level auth: verify message belongs to the user's tenant via conversation
    message = await session.scalar(
        select(Message)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(
            Message.id == message_id,
            Conversation.tenant_id == ctx.tenant_id,
        )
    )
    if message is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "MESSAGE_NOT_FOUND"},
        )

    # Upsert: unique constraint (message_id, user_id)
    existing = await session.scalar(
        select(Feedback).where(
            Feedback.message_id == message_id,
            Feedback.user_id == ctx.user_id,
        )
    )
    if existing is not None:
        existing.rating = body.rating
        existing.comment = body.comment
        feedback = existing
    else:
        feedback = Feedback(
            id=uuid.uuid4(),
            tenant_id=ctx.tenant_id,
            message_id=message_id,
            user_id=ctx.user_id,
            rating=body.rating,
            comment=body.comment,
        )
        session.add(feedback)

    await session.flush()

    ip = request.client.host if request.client else None
    await AuditService(session).log(
        ctx=ctx,
        action="message.feedback",
        resource_type="message",
        resource_id=message_id,
        details={"rating": body.rating},  # rating only — comment not in audit log
        ip=ip,
    )
    await session.commit()
    await session.refresh(feedback)

    logger.info(
        "feedback_submitted",
        message_id=str(message_id),
        tenant_id=str(ctx.tenant_id),
    )
    return FeedbackResponse.model_validate(feedback)

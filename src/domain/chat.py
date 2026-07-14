"""ChatService — orchestrates query graph invocation.

TASK-010 (LangGraph query graph) is not yet implemented.
`_invoke_graph()` is a stub that returns a placeholder until TASK-010 is merged.

Security rules:
- Message content MUST NOT appear in any log statement.
- Log conversation_id, message_id, pipeline_id only.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas.chat import ChatMessage, MessageSourceOut
from src.db.models.conversation import Conversation
from src.db.models.message import Message
from src.db.models.rag_pipeline import RagPipeline
from src.domain.auth import UserContext

logger = structlog.get_logger(__name__)

_STUB_ANSWER = (
    "The RAG pipeline is not yet configured. "
    "This is a stub response — full retrieval-augmented generation will be available "
    "once the ingest and retrieval stages (TASK-006 through TASK-010) are deployed."
)


@dataclass
class CompletionResult:
    message_id: uuid.UUID
    answer: str
    prompt_tokens: int
    completion_tokens: int
    citations: list[MessageSourceOut] = field(default_factory=list)


class ChatService:
    """Business logic for chat completions.

    The `_invoke_graph()` method is a stub. Replace it with a real LangGraph
    ainvoke call in TASK-010; the rest of the service stays unchanged.
    """

    async def get_or_create_conversation(
        self,
        *,
        db: AsyncSession,
        ctx: UserContext,
        pipeline_id: uuid.UUID,
        conversation_id: uuid.UUID | None,
    ) -> Conversation:
        if conversation_id is not None:
            from fastapi import HTTPException, status

            conv = await db.get(Conversation, conversation_id)
            if (
                conv is None
                or conv.tenant_id != ctx.tenant_id
                or conv.user_id != ctx.user_id
                or conv.is_deleted
            ):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={"code": "CONVERSATION_NOT_FOUND"},
                )
            return conv

        conv = Conversation(
            id=uuid.uuid4(),
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            pipeline_id=pipeline_id,
        )
        db.add(conv)
        await db.flush()
        logger.info(
            "conversation_created",
            conversation_id=str(conv.id),
            tenant_id=str(ctx.tenant_id),
            pipeline_id=str(pipeline_id),
        )
        return conv

    async def complete(
        self,
        *,
        db: AsyncSession,
        ctx: UserContext,
        pipeline: RagPipeline,
        conversation: Conversation,
        messages: list[ChatMessage],
    ) -> CompletionResult:
        question = self._extract_question(messages)

        user_msg = Message(
            id=uuid.uuid4(),
            conversation_id=conversation.id,
            role="user",
            content=question,
        )
        db.add(user_msg)

        # Stub: replaced by real graph invocation in TASK-010
        answer = await self._invoke_graph(question, pipeline, ctx)

        message_id = uuid.uuid4()
        assistant_msg = Message(
            id=message_id,
            conversation_id=conversation.id,
            role="assistant",
            content=answer,
            prompt_tokens=len(question.split()),
            completion_tokens=len(answer.split()),
        )
        db.add(assistant_msg)
        await db.flush()

        # NOTE: never log question/answer content — only identifiers
        logger.info(
            "chat_completion",
            conversation_id=str(conversation.id),
            message_id=str(message_id),
            pipeline_id=str(pipeline.id),
        )

        return CompletionResult(
            message_id=message_id,
            answer=answer,
            prompt_tokens=len(question.split()),
            completion_tokens=len(answer.split()),
            citations=[],
        )

    async def stream_completion(
        self,
        *,
        db: AsyncSession,
        ctx: UserContext,
        pipeline: RagPipeline,
        conversation: Conversation,
        messages: list[ChatMessage],
    ) -> AsyncGenerator[str, None]:
        """Yield SSE-formatted chunks. Stub emits the full answer as one chunk."""
        result = await self.complete(
            db=db, ctx=ctx, pipeline=pipeline, conversation=conversation, messages=messages
        )

        content_chunk = {
            "id": str(result.message_id),
            "object": "chat.completion.chunk",
            "choices": [
                {
                    "delta": {"role": "assistant", "content": result.answer},
                    "index": 0,
                    "finish_reason": None,
                }
            ],
        }
        yield f"data: {json.dumps(content_chunk)}\n\n"

        final_chunk = {
            "id": str(result.message_id),
            "object": "chat.completion.chunk",
            "choices": [{"delta": {}, "index": 0, "finish_reason": "stop"}],
            "message_sources": [],
            "conversation_id": str(conversation.id),
        }
        yield f"data: {json.dumps(final_chunk)}\n\n"
        yield "data: [DONE]\n\n"

    @staticmethod
    def _extract_question(messages: list[ChatMessage]) -> str:
        for msg in reversed(messages):
            if msg.role == "user":
                return msg.content
        return messages[-1].content

    @staticmethod
    async def _invoke_graph(
        question: str,
        pipeline: RagPipeline,
        ctx: UserContext,
    ) -> str:
        """Stub graph invocation. Replace with real LangGraph ainvoke in TASK-010."""
        # Silence unused-argument warnings until TASK-010 wires these up
        _ = question, pipeline, ctx
        return _STUB_ANSWER

"""ChatService — orchestrates query graph invocation.

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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas.chat import ChatMessage, MessageSourceOut
from src.core.clients.llm_client import LLMClient
from src.core.exceptions import ConversationNotFoundError
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

_MAX_HISTORY_MESSAGES = 10


@dataclass
class CompletionResult:
    message_id: uuid.UUID
    answer: str
    prompt_tokens: int
    completion_tokens: int
    citations: list[MessageSourceOut] = field(default_factory=list)
    no_results: bool = False


class ChatService:
    """Business logic for chat completions.

    Calls the LangGraph query graph via invoke_query_graph().
    Transaction boundary: caller (router) commits; service only flushes.
    Exception: stream_completion() calls commit() internally because StreamingResponse
    starts after the router has already returned — the router can't commit after the
    generator finishes.
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
            conv = await db.get(Conversation, conversation_id)
            if (
                conv is None
                or conv.tenant_id != ctx.tenant_id
                or conv.user_id != ctx.user_id
                or conv.is_deleted
            ):
                raise ConversationNotFoundError("CONVERSATION_NOT_FOUND")
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

        # Load conversation history (last N messages) for context
        conversation_history = await self._load_conversation_history(
            db=db, conversation_id=conversation.id
        )

        # Build LLM client and retrieval service
        llm = self._build_llm_client()
        retrieval = await self._build_retrieval_service()

        # Dispatch to research graph when pipeline.prompt_config["research_mode"] is True
        research_mode: bool = bool((pipeline.prompt_config or {}).get("research_mode", False))

        if research_mode:
            (
                answer,
                citation_dicts,
                prompt_tokens,
                completion_tokens,
            ) = await self._invoke_research_graph(
                question=question,
                pipeline=pipeline,
                ctx=ctx,
                db=db,
                llm=llm,
                retrieval=retrieval,
                conversation_history=conversation_history,
            )
            # Research graph always returns evidence-based answers; no_results is
            # derived from citation presence (empty citations ≙ nothing found).
            no_results = len(citation_dicts) == 0 and not answer
        else:
            # Invoke the real LangGraph query graph
            (
                answer,
                citation_dicts,
                no_results,
                prompt_tokens,
                completion_tokens,
            ) = await self._invoke_graph(
                question=question,
                pipeline=pipeline,
                ctx=ctx,
                db=db,
                llm=llm,
                retrieval=retrieval,
                conversation_history=conversation_history,
            )

        # Convert citation dicts to MessageSourceOut schemas
        citations = self._parse_citations(citation_dicts)

        message_id = uuid.uuid4()
        assistant_msg = Message(
            id=message_id,
            conversation_id=conversation.id,
            role="assistant",
            content=answer,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        db.add(assistant_msg)
        await db.flush()

        # NOTE: never log question/answer content — only identifiers
        logger.info(
            "chat_completion",
            conversation_id=str(conversation.id),
            message_id=str(message_id),
            pipeline_id=str(pipeline.id),
            no_results=no_results,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

        return CompletionResult(
            message_id=message_id,
            answer=answer,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            citations=citations,
            no_results=no_results,
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
        """Yield SSE-formatted chunks.

        Note: stream_completion calls session.commit() internally because the StreamingResponse
        starts after the router has returned — the router cannot commit after the generator ends.
        """
        result = await self.complete(
            db=db, ctx=ctx, pipeline=pipeline, conversation=conversation, messages=messages
        )

        # Commit here: the router cannot commit after StreamingResponse has started
        await db.commit()

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
            "message_sources": [s.model_dump() for s in result.citations],
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
    def _build_llm_client() -> LLMClient:
        """Instantiate LLMClient with default config.

        The actual model endpoint is resolved per-call inside each node
        using the model record from DB (model_record.endpoint_url).
        """
        return LLMClient()

    @staticmethod
    async def _build_retrieval_service() -> object:
        """Build a RetrievalService from settings.

        Delegates to retrieval.service.get_retrieval_service() — the ONLY
        module permitted to instantiate AsyncQdrantClient (ADR-1).
        """
        from src.retrieval.service import get_retrieval_service

        return get_retrieval_service()

    async def _invoke_graph(
        self,
        *,
        question: str,
        pipeline: RagPipeline,
        ctx: UserContext,
        db: AsyncSession,
        llm: LLMClient,
        retrieval: object,
        conversation_history: list[dict[str, str]],
    ) -> tuple[str, list[dict[str, object]], bool, int, int]:
        """Invoke the LangGraph query graph and return results.

        Returns:
            Tuple of (answer, citation_dicts, no_results, prompt_tokens, completion_tokens).
        """
        from src.graphs.query_graph.graph import invoke_query_graph
        from src.retrieval.service import RetrievalService

        if not isinstance(retrieval, RetrievalService):
            raise TypeError("retrieval must be a RetrievalService instance")

        return await invoke_query_graph(
            question=question,
            pipeline=pipeline,
            ctx=ctx,
            db=db,
            llm=llm,
            retrieval=retrieval,
            conversation_history=conversation_history,
        )

    async def _invoke_research_graph(
        self,
        *,
        question: str,
        pipeline: RagPipeline,
        ctx: UserContext,
        db: AsyncSession,
        llm: LLMClient,
        retrieval: object,
        conversation_history: list[dict[str, str]],
    ) -> tuple[str, list[dict[str, object]], int, int]:
        """Invoke the agentic multi-hop research graph and return results.

        Called when pipeline.prompt_config["research_mode"] is True.

        Returns:
            Tuple of (answer, citation_dicts, prompt_tokens, completion_tokens).
        """
        from src.graphs.research_graph.graph import invoke_research_graph
        from src.retrieval.service import RetrievalService

        if not isinstance(retrieval, RetrievalService):
            raise TypeError("retrieval must be a RetrievalService instance")

        return await invoke_research_graph(
            question=question,
            pipeline=pipeline,
            ctx=ctx,
            db=db,
            llm=llm,
            retrieval=retrieval,
            conversation_history=conversation_history,
        )

    @staticmethod
    async def _load_conversation_history(
        *,
        db: AsyncSession,
        conversation_id: uuid.UUID,
    ) -> list[dict[str, str]]:
        """Load the last N messages from a conversation for context injection.

        Returns list of {"role": ..., "content": ...} dicts, oldest first.
        Content is NOT logged per GDPR rules.
        """
        result = await db.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc())
            .limit(_MAX_HISTORY_MESSAGES)
        )
        messages = list(reversed(result.scalars().all()))
        return [{"role": m.role, "content": m.content} for m in messages]

    @staticmethod
    def _parse_citations(citation_dicts: list[dict[str, object]]) -> list[MessageSourceOut]:
        """Convert raw citation dicts from the graph to MessageSourceOut schemas.

        Skips any dict that cannot be parsed (missing required fields).
        """
        citations: list[MessageSourceOut] = []
        for cit in citation_dicts:
            try:
                raw_section = cit.get("section_heading")
                raw_url = cit.get("source_url")
                raw_chunk = cit.get("chunk_id")
                raw_page = cit.get("page_number")
                raw_highlight = cit.get("highlight_text")
                source = MessageSourceOut(
                    document_id=str(cit.get("document_id", "")),
                    collection_id=str(cit.get("collection_id", "")),
                    document_title=str(cit.get("document_title", "")),
                    section_heading=str(raw_section) if raw_section is not None else None,
                    source_url=str(raw_url) if raw_url is not None else None,
                    chunk_id=str(raw_chunk) if raw_chunk is not None else None,
                    page_number=int(str(raw_page)) if raw_page is not None else None,
                    highlight_text=str(raw_highlight) if raw_highlight is not None else None,
                    relevance_score=float(str(cit.get("relevance_score") or 0.0)),
                )
                citations.append(source)
            except (ValueError, TypeError):
                logger.warning(
                    "chat_service.citation_parse_error",
                    keys=list(cit.keys()),
                )
        return citations

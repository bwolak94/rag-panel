# TASK-011: Chat API and Conversation Management

**Status:** TODO
**Priority:** P0 — required for Open WebUI integration
**Owner:** backend-dev
**Reviewer:** python-reviewer (security profile)
**Related docs:** `docs/architecture.md` §4, §10 | `docs/data-model.md` §2.3 | `docs/api.md`
**Estimated effort:** 3–4 days

---

## Overview

Implement the OpenAI-compatible chat completions endpoint and conversation management API. This is the primary interface between Open WebUI and the RAG platform.

The `POST /v1/chat/completions` endpoint receives requests in OpenAI format, uses the `model` field to resolve a RAG pipeline, invokes the query graph (TASK-010), and returns a response with citations in an extension field. SSE streaming is supported.

Conversation management endpoints (`/conversations/*`) provide CRUD for the conversation history. The chat completions endpoint auto-creates conversations; these endpoints let clients list, retrieve, and delete them.

Per `docs/architecture.md` §10, the RAG API is the source of truth for audit. Open WebUI maintains a parallel display history — both are linked by `user_id` (Keycloak `sub`).

All endpoints update `docs/03-Specyfikacja-API.md` after implementation.

---

## Usage

### Open WebUI integration flow

```
1. Open WebUI sends POST /v1/models → receives RAG pipeline list as "models"
2. User selects model "rag-procedures" in chat UI
3. Open WebUI sends POST /v1/chat/completions {model: "rag-procedures", messages: [...]}
4. API resolves pipeline from rag_pipelines WHERE name = "rag-procedures" AND tenant_id = JWT tenant
5. API invokes query graph
6. API streams SSE response; final chunk includes citations in extra field
7. Open WebUI displays answer with source document links
```

### Conversation management flow

```
POST /conversations             → create (returns conversation_id)
GET  /conversations             → list user's conversations (paginated)
GET  /conversations/{id}        → detail with messages and sources
DELETE /conversations/{id}      → soft delete (is_deleted=True + audit log)
POST /messages/{id}/feedback    → thumbs up/down with optional comment
```

---

## Tech Stack

- **FastAPI** — async router, SSE via `StreamingResponse`
- **Pydantic** v2 — request/response schemas
- **SQLAlchemy 2.0** async — all DB operations via `AsyncSession`
- **python-jose** — JWT decoding (Keycloak-issued)
- **sse-starlette** or `fastapi.responses.StreamingResponse` with `text/event-stream` — SSE streaming
- **structlog** — logging with `tenant_id`, `user_id`, `conversation_id`; never log message content

---

## Database Patterns

### `conversations`

| Operation | When |
|---|---|
| `INSERT` | `POST /v1/chat/completions` (first message) or `POST /conversations` |
| `UPDATE updated_at` | Every `POST /v1/chat/completions` on existing conversation; via `node_persist` in query graph |
| `UPDATE is_deleted=True` | `DELETE /conversations/{id}` |
| `SELECT` | `GET /conversations`, `GET /conversations/{id}`; always filter by `tenant_id` AND `user_id` AND `is_deleted=False` |

### `messages`

| Operation | When |
|---|---|
| `INSERT` (user role) | `POST /v1/chat/completions` — before graph invocation; provides `message_id` to graph |
| `INSERT` (assistant role, partial) | Before graph; graph fills in `content`, `prompt_tokens`, `completion_tokens`, `latency_ms` in `node_persist` |
| `SELECT` | `GET /conversations/{id}` — ordered by `created_at ASC`; always joined to verify `conversation.tenant_id == user_ctx.tenant_id` |

### `message_sources`

Read-only from this layer. Written by `node_persist` in the query graph. Returned with messages in `GET /conversations/{id}`.

### `feedback`

| Operation | When |
|---|---|
| `INSERT` or `UPDATE` | `POST /messages/{id}/feedback`; unique constraint `(message_id, user_id)` — upsert behavior |
| Access control | Verify `message.conversation.tenant_id == user_ctx.tenant_id` before writing |

### `audit_log`

Written for: `DELETE /conversations/{id}` (action=`conversation.deleted`), `POST /messages/{id}/feedback` (action=`message.feedback`).

---

## Architecture — SOLID & DRY

### File layout

```
src/
    api/
        routers/
            chat.py              # POST /v1/chat/completions + GET /v1/models
            conversations.py     # GET/POST/DELETE /conversations
            messages.py          # POST /messages/{id}/feedback
        schemas/
            chat.py              # OpenAI-compatible request/response schemas
            conversations.py     # ConversationCreate, ConversationResponse, etc.
        dependencies/
            auth.py              # get_current_ctx(), require() — from TASK-003, do NOT reimplement
            collections.py       # get_user_allowed_collections()
            pipelines.py         # resolve_pipeline_by_model()
    domain/
        chat.py                  # ChatService — orchestrates query graph invocation
        conversation.py          # ConversationService — CRUD for conversations
```

> **Architecture note:** Services must live in `src/domain/`, never in `src/api/`. Routers import from `src/domain/`, not the other way around. Placing `ChatService` under `src/api/services/` violates the layered architecture rule (`api → domain → core`; importing from `api` into `domain`/`core` is forbidden). The canonical path is `src/domain/chat.py`.

### Schemas (`src/api/schemas/chat.py`)

```python
from pydantic import BaseModel, Field
from typing import Literal

# Request — OpenAI-compatible
class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str

class ChatCompletionRequest(BaseModel):
    model: str                             # maps to rag_pipelines.name or models_registry.name
    messages: list[ChatMessage]
    stream: bool = False
    conversation_id: str | None = None     # extension: link to existing conversation
    max_tokens: int | None = None
    temperature: float | None = None

# Response — OpenAI-compatible + RAG extensions
class MessageSourceOut(BaseModel):
    document_id: str
    chunk_id: str | None
    page_number: int | None
    highlight_text: str | None
    relevance_score: float

class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: Literal["stop", "length", "content_filter"] = "stop"

class ChatCompletionUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

class ChatCompletionResponse(BaseModel):
    id: str                                # message_id as string
    object: Literal["chat.completion"] = "chat.completion"
    model: str
    choices: list[ChatCompletionChoice]
    usage: ChatCompletionUsage
    # RAG extension fields (Open WebUI supports extra top-level fields)
    message_sources: list[MessageSourceOut] = Field(default_factory=list)
    conversation_id: str | None = None
```

### Schemas (`src/api/schemas/conversations.py`)

```python
from uuid import UUID
from datetime import datetime
from pydantic import BaseModel

class ConversationCreate(BaseModel):
    pipeline_id: UUID
    title: str | None = None

class ConversationResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    user_id: UUID
    pipeline_id: UUID | None
    title: str | None
    created_at: datetime
    updated_at: datetime

class MessageSourceResponse(BaseModel):
    id: UUID
    document_id: UUID | None
    chunk_id: UUID | None
    relevance_score: float
    highlight_text: str | None
    page_number: int | None

class MessageResponse(BaseModel):
    id: UUID
    role: str
    content: str
    model_id: UUID | None
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_ms: float | None
    created_at: datetime
    sources: list[MessageSourceResponse] = []

class ConversationDetailResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    pipeline_id: UUID | None
    title: str | None
    created_at: datetime
    updated_at: datetime
    messages: list[MessageResponse]

class FeedbackCreate(BaseModel):
    rating: Literal["up", "down"]
    comment: str | None = None

class FeedbackResponse(BaseModel):
    id: UUID
    message_id: UUID
    rating: str
    comment: str | None
    created_at: datetime
```

---

## Implementation Steps

### Step 1: Dependencies

**`src/api/dependencies/auth.py`** — do NOT reimplement `get_current_ctx` here. Use the canonical implementation from TASK-003.

> **BLOCKER note:** TASK-011 must NOT define or reimplement `get_current_ctx` using `jwt.decode(token, settings.keycloak_public_key, ...)` or any HMAC/HS256 variant. The single authoritative implementation lives in `src/api/dependencies/auth.py` as delivered by TASK-003. TASK-011 only adds `resolve_pipeline_by_model()` as an additional dependency on top of `get_current_ctx`. Importing and calling `get_current_ctx` from TASK-003 is the correct approach — never duplicate it.

**`src/api/dependencies/pipelines.py`**:

```python
async def resolve_pipeline_by_model(
    model: str,
    user_ctx: UserContext,
    db: AsyncSession,
) -> RagPipeline:
    """Resolve a rag_pipeline by model name (= pipeline.name) within the user's tenant."""
    pipeline = await db.scalar(
        select(RagPipeline)
        .where(
            RagPipeline.tenant_id == user_ctx.tenant_id,
            RagPipeline.name == model,
            RagPipeline.is_active == True,
        )
    )
    if not pipeline:
        raise HTTPException(
            status_code=404,
            detail={"code": "MODEL_NOT_FOUND", "model": model},
        )
    # Verify all pipeline.collection_ids are in user_ctx.allowed_collection_ids
    accessible = set(user_ctx.allowed_collection_ids)
    for cid in pipeline.collection_ids:
        if cid not in accessible:
            raise HTTPException(
                status_code=403,
                detail={"code": "COLLECTION_ACCESS_DENIED", "collection_id": str(cid)},
            )
    return pipeline
```

### Step 2: Chat router (`src/api/routers/chat.py`)

```python
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from src.api.schemas.chat import ChatCompletionRequest, ChatCompletionResponse
from src.api.dependencies.auth import get_current_ctx, require
from src.api.dependencies.pipelines import resolve_pipeline_by_model
from src.domain.chat import ChatService
import json, uuid

router = APIRouter(prefix="/v1", tags=["chat"])

@router.get("/models")
async def list_models(
    user_ctx: UserContext = Depends(require("chat:query")),
    db: AsyncSession = Depends(get_db),
):
    """List available RAG pipelines as OpenAI-compatible models.

    Returns only pipelines accessible to the user's roles. Open WebUI displays
    these as selectable models in the chat interface.
    """
    pipelines = await _load_accessible_pipelines(db, user_ctx)
    return {
        "object": "list",
        "data": [
            {
                "id": p.name,
                "object": "model",
                "owned_by": str(p.tenant_id),
                "created": int(p.created_at.timestamp()),
            }
            for p in pipelines
        ],
    }


@router.post("/chat/completions")
async def chat_completions(
    request: ChatCompletionRequest,
    user_ctx: UserContext = Depends(require("chat:query")),
    db: AsyncSession = Depends(get_db),
    chat_svc: ChatService = Depends(get_chat_service),
):
    """OpenAI-compatible chat completion. Runs the RAG query graph.

    The 'model' field maps to rag_pipelines.name within the user's tenant.
    If 'stream' is True, returns an SSE stream of completion chunks.
    The final SSE chunk includes 'message_sources' with citations.
    """
    pipeline = await resolve_pipeline_by_model(request.model, user_ctx, db)

    # Resolve or create conversation
    conversation_id = UUID(request.conversation_id) if request.conversation_id else None
    conversation = await chat_svc.get_or_create_conversation(
        db=db,
        user_ctx=user_ctx,
        pipeline_id=pipeline.id,
        conversation_id=conversation_id,
    )

    if request.stream:
        return StreamingResponse(
            chat_svc.stream_completion(
                db=db,
                user_ctx=user_ctx,
                pipeline=pipeline,
                conversation=conversation,
                messages=request.messages,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    result = await chat_svc.complete(
        db=db,
        user_ctx=user_ctx,
        pipeline=pipeline,
        conversation=conversation,
        messages=request.messages,
    )
    return ChatCompletionResponse(
        id=str(result.message_id),
        model=request.model,
        choices=[{"index": 0, "message": {"role": "assistant", "content": result.answer}, "finish_reason": "stop"}],
        usage={"prompt_tokens": result.prompt_tokens, "completion_tokens": result.completion_tokens,
               "total_tokens": result.prompt_tokens + result.completion_tokens},
        message_sources=[s.model_dump() for s in result.citations],
        conversation_id=str(conversation.id),
    )
```

### Step 3: ChatService (`src/domain/chat.py`)

```python
from uuid import uuid4
from src.graphs.query_graph.graph import build_query_graph
from src.graphs.query_graph.state import QueryState, PipelineConfig

class ChatService:
    def __init__(self, graph, retrieval_svc: RetrievalService, llm: LLMClient):
        self._graph = graph
        self._retrieval = retrieval_svc
        self._llm = llm

    async def get_or_create_conversation(
        self,
        db: AsyncSession,
        user_ctx: UserContext,
        pipeline_id: UUID,
        conversation_id: UUID | None,
    ) -> Conversation:
        if conversation_id:
            conv = await db.get(Conversation, conversation_id)
            if not conv or conv.tenant_id != user_ctx.tenant_id or conv.user_id != user_ctx.user_id:
                raise HTTPException(404, {"code": "CONVERSATION_NOT_FOUND"})
            if conv.is_deleted:
                raise HTTPException(404, {"code": "CONVERSATION_NOT_FOUND"})
            return conv

        conv = Conversation(
            id=uuid4(),
            tenant_id=user_ctx.tenant_id,
            user_id=user_ctx.user_id,
            pipeline_id=pipeline_id,
        )
        db.add(conv)
        await db.flush()
        return conv

    async def complete(
        self,
        db: AsyncSession,
        user_ctx: UserContext,
        pipeline: RagPipeline,
        conversation: Conversation,
        messages: list[ChatMessage],
    ) -> CompletionResult:
        question = self._extract_latest_user_message(messages)
        message_id = uuid4()

        # Pre-insert user message and placeholder assistant message
        await self._insert_user_message(db, conversation.id, question)
        await self._insert_assistant_placeholder(db, conversation.id, message_id, pipeline)

        pipeline_config = await self._build_pipeline_config(db, pipeline, user_ctx)
        initial_state = QueryState(
            question=question,
            user_ctx=user_ctx,
            pipeline_config=pipeline_config,
            conversation_id=conversation.id,
            message_id=message_id,
        )

        config = {"configurable": {"thread_id": str(message_id)}}
        final_state = await self._graph.ainvoke(
            initial_state.model_dump(),
            config=config,
        )

        return CompletionResult(
            message_id=message_id,
            answer=final_state["answer"],
            prompt_tokens=final_state["prompt_tokens"],
            completion_tokens=final_state["completion_tokens"],
            citations=final_state["citations"],
        )

    async def stream_completion(
        self,
        db: AsyncSession,
        user_ctx: UserContext,
        pipeline: RagPipeline,
        conversation: Conversation,
        messages: list[ChatMessage],
    ):
        """Yields SSE-formatted chunks. Final chunk includes citations."""
        question = self._extract_latest_user_message(messages)
        message_id = uuid4()
        # ... setup same as complete()
        async for chunk in self._graph.astream(initial_state.model_dump(), config=config):
            if "node_generate" in chunk:
                answer_fragment = chunk["node_generate"].get("answer", "")
                yield self._format_sse_chunk(answer_fragment, message_id)
        # Final chunk with citations
        yield self._format_sse_final(message_id, conversation.id, final_state)
        yield "data: [DONE]\n\n"

    @staticmethod
    def _format_sse_chunk(content: str, message_id: UUID) -> str:
        data = {
            "id": str(message_id),
            "object": "chat.completion.chunk",
            "choices": [{"delta": {"content": content}, "index": 0, "finish_reason": None}],
        }
        return f"data: {json.dumps(data)}\n\n"

    @staticmethod
    def _format_sse_final(message_id: UUID, conversation_id: UUID, state: dict) -> str:
        data = {
            "id": str(message_id),
            "object": "chat.completion.chunk",
            "choices": [{"delta": {}, "index": 0, "finish_reason": "stop"}],
            "message_sources": state.get("citations", []),
            "conversation_id": str(conversation_id),
        }
        return f"data: {json.dumps(data)}\n\n"
```

**`get_chat_service` dependency** (place in `src/api/dependencies/chat.py` or at the top of `src/api/routers/chat.py`):

```python
# NOTE (MINOR): This factory must be defined before use in Depends(get_chat_service).
async def get_chat_service(
    db: AsyncSession = Depends(get_db),
    retrieval: RetrievalService = Depends(get_retrieval_service),
) -> ChatService:
    return ChatService(db=db, retrieval=retrieval)
```

### Step 4: Conversations router (`src/api/routers/conversations.py`)

```python
router = APIRouter(prefix="/conversations", tags=["conversations"])

@router.post("", response_model=ConversationResponse, status_code=201)
async def create_conversation(
    body: ConversationCreate,
    user_ctx: UserContext = Depends(require("chat:query")),
    db: AsyncSession = Depends(get_db),
):
    """Create a new conversation linked to a pipeline."""
    # Verify pipeline access
    pipeline = await db.get(RagPipeline, body.pipeline_id)
    if not pipeline or pipeline.tenant_id != user_ctx.tenant_id:
        raise HTTPException(404, {"code": "PIPELINE_NOT_FOUND"})

    conv = Conversation(
        id=uuid4(),
        tenant_id=user_ctx.tenant_id,
        user_id=user_ctx.user_id,
        pipeline_id=body.pipeline_id,
        title=body.title,
    )
    db.add(conv)
    await db.commit()
    return ConversationResponse.model_validate(conv)


@router.get("", response_model=list[ConversationResponse])
async def list_conversations(
    user_ctx: UserContext = Depends(require("chat:query")),
    db: AsyncSession = Depends(get_db),
    offset: int = 0,
    limit: int = 50,
):
    """List the current user's conversations. Paginated. Excludes soft-deleted."""
    rows = await db.scalars(
        select(Conversation)
        .where(
            Conversation.tenant_id == user_ctx.tenant_id,
            Conversation.user_id == user_ctx.user_id,
            Conversation.is_deleted == False,
        )
        .order_by(Conversation.updated_at.desc())
        .offset(offset)
        .limit(min(limit, 100))
    )
    return [ConversationResponse.model_validate(c) for c in rows]


@router.get("/{conversation_id}", response_model=ConversationDetailResponse)
async def get_conversation(
    conversation_id: UUID,
    user_ctx: UserContext = Depends(require("chat:query")),
    db: AsyncSession = Depends(get_db),
):
    """Get a conversation with all messages and their sources."""
    conv = await db.scalar(
        select(Conversation)
        .where(
            Conversation.id == conversation_id,
            Conversation.tenant_id == user_ctx.tenant_id,  # resource-level auth
            Conversation.user_id == user_ctx.user_id,
            Conversation.is_deleted == False,
        )
        .options(
            selectinload(Conversation.messages).selectinload(Message.sources)
        )
    )
    if not conv:
        raise HTTPException(404, {"code": "CONVERSATION_NOT_FOUND"})
    return ConversationDetailResponse.model_validate(conv)


@router.delete("/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: UUID,
    user_ctx: UserContext = Depends(require("chat:query")),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete a conversation. Creates audit log entry."""
    # NOTE (MINOR — IDOR fix): use a tenant-scoped query instead of db.get() which
    # is unscoped and would allow an attacker to confirm existence of other tenants'
    # conversations by probing IDs (timing/response difference).
    conv = await db.scalar(
        select(Conversation)
        .where(Conversation.id == conversation_id, Conversation.tenant_id == user_ctx.tenant_id)
    )
    if conv is None:
        raise NotFoundError("Conversation not found")
    if conv.user_id != user_ctx.user_id or conv.is_deleted:
        raise HTTPException(404, {"code": "CONVERSATION_NOT_FOUND"})

    conv.is_deleted = True
    await db.flush()

    await insert_audit_log(db, AuditEntry(
        tenant_id=user_ctx.tenant_id,
        user_id=user_ctx.user_id,
        action="conversation.deleted",
        resource_type="conversation",
        resource_id=conversation_id,
        details={},   # no content
    ))
    await db.commit()
```

### Step 5: Messages router (`src/api/routers/messages.py`)

```python
router = APIRouter(prefix="/messages", tags=["messages"])

@router.post("/{message_id}/feedback", response_model=FeedbackResponse, status_code=201)
async def submit_feedback(
    message_id: UUID,
    body: FeedbackCreate,
    user_ctx: UserContext = Depends(require("chat:query")),
    db: AsyncSession = Depends(get_db),
):
    """Submit thumbs up/down feedback for an assistant message.

    One rating per user per message (unique constraint). Subsequent calls update the rating.
    """
    # Verify message exists and belongs to user's tenant (via conversation join)
    message = await db.scalar(
        select(Message)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(
            Message.id == message_id,
            Conversation.tenant_id == user_ctx.tenant_id,  # resource-level auth
        )
    )
    if not message:
        raise HTTPException(404, {"code": "MESSAGE_NOT_FOUND"})

    # Upsert feedback (unique per message_id + user_id)
    existing = await db.scalar(
        select(Feedback).where(
            Feedback.message_id == message_id,
            Feedback.user_id == user_ctx.user_id,
        )
    )
    if existing:
        existing.rating = body.rating
        existing.comment = body.comment
        feedback = existing
    else:
        feedback = Feedback(
            id=uuid4(),
            tenant_id=user_ctx.tenant_id,
            message_id=message_id,
            user_id=user_ctx.user_id,
            rating=body.rating,
            comment=body.comment,
        )
        db.add(feedback)

    await db.flush()

    await insert_audit_log(db, AuditEntry(
        tenant_id=user_ctx.tenant_id,
        user_id=user_ctx.user_id,
        action="message.feedback",
        resource_type="message",
        resource_id=message_id,
        details={"rating": body.rating},   # no comment content in audit log
    ))

    await db.commit()
    return FeedbackResponse.model_validate(feedback)
```

### Step 6: LLM unavailability handling

Per `docs/architecture.md` §16, when the LLM is unreachable after retries, the query graph raises `LLMUnavailableError`. The chat router catches it and returns HTTP 503:

```python
from src.core.exceptions import LLMUnavailableError

@router.post("/v1/chat/completions")
async def chat_completions(...):
    try:
        ...
    except LLMUnavailableError:
        raise HTTPException(
            status_code=503,
            detail={"error": "llm_unavailable",
                    "message": "The AI service is temporarily unavailable. Please try again in a few minutes."},
            headers={"Retry-After": "60"},
        )
```

### Step 7: Register routes in app

```python
# src/api/app.py
from src.api.routers import chat, conversations, messages

app.include_router(chat.router)
app.include_router(conversations.router)
app.include_router(messages.router)
```

---

## API Contracts

All endpoints require `Authorization: Bearer <JWT>` with a valid Keycloak-issued token.

### POST /v1/chat/completions

**Permission:** `chat:query`

**Request:**
```json
{
  "model": "rag-procedures",
  "messages": [
    {"role": "user", "content": "Jakie są procedury sterylizacji?"}
  ],
  "stream": false,
  "conversation_id": null
}
```

**Response 200:**
```json
{
  "id": "msg_01abc",
  "object": "chat.completion",
  "model": "rag-procedures",
  "choices": [{
    "index": 0,
    "message": {"role": "assistant", "content": "Procedury sterylizacji..."},
    "finish_reason": "stop"
  }],
  "usage": {"prompt_tokens": 512, "completion_tokens": 256, "total_tokens": 768},
  "message_sources": [
    {"document_id": "...", "chunk_id": "...", "page_number": 12, "highlight_text": "...", "relevance_score": 0.91}
  ],
  "conversation_id": "conv_01xyz"
}
```

**Error responses:**

| Status | Code | Condition |
|---|---|---|
| 401 | `TOKEN_INVALID` | Missing or invalid JWT |
| 403 | `PERMISSION_DENIED` | User lacks `chat:query` |
| 404 | `MODEL_NOT_FOUND` | Pipeline name not found in tenant |
| 422 | — | Validation error (empty messages, etc.) |
| 503 | `llm_unavailable` | LLM unreachable after retries |

### GET /v1/models

**Permission:** `chat:query`

**Response 200:** OpenAI-compatible model list; one entry per accessible pipeline.

### POST /conversations

**Permission:** `chat:query`

**Request:** `{"pipeline_id": "uuid", "title": "optional"}`

**Response 201:** `ConversationResponse`

### GET /conversations

**Permission:** `chat:query`

**Query params:** `offset=0&limit=50`

**Response 200:** `list[ConversationResponse]`; only user's conversations in current tenant.

### GET /conversations/{id}

**Permission:** `chat:query`

**Response 200:** `ConversationDetailResponse` with messages and sources.

**Error 404 conditions:** conversation not found, belongs to different tenant, belongs to different user, is soft-deleted.

### DELETE /conversations/{id}

**Permission:** `chat:query` (user can only delete own conversations)

**Response 204:** No body.

**Audit log:** `action=conversation.deleted`.

### POST /messages/{id}/feedback

**Permission:** `chat:query`

**Request:** `{"rating": "up" | "down", "comment": "optional"}`

**Response 201:** `FeedbackResponse`

**Idempotent:** second call updates the rating.

---

## Security Checklist

- [ ] `GET /conversations` filters by BOTH `tenant_id` AND `user_id` — users cannot see other users' conversations.
- [ ] `GET /conversations/{id}` verifies `conversation.tenant_id == user_ctx.tenant_id` AND `conversation.user_id == user_ctx.user_id` — IDOR prevention.
- [ ] `POST /messages/{id}/feedback` verifies message belongs to the user's tenant via conversation join — cross-tenant feedback submission impossible.
- [ ] `DELETE /conversations/{id}` is a soft delete — no message or source data removed; full audit trail preserved.
- [ ] No message content appears in any log statement. Use `conversation_id`, `message_id`, `pipeline_id` in logs.
- [ ] `resolve_pipeline_by_model()` verifies all `pipeline.collection_ids` are in `user_ctx.allowed_collection_ids` — prevents pipeline escalation.
- [ ] SSE `StreamingResponse` sets `Cache-Control: no-cache` and `X-Accel-Buffering: no`.
- [ ] `LLMUnavailableError` caught at router level — returns HTTP 503 with `Retry-After: 60` header.
- [ ] JWT token is extracted from the `Authorization: Bearer` header only. No API key bypass path.
- [ ] `conversation_id` in request body (extension field) is validated: must belong to `user_ctx.user_id` within `user_ctx.tenant_id`.

---

## Terms of Use (relevant constraints)

- **`model` field maps to `rag_pipelines.name`**, not `models_registry.name`. Open WebUI presents pipelines as model choices. Raw LLM access (for future power-user paths) uses a separate routing check.
- **Message content stored, not logged**: `messages.content` is written to Postgres by `node_persist` but never written to structlog, Langfuse spans, or any other store.
- **RAG API is the source of truth**: the RAG API persists all conversations and messages independently of Open WebUI's own history. Both are linked by `user_id` (Keycloak `sub`).
- **Conversation ownership**: conversations belong to a specific `user_id`. Admin users with `admin:audit` permission can read conversations for audit purposes via a separate admin endpoint (not in scope for this task).
- **Feedback is upserted**, not appended. The unique constraint `(message_id, user_id)` means a user can change their rating — the latest value is stored.
- **`POST /conversations` is optional**: the chat completions endpoint auto-creates conversations when no `conversation_id` is provided. This endpoint is for UIs that want to pre-create conversations with custom titles.

---

## Tests

### Unit tests

**`tests/unit/auth/test_get_current_ctx.py`** — covered by TASK-016 §1. Relevant here:

- JWT missing `tenant_id` → 401 `TOKEN_MISSING_CLAIM`
- JWT with wrong audience → 401 `TOKEN_INVALID_AUDIENCE`
- JWT expired → 401 `TOKEN_EXPIRED`

### Integration tests (`tests/integration/test_api_contracts.py`)

Use `httpx.AsyncClient` with `ASGITransport(app=app)`. JWT from `make_jwt()` helper with HMAC-SHA256 (test secret).

**Chat completions — happy path:**

| Test | Setup | Assert |
|---|---|---|
| `test_chat_completion_non_streaming` | Valid JWT, accessible pipeline, LLM mock | 200; `choices[0].message.content` non-empty; `message_sources` present |
| `test_chat_completion_streaming` | `stream=true` | 200; SSE content-type; multiple `data:` chunks; final chunk has `finish_reason=stop` and `message_sources` |
| `test_chat_creates_conversation` | No `conversation_id` provided | `conversation_id` in response; conversation row in DB |
| `test_chat_uses_existing_conversation` | Valid `conversation_id` | Response `conversation_id` matches input; no new conversation created |

**Chat completions — 401/403:**

| Test | Assert |
|---|---|
| `test_no_auth_header_returns_401` | 401 |
| `test_expired_jwt_returns_401` | 401 `TOKEN_EXPIRED` |
| `test_model_not_in_tenant_returns_404` | 404 `MODEL_NOT_FOUND` |
| `test_viewer_without_chat_query_permission_returns_403` | 403 `PERMISSION_DENIED` |

**Conversations:**

| Test | Assert |
|---|---|
| `test_list_conversations_only_own` | User A cannot see User B conversations |
| `test_get_conversation_cross_tenant_returns_404` | 404 for cross-tenant ID |
| `test_delete_conversation_soft_delete` | `is_deleted=True`; subsequent GET returns 404 |
| `test_delete_conversation_creates_audit_log` | `audit_log` has `conversation.deleted` entry |

**Feedback:**

| Test | Assert |
|---|---|
| `test_feedback_submit` | 201; row in `feedback` table |
| `test_feedback_update_rating` | Second call updates rating; no duplicate row |
| `test_feedback_cross_tenant_message_returns_404` | 404 |

### Role-based auth tests (`tests/security/test_rbac.py`)

| Role | `POST /v1/chat/completions` | `DELETE /conversations/{id}` | Expected |
|---|---|---|---|
| Viewer | own conversation | — | 200 |
| Viewer | — | — | 403 if lacks `chat:query` |
| Contributor | — | — | Same as Viewer for chat |
| Admin | — | — | Same as Viewer for own conversations |
| No auth | — | — | 401 |

---

## Definition of Done

- [ ] `POST /v1/chat/completions` works in both streaming and non-streaming modes.
- [ ] `GET /v1/models` returns only pipelines accessible to the user's roles.
- [ ] All 5 conversation/message endpoints implemented with correct status codes.
- [ ] Resource-level auth on every endpoint (tenant_id + user_id verification).
- [ ] `conversation_id` auto-created when not provided in chat request.
- [ ] `LLMUnavailableError` mapped to HTTP 503 with `Retry-After: 60`.
- [ ] No message content in any log statement.
- [ ] `docs/03-Specyfikacja-API.md` updated with all endpoints from this task.
- [ ] `ruff check --fix . && mypy src/ && pytest tests/integration/test_api_contracts.py -x -q` passes.
- [ ] RBAC tests for all roles pass.
- [ ] IDOR tests (`tests/security/test_idor.py`) covering conversation access pass.

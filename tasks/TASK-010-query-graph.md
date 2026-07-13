# TASK-010: LangGraph Query Graph

**Status:** TODO
**Priority:** P0 — required for chat functionality
**Owner:** backend-dev + rag-engineer
**Reviewer:** python-reviewer (security profile) + security-auditor
**Related docs:** `docs/architecture.md` §4, §7, §10, §17 | `docs/data-model.md` §2.3 | `docs/rodo.md`
**Estimated effort:** 5–7 days

---

## Overview

Implement the LangGraph query graph in `src/graphs/query_graph/`. The graph processes user questions through a 7-node pipeline: `classify_intent → rewrite_query → retrieve → grade_documents → generate → guardrails_output → persist`. Each node is a separate file `node_<name>.py`. Graph state is a Pydantic model `QueryState` in `state.py`. The Postgres checkpointer records execution state per node for debugging.

The graph is invoked by the chat API endpoint (`POST /v1/chat/completions`, TASK-011) and runs to completion synchronously from the caller's perspective (or streams tokens during the `generate` node). It is the only module that calls `RetrievalService.search()` during the query path.

Full Langfuse tracing with the span hierarchy defined in `docs/architecture.md` §17:
```
Trace: query_graph (trace_id = message_id)
  Span: classify_intent
  Span: rewrite_query
  Span: retrieve
  Span: grade_documents
  [Optional] Span: refine_query
  Span: generate
  Span: guardrails_output
  Span: persist
```

`capture_input=False` and `capture_output=False` on all spans — PII protection.

---

## Usage

```python
# src/api/routers/chat.py
from src.graphs.query_graph.graph import build_query_graph

graph = build_query_graph(checkpointer=pg_checkpointer)

initial = QueryState(
    question=user_message,
    user_ctx=user_ctx,
    pipeline_config=pipeline_config,
    conversation_id=conversation_id,
)
config = {"configurable": {"thread_id": str(message_id)}}

# Non-streaming
result = await graph.ainvoke(initial.model_dump(), config=config)

# Streaming (SSE)
async for chunk in graph.astream(initial.model_dump(), config=config):
    yield format_sse_chunk(chunk)
```

---

## Tech Stack

- **LangGraph** `0.2+` — graph compilation, conditional routing, Postgres checkpointer
- **langgraph-checkpoint-postgres** — `AsyncPostgresSaver`
- **langfuse** — `langfuse.decorators` or `langfuse.callback` for span tracing; `capture_input=False`, `capture_output=False`
- **httpx** — async OpenAI-compatible LLM client calls
- **tenacity** — LLM retry per `docs/architecture.md` §16 (classification/rewrite/grade/generate: 2 retries, exp backoff 2-16s ±20% jitter)
- **pydantic** v2 — `QueryState`, `PipelineConfig`, `Citation`, `GradedChunk`
- **structlog** — structured logging (user_id, tenant_id, conversation_id; NO message content)

---

## Database Patterns

### Tables written by the graph

**`messages`** — Written in `node_persist`. One row per `user` turn (already written by API before graph invocation) and one row for the `assistant` response. The `content` field is stored but never logged.

**`message_sources`** — Written in `node_persist`. One row per citation in the generated response. `chunk_id` is looked up from `chunks_registry.qdrant_point_id` — if the chunk has been deleted, `chunk_id` is NULL (ON DELETE SET NULL, per `docs/data-model.md` §2.3).

**`conversations`** — `updated_at` is bumped in `node_persist`.

**`audit_log`** — Written in `node_persist` for each `chat.query` action. Record: `{action: "chat.query", resource_type: "conversation", resource_id: conversation_id}`. Details must not include message content — only `pipeline_id`, `intent`, `chunks_used`, `latency_ms`.

### Tables read by the graph

**`rag_pipelines`** — Read before graph invocation by the API; passed as `PipelineConfig` in `QueryState`. Not re-read inside nodes.

**`chunks_registry`** — Read in `node_persist` to resolve `qdrant_point_id → chunk_id` for `message_sources`. Query: `SELECT id FROM chunks_registry WHERE qdrant_point_id = ANY(:point_ids) AND tenant_id = :tenant_id`.

---

## Architecture — SOLID & DRY

### File layout

```
src/graphs/query_graph/
    __init__.py
    graph.py          # build_query_graph() factory
    state.py          # QueryState Pydantic model
    nodes/
        __init__.py
        node_classify_intent.py
        node_rewrite_query.py
        node_retrieve.py
        node_grade_documents.py
        node_refine_query.py      # retry branch (not_found → refine → retrieve)
        node_generate.py
        node_guardrails_output.py
        node_persist.py
    routing.py        # Conditional edge functions
    tracing.py        # Langfuse span helpers
```

### QueryState model (`state.py`)

```python
from __future__ import annotations
import time
from uuid import UUID
from typing import Literal
from pydantic import BaseModel, Field

IntentType = Literal[
    "factual", "procedural", "comparative", "meta", "out_of_scope", "small_talk"
]

class UserContext(BaseModel):
    user_id: UUID
    tenant_id: UUID
    role_ids: list[UUID]
    permissions: set[str]
    allowed_collection_ids: list[UUID]

class PipelineConfig(BaseModel):
    pipeline_id: UUID
    collection_ids: list[UUID]
    llm_model_id: UUID
    llm_endpoint_url: str
    llm_model_name: str
    prompt_config: dict           # {system_prompt_version, rag_template_version, top_k, score_threshold, max_context_tokens}
    guardrails: dict              # {pii_filter, disclaimer_required, disclaimer_text, blocked_topics, require_citations}
    qdrant_collection: str        # e.g., "emb_bge_m3" (pattern: emb_{embedding_model_slug})

class RetrievedChunk(BaseModel):
    point_id: UUID
    document_id: UUID
    score: float
    text: str                     # chunk text for LLM context
    page: int | None
    section: str | None
    collection_id: UUID

class GradedChunk(BaseModel):
    chunk: RetrievedChunk
    relevance_score: float        # 0.0 – 1.0 from LLM grading
    relevant: bool                # True if relevance_score >= pipeline threshold

class Citation(BaseModel):
    document_id: UUID
    point_id: UUID
    chunk_id: UUID | None         # resolved from chunks_registry
    page_number: int | None
    highlight_text: str | None
    score: float

class QueryState(BaseModel):
    # Input (set by caller)
    question: str
    user_ctx: UserContext
    pipeline_config: PipelineConfig
    conversation_id: UUID
    message_id: UUID              # ID of the assistant message being generated

    # Intermediate (mutated by nodes)
    intent: IntentType | None = None
    rewritten_query: str | None = None
    retrieved_chunks: list[RetrievedChunk] = Field(default_factory=list)
    graded_chunks: list[GradedChunk] = Field(default_factory=list)
    retry_count: int = 0
    conversation_history: list[dict[str, str]] = Field(default_factory=list)
    # Loaded by node_classify_intent (first node with DB access) and passed forward.
    # node_generate reads this field directly — no DB call inside node_generate.

    # Output
    answer: str = ""
    citations: list[Citation] = Field(default_factory=list)

    # Metrics (populated by nodes, persisted in node_persist)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    started_at: float = Field(default_factory=time.monotonic)
    # Set once at graph entry; node_persist computes elapsed_ms(state.started_at).

    # Control
    not_found: bool = False        # True when no relevant context found
    guardrails_triggered: bool = False
```

### Conditional routing (`routing.py`)

```python
from src.graphs.query_graph.state import QueryState

def route_after_classify(state: QueryState) -> str:
    if state.intent in ("out_of_scope", "small_talk"):
        return "node_generate"    # generate without retrieval (adds disclaimer)
    return "node_rewrite_query"

def route_after_grade(state: QueryState) -> str:
    relevant_count = sum(1 for c in state.graded_chunks if c.relevant)
    if relevant_count == 0 and state.retry_count < 2:
        return "node_refine_query"
    if relevant_count == 0:
        return "node_generate"    # not_found path
    return "node_generate"

def route_after_refine(state: QueryState) -> str:
    return "node_retrieve"        # refine → retrieve → grade loop
```

### Graph compilation (`graph.py`)

```python
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from src.graphs.query_graph.state import QueryState
from src.graphs.query_graph import nodes
from src.graphs.query_graph.routing import (
    route_after_classify, route_after_grade, route_after_refine
)

def build_query_graph(checkpointer: AsyncPostgresSaver | None = None):
    builder = StateGraph(QueryState)

    builder.add_node("node_classify_intent",   nodes.node_classify_intent)
    builder.add_node("node_rewrite_query",     nodes.node_rewrite_query)
    builder.add_node("node_retrieve",          nodes.node_retrieve)
    builder.add_node("node_grade_documents",   nodes.node_grade_documents)
    builder.add_node("node_refine_query",      nodes.node_refine_query)
    builder.add_node("node_generate",          nodes.node_generate)
    builder.add_node("node_guardrails_output", nodes.node_guardrails_output)
    builder.add_node("node_persist",           nodes.node_persist)

    builder.set_entry_point("node_classify_intent")
    builder.add_conditional_edges("node_classify_intent", route_after_classify)
    builder.add_edge("node_rewrite_query", "node_retrieve")
    builder.add_edge("node_retrieve",      "node_grade_documents")
    builder.add_conditional_edges("node_grade_documents", route_after_grade)
    builder.add_conditional_edges("node_refine_query",   route_after_refine)
    builder.add_edge("node_generate",          "node_guardrails_output")
    builder.add_edge("node_guardrails_output", "node_persist")
    builder.add_edge("node_persist",           END)

    return builder.compile(checkpointer=checkpointer)
```

---

## Implementation Steps

### Step 0: Helper utilities (`src/graphs/query_graph/utils.py`)

Create this module before implementing any node. Every node that calls one of these functions must import from here — never define them inline.

```python
# src/graphs/query_graph/utils.py
import time
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.db.repositories.message_repository import MessageRepository
from src.graphs.query_graph.state import GradedChunk


async def load_conversation_history(
    db: AsyncSession, conversation_id: UUID, limit: int = 5
) -> list[dict[str, str]]:
    """Load last N messages for conversation context.

    Returns messages as OpenAI-style dicts: [{"role": "user"|"assistant", "content": "..."}].
    Content is loaded from DB but must NEVER be logged.
    """
    repo = MessageRepository(db)
    messages = await repo.get_recent(conversation_id=conversation_id, limit=limit)
    return [{"role": m.role, "content": m.content} for m in messages]


def sum_prompt_tokens(response: Any) -> int:
    return getattr(getattr(response, "usage", None), "prompt_tokens", 0)


def sum_completion_tokens(response: Any) -> int:
    return getattr(getattr(response, "usage", None), "completion_tokens", 0)


def start_timer() -> float:
    return time.monotonic()


def elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def parse_citations(response_text: str, chunks: list[GradedChunk]) -> list[dict]:
    """Extract citation markers from response and map to chunks.

    Looks for [SOURCE N] markers in response_text, resolves N → chunks[N-1].
    Returns list of dicts matching the Citation schema fields.
    """
    import re

    citations = []
    seen_indices: set[int] = set()
    for match in re.finditer(r"\[SOURCE\s+(\d+)\]", response_text):
        idx = int(match.group(1)) - 1  # 1-based → 0-based
        if 0 <= idx < len(chunks) and idx not in seen_indices:
            seen_indices.add(idx)
            gc = chunks[idx]
            citations.append({
                "document_id": gc.chunk.document_id,
                "point_id": gc.chunk.point_id,
                "chunk_id": None,  # resolved in node_persist from chunks_registry
                "page_number": gc.chunk.page,
                "highlight_text": gc.chunk.text[:300] if gc.chunk.text else None,
                "score": gc.relevance_score,
            })
    return citations


def check_prompt_injection_echo(response: str, chunks: list[GradedChunk]) -> bool:
    """Return True if response appears to echo instruction-like content from chunks.

    Heuristic: detect if known injection patterns from chunk text appear verbatim
    in the response. Raises GuardrailsViolation on detection.
    """
    INJECTION_PATTERNS = [
        "ignore previous instructions",
        "ignore all previous",
        "disregard the above",
        "system prompt",
        "</s>",
        "[INST]",
    ]
    response_lower = response.lower()
    for pattern in INJECTION_PATTERNS:
        if pattern in response_lower:
            return True
    return False


def redact_pii_in_output(text: str) -> tuple[str, bool]:
    """Replace detected PII patterns with [REDACTED].

    Returns (redacted_text, pii_was_found).
    Patterns: PESEL (11 digits), NIP (10 digits), Polish phone, e-mail.
    """
    import re

    pii_found = False
    patterns = [
        (r"\b\d{11}\b", "[REDACTED_PESEL]"),           # PESEL
        (r"\b\d{10}\b", "[REDACTED_NIP]"),              # NIP
        (r"\b\+?48[\s-]?\d{3}[\s-]?\d{3}[\s-]?\d{3}\b", "[REDACTED_PHONE]"),
        (r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", "[REDACTED_EMAIL]"),
    ]
    for pattern, replacement in patterns:
        new_text, count = re.subn(pattern, replacement, text)
        if count:
            pii_found = True
            text = new_text
    return text, pii_found
```

NOTE: `node_guardrails_output` calls `_check_prompt_injection_echo` and `_redact_pii_in_output` — these must be imported from `utils.py`, not defined locally. The signatures in node code must match:
- `check_prompt_injection_echo(answer, relevant_chunks)` — raises `GuardrailsViolation` if injection detected
- `redact_pii_in_output(answer)` — returns `(str, bool)` tuple

### Step 1: State, schemas, routing scaffolding

Implement `state.py`, `routing.py`. Run `mypy src/graphs/query_graph/state.py` — must pass clean.

### Step 2: node_classify_intent

File: `src/graphs/query_graph/nodes/node_classify_intent.py`

Prompt: `src/graphs/prompts/classify_intent_v1.md`

```python
from src.graphs.query_graph.state import QueryState, IntentType
from src.graphs.query_graph.tracing import span
from src.graphs.query_graph.utils import load_conversation_history, sum_prompt_tokens, sum_completion_tokens

async def node_classify_intent(
    state: QueryState,
    *,
    llm: LLMClient,
    db: AsyncSession,
) -> dict:
    """Classify the user's question intent. Also loads conversation history for multi-turn context.

    Intents: factual | procedural | comparative | meta | out_of_scope | small_talk.
    out_of_scope and small_talk bypass retrieval entirely.

    Loads conversation_history here (first node with DB access) so subsequent nodes
    (node_generate) can read state.conversation_history without needing a DB parameter.
    """
    # Load conversation history once; stored in state for node_generate to consume.
    history = await load_conversation_history(db, state.conversation_id, limit=5)

    with span("classify_intent", capture_input=False, capture_output=False) as s:
        prompt = load_prompt("classify_intent", version=1)
        response = await llm.chat_completion(
            model=state.pipeline_config.llm_model_name,
            base_url=state.pipeline_config.llm_endpoint_url,
            messages=[
                {"role": "system", "content": prompt.system},
                {"role": "user", "content": state.question},
            ],
            response_format={"type": "json_object"},
            max_tokens=50,
        )
        parsed = _parse_intent(response.choices[0].message.content)
        s.update(intent=parsed, tokens_in=response.usage.prompt_tokens,
                 tokens_out=response.usage.completion_tokens)

    return {
        "intent": parsed,
        "conversation_history": history,
        "prompt_tokens": state.prompt_tokens + sum_prompt_tokens(response),
        "completion_tokens": state.completion_tokens + sum_completion_tokens(response),
    }

def _parse_intent(json_str: str) -> IntentType:
    import json
    data = json.loads(json_str)
    intent = data.get("intent", "factual").lower()
    valid = {"factual", "procedural", "comparative", "meta", "out_of_scope", "small_talk"}
    return intent if intent in valid else "factual"
```

Prompt file `classify_intent_v1.md` must define JSON output schema `{"intent": "<value>"}`.

### Step 3: node_rewrite_query

File: `src/graphs/query_graph/nodes/node_rewrite_query.py`

Strategy: HyDE (Hypothetical Document Embedding) for dense retrieval; preserve original question in state for BM25 fallback reference.

```python
async def node_rewrite_query(
    state: QueryState,
    *,
    llm: LLMClient,
) -> dict:
    """Rewrite query using HyDE for improved dense retrieval.

    Generates a hypothetical ideal answer paragraph that will be embedded
    and used as the query vector — semantically closer to actual document content.
    """
    prompt = load_prompt("rewrite_query", version=1)
    response = await llm.chat_completion(
        model=state.pipeline_config.llm_model_name,
        base_url=state.pipeline_config.llm_endpoint_url,
        messages=[
            {"role": "system", "content": prompt.system},
            {"role": "user", "content": prompt.user.format(question=state.question)},
        ],
        max_tokens=300,
    )
    rewritten = response.choices[0].message.content.strip()
    return {
        "rewritten_query": rewritten,
        "prompt_tokens": state.prompt_tokens + response.usage.prompt_tokens,
        "completion_tokens": state.completion_tokens + response.usage.completion_tokens,
    }
```

### Step 4: node_retrieve

File: `src/graphs/query_graph/nodes/node_retrieve.py`

```python
from src.retrieval.service import RetrievalService
from src.retrieval.schemas import TenantContext
# _get_embedding_model_name and _get_embedding_base_url are local helpers defined
# at the bottom of node_retrieve.py — they read from PipelineConfig, no DB needed.

def _get_embedding_model_name(pipeline_config: PipelineConfig) -> str:
    """Extract the embedding model name from pipeline config."""
    return pipeline_config.prompt_config.get("embedding_model_name", "bge-m3")

def _get_embedding_base_url(pipeline_config: PipelineConfig) -> str:
    """Extract the embedding model base URL from pipeline config."""
    return pipeline_config.prompt_config.get("embedding_base_url", pipeline_config.llm_endpoint_url)

async def node_retrieve(
    state: QueryState,
    *,
    retrieval: RetrievalService,
    llm: LLMClient,        # needed for embedding the rewritten query
) -> dict:
    """Retrieve top-k semantically relevant chunks using RetrievalService.

    Also supports optional BM25 via pg_trgm (Phase 2). In MVP: dense retrieval only.
    """
    query_text = state.rewritten_query or state.question
    top_k = state.pipeline_config.prompt_config.get("top_k", 8)
    score_threshold = state.pipeline_config.prompt_config.get("score_threshold", 0.35)

    # Embed the query using the same model as the target collection
    embed_response = await llm.embeddings(
        model=_get_embedding_model_name(state.pipeline_config),
        input=[query_text],
        base_url=_get_embedding_base_url(state.pipeline_config),
    )
    query_vector = embed_response.data[0].embedding

    ctx = TenantContext(
        tenant_id=state.user_ctx.tenant_id,
        allowed_collection_ids=state.user_ctx.allowed_collection_ids,
    )

    raw_results = await retrieval.search(
        ctx=ctx,
        qdrant_collection=state.pipeline_config.qdrant_collection,
        query_vector=query_vector,
        top_k=top_k,
        score_threshold=score_threshold,
    )

    chunks = [
        RetrievedChunk(
            point_id=r.point_id,
            document_id=r.document_id,
            score=r.score,
            text=r.payload.get("text", ""),
            page=r.page_number,
            section=r.payload.get("section"),
            collection_id=r.collection_id,
        )
        for r in raw_results
    ]
    return {"retrieved_chunks": chunks}
```

### Step 5: node_grade_documents

File: `src/graphs/query_graph/nodes/node_grade_documents.py`

Prompt: `src/graphs/prompts/grade_documents_v1.md`

Grade each chunk independently. Filter chunks below threshold. If zero relevant chunks found, set `not_found=True` (routing picks up in `route_after_grade`).

```python
from src.core.config import settings
from src.graphs.query_graph.utils import sum_prompt_tokens, sum_completion_tokens

# Read threshold from settings so it is configurable without code changes.
# Add `retrieval_relevance_threshold: float = 0.5` to the Settings model (TASK-001).
RELEVANCE_THRESHOLD = settings.retrieval_relevance_threshold  # default 0.5

async def node_grade_documents(
    state: QueryState,
    *,
    llm: LLMClient,
) -> dict:
    """Grade each retrieved chunk for relevance to the original question.

    LLM assigns a relevance score 0.0–1.0 per chunk. Chunks below RELEVANCE_THRESHOLD
    (configured via settings.retrieval_relevance_threshold, default 0.5) are
    excluded from context. If no chunks pass, the graph routes to not_found.
    """
    prompt = load_prompt("grade_documents", version=1)
    graded: list[GradedChunk] = []
    total_prompt_tokens = 0
    total_completion_tokens = 0

    for chunk in state.retrieved_chunks:
        response = await llm.chat_completion(
            model=state.pipeline_config.llm_model_name,
            base_url=state.pipeline_config.llm_endpoint_url,
            messages=[
                {"role": "system", "content": prompt.system},
                {"role": "user", "content": prompt.user.format(
                    question=state.question,
                    # Chunk text passed as untrusted data with clear delimiters
                    chunk_text="<DOCUMENT_CHUNK>\n{text}\n</DOCUMENT_CHUNK>".format(
                        text=chunk.text[:1500]  # cap to avoid token overflow
                    ),
                )},
            ],
            response_format={"type": "json_object"},
            max_tokens=30,
        )
        score_data = json.loads(response.choices[0].message.content)
        relevance = float(score_data.get("relevance_score", 0.0))
        graded.append(GradedChunk(
            chunk=chunk,
            relevance_score=relevance,
            relevant=relevance >= RELEVANCE_THRESHOLD,
        ))
        total_prompt_tokens += sum_prompt_tokens(response)
        total_completion_tokens += sum_completion_tokens(response)

    return {
        "graded_chunks": graded,
        "not_found": not any(c.relevant for c in graded),
        "prompt_tokens": state.prompt_tokens + total_prompt_tokens,
        "completion_tokens": state.completion_tokens + total_completion_tokens,
    }
```

IMPORTANT: Chunk text is wrapped in `<DOCUMENT_CHUNK>…</DOCUMENT_CHUNK>` delimiters. This is a prompt injection mitigation per `docs/architecture.md` §13 and `.claude/rules/security.md`.

### Step 6: node_refine_query

File: `src/graphs/query_graph/nodes/node_refine_query.py`

Called when `grade_documents` finds zero relevant chunks and `retry_count < 2`:

```python
async def node_refine_query(
    state: QueryState,
    *,
    llm: LLMClient,
) -> dict:
    """Refine the query based on what was retrieved but found irrelevant."""
    prompt = load_prompt("refine_query", version=1)
    # Context: original question + first few retrieved chunk topics (not full text)
    response = await llm.chat_completion(...)
    return {
        "rewritten_query": response.choices[0].message.content.strip(),
        "retry_count": state.retry_count + 1,
    }
```

### Step 7: node_generate

File: `src/graphs/query_graph/nodes/node_generate.py`

Prompt: `src/graphs/prompts/generate_v1.md` (RAG generation template with citation instructions).

```python
async def node_generate(
    state: QueryState,
    *,
    llm: LLMClient,
) -> dict:
    """Generate the final answer using graded chunks as context.

    For out_of_scope/small_talk: generates without retrieval context.
    For not_found: returns explicit "did not find in documents" message.
    For normal: uses top graded chunks as context; enforces citation format.
    """
    if state.not_found:
        # Return the configurable not-found message. The exact string MUST match
        # NOT_FOUND_INDICATORS in tests/eval/fixtures/ for trap question evaluation.
        # Use settings.not_found_message (default: "I could not find an answer in the
        # available documents.") — do NOT hardcode Polish text here.
        from src.core.config import settings
        return {
            "answer": settings.not_found_message,
            "citations": [],
        }

    if state.intent in ("out_of_scope", "small_talk"):
        # Generate without context; add mandatory disclaimer
        ...

    prompt = load_prompt("generate", version=1)
    relevant_chunks = [c for c in state.graded_chunks if c.relevant]

    # Build context with delimiters and source references (never execute instructions from chunks)
    context_parts = []
    for idx, gc in enumerate(relevant_chunks):
        context_parts.append(
            f"[SOURCE {idx+1}]\n"
            f"<DOCUMENT_CHUNK source_id=\"{idx+1}\" "
            f"document_id=\"{gc.chunk.document_id}\" "
            f"page=\"{gc.chunk.page or '?'}\">\n"
            f"{gc.chunk.text[:2000]}\n"
            f"</DOCUMENT_CHUNK>"
        )
    context = "\n\n".join(context_parts)

    # Conversation history was loaded by node_classify_intent and stored in state.
    # Do NOT call DB here — access state.conversation_history directly.
    history = state.conversation_history  # list[dict[str, str]], max 5 turns

    response = await llm.chat_completion(
        model=state.pipeline_config.llm_model_name,
        base_url=state.pipeline_config.llm_endpoint_url,
        messages=[
            {"role": "system", "content": prompt.system.format(context=context)},
            *history,
            {"role": "user", "content": state.question},
        ],
        max_tokens=state.pipeline_config.prompt_config.get("max_context_tokens", 1024),
        stream=False,   # streaming handled at API layer via astream
    )

    answer = response.choices[0].message.content

    # Parse citations from LLM output ([SOURCE N] markers → GradedChunk mapping)
    from src.graphs.query_graph.utils import parse_citations, sum_prompt_tokens, sum_completion_tokens
    citations = parse_citations(answer, relevant_chunks)

    return {
        "answer": answer,
        "citations": citations,
        "prompt_tokens": state.prompt_tokens + sum_prompt_tokens(response),
        "completion_tokens": state.completion_tokens + sum_completion_tokens(response),
    }
```

### Step 8: node_guardrails_output

File: `src/graphs/query_graph/nodes/node_guardrails_output.py`

```python
import re
from src.graphs.query_graph.utils import check_prompt_injection_echo, redact_pii_in_output
from src.core.exceptions import GuardrailsViolation

def _replace_blocked_content(answer: str, topic: str) -> str:
    """Replace occurrences of a blocked topic in the answer with a neutral placeholder."""
    pattern = re.compile(re.escape(topic), re.IGNORECASE)
    return pattern.sub("[BLOCKED CONTENT]", answer)

async def node_guardrails_output(
    state: QueryState,
) -> dict:
    """Apply output guardrails: PII filter, disclaimer, prompt injection defense.

    Rule-based in MVP. Phase 3: optional LLM guardrail model.
    All helpers are imported from utils.py — not defined inline.
    """
    answer = state.answer
    guardrails = state.pipeline_config.guardrails
    triggered = False

    # 1. Detect if the LLM echoed back prompt injection text from chunks
    # check_prompt_injection_echo returns True if injection echo detected.
    # Raise GuardrailsViolation so the graph can route to a safe fallback.
    relevant_chunks = [c for c in state.graded_chunks if c.relevant]
    if check_prompt_injection_echo(answer, relevant_chunks):
        raise GuardrailsViolation("Prompt injection echo detected in LLM output")

    # 2. PII filter on output (regex-based, same patterns as pii_scan node)
    if guardrails.get("pii_filter", True):
        answer, pii_found = redact_pii_in_output(answer)
        if pii_found:
            triggered = True

    # 3. Industry disclaimer (mandatory for medical domain per tenant settings)
    if guardrails.get("disclaimer_required", False):
        disclaimer = guardrails.get("disclaimer_text", "")
        if disclaimer and disclaimer not in answer:
            answer = f"{answer}\n\n---\n{disclaimer}"

    # 4. Blocked topics check
    blocked = guardrails.get("blocked_topics", [])
    for topic in blocked:
        if topic.lower() in answer.lower():
            answer = _replace_blocked_content(answer, topic)
            triggered = True

    return {
        "answer": answer,
        "guardrails_triggered": triggered,
    }
```

### Step 9: node_persist

File: `src/graphs/query_graph/nodes/node_persist.py`

```python
from src.graphs.query_graph.utils import elapsed_ms

async def node_persist(
    state: QueryState,
    *,
    db: AsyncSession,
) -> dict:
    """Persist assistant message, citations, and metrics. Update conversation. Write audit log."""
    now = utcnow()
    # state.started_at is set by QueryState default_factory=time.monotonic at graph entry.
    latency_ms = elapsed_ms(state.started_at)

    # 1. Insert assistant message
    await db.execute(
        update(Message)
        .where(Message.id == state.message_id,
               # message.conversation.tenant_id verified via join
               )
        .values(
            content=state.answer,
            prompt_tokens=state.prompt_tokens,
            completion_tokens=state.completion_tokens,
            latency_ms=latency_ms,
        )
    )

    # 2. Resolve chunk_ids from chunks_registry (qdrant_point_id → chunks_registry.id)
    point_ids = [str(c.point_id) for c in state.citations]
    chunk_rows = await db.execute(
        select(ChunksRegistry.qdrant_point_id, ChunksRegistry.id)
        .where(
            ChunksRegistry.qdrant_point_id.in_(point_ids),
            ChunksRegistry.tenant_id == state.user_ctx.tenant_id,
        )
    )
    point_to_chunk = {str(r.qdrant_point_id): r.id for r in chunk_rows}

    # 3. Insert message_sources
    source_rows = [
        MessageSource(
            message_id=state.message_id,
            document_id=citation.document_id,
            chunk_id=point_to_chunk.get(str(citation.point_id)),   # None if chunk deleted
            relevance_score=citation.score,
            highlight_text=citation.highlight_text,
            page_number=citation.page_number,
        )
        for citation in state.citations
    ]
    if source_rows:
        await db.execute(insert(MessageSource).values([s.__dict__ for s in source_rows]))

    # 4. Bump conversation updated_at
    await db.execute(
        update(Conversation)
        .where(Conversation.id == state.conversation_id)
        .values(updated_at=now)
    )

    # 5. Audit log — no message content, no chunk text
    await insert_audit_log(db, AuditEntry(
        tenant_id=state.user_ctx.tenant_id,
        user_id=state.user_ctx.user_id,
        action="chat.query",
        resource_type="conversation",
        resource_id=state.conversation_id,
        details={
            "pipeline_id": str(state.pipeline_config.pipeline_id),
            "intent": state.intent,
            "chunks_used": len([c for c in state.graded_chunks if c.relevant]),
            "latency_ms": round(latency_ms, 1),
            "guardrails_triggered": state.guardrails_triggered,
        },
    ))

    return {"latency_ms": latency_ms}
```

### Step 10: Tracing integration (`tracing.py`)

```python
from langfuse import Langfuse
from contextlib import contextmanager

_langfuse = Langfuse()   # configured from LANGFUSE_* env vars

@contextmanager
def span(name: str, capture_input: bool = False, capture_output: bool = False):
    """Context manager for a Langfuse span. capture_input and capture_output
    are always False per GDPR/PII protection rule in docs/architecture.md §17."""
    s = _langfuse.start_span(name=name, metadata={"capture_input": False, "capture_output": False})
    try:
        yield s
    except Exception as exc:
        s.update(level="ERROR", status_message=str(exc))
        raise
    finally:
        s.end()
```

The trace wraps the entire graph invocation. Each node creates a span inside. The trace root uses `session_id=str(conversation_id)` and metadata `{"tenant_id": str(tenant_id), "user_id": str(user_id)}` — no PII.

---

## API Contracts

The query graph is not a direct HTTP endpoint. It is invoked by the chat router (TASK-011). The graph's input and output types are the contract:

### Input

```python
QueryState(
    question="...",
    user_ctx=UserContext(...),
    pipeline_config=PipelineConfig(...),
    conversation_id=UUID(...),
    message_id=UUID(...),
)
```

### Output fields consumed by the chat router

```python
{
    "answer": str,
    "citations": list[Citation],
    "intent": IntentType,
    "prompt_tokens": int,
    "completion_tokens": int,
    "latency_ms": float,
    "guardrails_triggered": bool,
}
```

### Citation format (for `message_sources` in API response)

Per `docs/architecture.md` §4 and TASK-011:

```python
class Citation(BaseModel):
    document_id: UUID
    chunk_id: UUID | None
    page_number: int | None
    highlight_text: str | None
    score: float
```

---

## Security Checklist

- [ ] Chunk text is always wrapped in `<DOCUMENT_CHUNK>…</DOCUMENT_CHUNK>` delimiters when passed to the LLM. This isolates document content from instruction space.
- [ ] `node_guardrails_output` checks for prompt injection echo (LLM parroting chunk instructions back in the answer).
- [ ] `node_persist` does NOT log `state.answer` or `state.question`. Only IDs and metrics in audit_log.details.
- [ ] Langfuse spans use `capture_input=False` and `capture_output=False` on every span. Verified in `tests/unit/query_graph/test_node_persist.py` by inspecting Langfuse mock calls.
- [ ] `node_retrieve` calls only `RetrievalService.search()` — no direct Qdrant client. Import linter enforces this.
- [ ] `TenantContext.allowed_collection_ids` is derived from the JWT-resolved user context, never from the request body.
- [ ] `route_after_classify`: out_of_scope and small_talk skip retrieval, preventing users from probing document collections with off-topic queries.
- [ ] PII redaction in `node_guardrails_output` is a defense-in-depth layer — the LLM should not produce PII, but if it does, the output is redacted before being returned.
- [ ] `node_generate` for `not_found` path returns a hardcoded non-hallucinated response. The LLM is NOT called in this branch for the answer text.
- [ ] Conversation history loaded in `node_generate` is limited to 5 turns to prevent prompt stuffing.

---

## Terms of Use (relevant constraints)

- **Node topology is fixed** per `docs/architecture.md` §7. Any topology change requires a diagram update in the architecture doc and ADR sign-off.
- **All prompts in `src/graphs/prompts/`**: `classify_intent_v1.md`, `rewrite_query_v1.md`, `grade_documents_v1.md`, `refine_query_v1.md`, `generate_v1.md`. No inline prompt text in node code.
- **Retrieval is only via `RetrievalService.search()`** — `node_retrieve` does not import `qdrant_client`.
- **Grading threshold** is read from `settings.retrieval_relevance_threshold` (default 0.5). Add this field to the Settings model in TASK-001: `retrieval_relevance_threshold: float = 0.5`. Phase 2: also allow per-pipeline override via `rag_pipelines.prompt_config.grade_threshold`.
- **HyDE rewrite**: the rewritten query is used for embedding, not for display to the user. The original question (`state.question`) is what appears in the conversation.
- **Streaming** (`astream`): the chat router calls `graph.astream()` and yields SSE chunks. The `node_generate` node itself calls the LLM in non-streaming mode (full response then yield). Phase 3: LangGraph streaming from within the generate node.

---

## Tests

### Unit tests (`tests/unit/query_graph/`)

All LLM calls are `AsyncMock`. No real network I/O. Use `base_state` fixture from `tests/unit/query_graph/conftest.py` (defined in TASK-016).

**`test_node_classify_intent.py`**

| Test | Setup | Assert |
|---|---|---|
| `test_factual_intent` | LLM returns `{"intent": "factual"}` | State `intent == "factual"` |
| `test_out_of_scope_intent` | LLM returns `{"intent": "out_of_scope"}` | `intent == "out_of_scope"` |
| `test_invalid_intent_defaults_to_factual` | LLM returns `{"intent": "garbage"}` | `intent == "factual"` (safe default) |
| `test_tokens_accumulated` | LLM returns usage | `prompt_tokens` updated |

**`test_node_rewrite_query.py`**

| Test | Setup | Assert |
|---|---|---|
| `test_rewrite_returns_new_query` | LLM returns HyDE paragraph | `rewritten_query` is set and non-empty |
| `test_original_question_preserved` | After rewrite | `state.question` unchanged |

**`test_node_retrieve.py`**

| Test | Setup | Assert |
|---|---|---|
| `test_retrieve_uses_retrieval_service` | `RetrievalService.search` is AsyncMock | Called with `ctx.tenant_id` and `allowed_collection_ids` from user_ctx |
| `test_retrieve_passes_top_k_from_config` | `pipeline_config.prompt_config["top_k"] = 5` | RetrievalService called with `top_k=5` |
| `test_retrieve_no_direct_qdrant` | Import inspection | `node_retrieve.py` has no `qdrant_client` import |

**`test_node_grade_documents.py`**

| Test | Setup | Assert |
|---|---|---|
| `test_chunk_above_threshold_marked_relevant` | LLM returns `{"relevance_score": 0.8}` | `graded_chunk.relevant == True` |
| `test_chunk_below_threshold_not_relevant` | LLM returns `{"relevance_score": 0.3}` | `graded_chunk.relevant == False` |
| `test_zero_relevant_sets_not_found` | All chunks score 0.0 | `state.not_found == True` |
| `test_chunk_text_wrapped_in_delimiters` | Inspect LLM call args | Prompt contains `<DOCUMENT_CHUNK>` wrapper |

**`test_node_generate.py`**

| Test | Setup | Assert |
|---|---|---|
| `test_not_found_returns_hardcoded_message` | `state.not_found = True` | LLM not called; answer equals `settings.not_found_message` (default: `"I could not find an answer in the available documents."`). The string must appear in `NOT_FOUND_INDICATORS` in `tests/eval/fixtures/` — verified by trap question eval. Add `not_found_message: str = "I could not find an answer in the available documents."` to Settings (TASK-001). |
| `test_disclaimer_added_for_out_of_scope` | `intent = "out_of_scope"`, disclaimer required | Answer includes disclaimer text |
| `test_citations_parsed_from_response` | LLM returns answer with citation markers | `citations` list populated |
| `test_llm_unavailable_raises_503` | LLM mock always raises | Raises `LLMUnavailableError` |

**`test_node_guardrails_output.py`**

| Test | Setup | Assert |
|---|---|---|
| `test_pii_redacted_in_answer` | Answer contains PESEL pattern | Output answer has PESEL replaced |
| `test_disclaimer_appended` | `guardrails.disclaimer_required = True` | Answer ends with disclaimer |
| `test_blocked_topic_replaced` | Answer mentions blocked topic | Content replaced |
| `test_langfuse_capture_false` | Inspect Langfuse mock | No span attribute contains answer text |

**`test_node_persist.py`**

| Test | Setup | Assert |
|---|---|---|
| `test_assistant_message_written` | Full state with answer | `messages` table has assistant row with correct `conversation_id` |
| `test_message_sources_written` | 3 citations in state | 3 rows in `message_sources` |
| `test_chunk_id_null_when_chunk_deleted` | `chunks_registry` has no row for point_id | `message_sources.chunk_id IS NULL` |
| `test_audit_log_no_content` | Normal flow | `audit_log.details` has no keys containing answer or question text |
| `test_langfuse_no_answer_in_span` | Inspect Langfuse mock call args | No span attribute is the answer text |

### Integration tests (`tests/integration/test_query_pipeline.py`)

| Test | Assert |
|---|---|
| `test_full_query_happy_path` | Factual question → retrieval → generation; message + sources in DB |
| `test_out_of_scope_skips_retrieval` | RetrievalService.search never called; answer includes disclaimer |
| `test_retry_on_no_relevant_chunks` | First retrieve returns irrelevant chunks; refine_query runs; second retrieve succeeds |
| `test_not_found_hardcoded_response` | After 2 retries still no relevant chunks; hardcoded not-found answer |

---

## Definition of Done

- [ ] All 7 required node files exist (plus `node_refine_query.py` for the retry branch).
- [ ] `QueryState` is fully type-annotated Pydantic model.
- [ ] `build_query_graph()` compiles without error.
- [ ] All prompts referenced in nodes exist in `src/graphs/prompts/` as versioned `.md` files.
- [ ] Langfuse spans use `capture_input=False`, `capture_output=False` — verified by unit test.
- [ ] `node_retrieve` has no `qdrant_client` import (import linter test passes).
- [ ] Chunk text wrapped in `<DOCUMENT_CHUNK>` delimiters in `node_grade_documents` and `node_generate`.
- [ ] `not_found` path returns hardcoded message; LLM is not called for answer generation.
- [ ] `ruff check --fix . && mypy src/ && pytest tests/unit/query_graph/ -x -q` green.
- [ ] Integration tests `tests/integration/test_query_pipeline.py` pass with testcontainers.
- [ ] `tests/security/test_tenant_isolation.py` tests involving the query graph pass.

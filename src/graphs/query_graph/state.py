"""QueryState — Pydantic BaseModel for the LangGraph query pipeline.

GDPR rules:
- question content must never appear in logs.
- answer and citations content must never appear in logs.
- graded_chunks text must never appear in logs.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class QueryState(BaseModel):
    """Shared state for the query graph pipeline.

    Identity fields are injected by ChatService and never mutated by nodes.
    Intermediate fields are set by individual nodes.
    """

    # Identity — injected by ChatService, never mutated
    question: str
    conversation_id: UUID
    tenant_id: UUID
    allowed_collection_ids: list[UUID]
    pipeline_id: UUID
    llm_model_id: UUID  # from pipeline
    collection_ids: list[UUID]  # from pipeline
    prompt_config: dict[str, Any] = Field(default_factory=dict)
    guardrails_config: dict[str, Any] = Field(default_factory=dict)
    conversation_history: list[dict[str, str]] = Field(
        default_factory=list
    )  # [{"role": "user/assistant", "content": "..."}]

    # Intermediate — set by nodes
    intent: str | None = None  # "topical" | "chitchat" | "out_of_scope"
    rewritten_query: str | None = None
    embedding_model_id: UUID | None = None  # resolved from collection
    qdrant_collection: str | None = None  # e.g. "emb_bge_m3"
    retrieved_chunks: list[dict[str, Any]] = Field(default_factory=list)
    graded_chunks: list[dict[str, Any]] = Field(default_factory=list)

    # Reranking — node_rerank is a pass-through when rerank_enabled=False (default)
    rerank_enabled: bool = False
    rerank_top_k: int | None = None  # None → keep all chunks after reranking

    # Token budget — controls context trimming in node_generate
    context_token_budget: int | None = None  # None → use prompt_config.max_context_tokens
    chunks_trimmed: int = 0  # count of chunks dropped due to budget; for observability
    context_tokens_used: int = 0  # actual token count of context sent to LLM (ADR-021)
    chunks_included: int = 0  # count of chunks that fit within the budget (ADR-021)

    # Graph RAG — set by node_graph_retrieve (opt-in via prompt_config.graph_rag_enabled)
    # Each element: {"entity": str, "entity_type": str, "icd_code": str|None,
    #                "related": [{"name": str, "relation": str, "confidence": float}]}
    # None when graph_rag_enabled=False or no matching entities were found.
    graph_context: list[dict[str, Any]] | None = None

    # Output — set by generate / guardrails
    answer: str | None = None
    citations: list[dict[str, Any]] = Field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    no_results: bool = False
    halt: bool = False  # True → guardrails short-circuit

    # Multi-language support (TASK-027) — set by node_detect_language / node_translate_query
    detected_language: str | None = None  # ISO 639-3: "pol", "eng"
    translated_query: str | None = None  # query in collection's primary language
    cross_language_retrieval: bool = False  # True when parallel retrieval was done
    response_language: str = "pol"  # language for final answer

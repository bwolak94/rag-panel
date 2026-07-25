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

    # Output — set by generate / guardrails
    answer: str | None = None
    citations: list[dict[str, Any]] = Field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    no_results: bool = False
    halt: bool = False  # True → guardrails short-circuit

"""LangGraph query pipeline factory.

build_query_graph() compiles the query pipeline with optional Postgres checkpointer.
invoke_query_graph() is the entry point called by ChatService._invoke_graph().

Graph topology — standard (fixed — change requires ADR):
    node_classify_intent →[cond]→ node_rewrite_query → node_retrieve → node_rerank
    → node_grade_documents →[cond]→ node_generate → node_guardrails_output → END

Graph topology — with Graph RAG (opt-in via prompt_config.graph_rag_enabled):
    node_classify_intent →[cond]→ node_rewrite_query → node_retrieve ──┐
                                                 └→ node_graph_retrieve ─┤
                                                                         ↓
                                                                   node_rerank
    → node_grade_documents →[cond]→ node_generate → node_guardrails_output → END

node_rerank is a transparent pass-through when QueryState.rerank_enabled=False (default).

Per docs/02-Architektura.md ADR-9.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from langfuse import observe
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clients.llm_client import LLMClient
from src.core.exceptions import TenantIsolationError
from src.core.langfuse_client import update_span_metadata as _lf_update_span
from src.db.models.rag_pipeline import RagPipeline
from src.domain.auth import UserContext
from src.graphs.query_graph import nodes
from src.graphs.query_graph.routing import (
    route_after_classify,
    route_after_detect_language,
    route_after_grade,
)
from src.graphs.query_graph.state import QueryState
from src.retrieval.service import RetrievalService

logger = structlog.get_logger(__name__)


def build_query_graph(
    checkpointer: Any = None,
    graph_rag_enabled: bool = False,
) -> Any:
    """Compile and return the query StateGraph.

    Args:
        checkpointer: Optional AsyncPostgresSaver for checkpoint-based graph resumption.
                      Pass None for testing or when langgraph-checkpoint-postgres is
                      not configured.
        graph_rag_enabled: When True, node_graph_retrieve is added in parallel with
                           node_retrieve; both converge at node_rerank.
                           Controlled by pipeline.prompt_config["graph_rag_enabled"].
                           Defaults to False (standard pipeline).

    Returns:
        Compiled LangGraph CompiledStateGraph.
    """
    from langgraph.graph import END, StateGraph

    builder: StateGraph = StateGraph(QueryState)  # type: ignore[type-arg]

    builder.add_node("node_classify_intent", nodes.node_classify_intent)  # type: ignore[call-overload]
    builder.add_node("node_rewrite_query", nodes.node_rewrite_query)  # type: ignore[call-overload]
    builder.add_node("node_detect_language", nodes.node_detect_language)  # type: ignore[call-overload]
    builder.add_node("node_translate_query", nodes.node_translate_query)  # type: ignore[call-overload]
    builder.add_node("node_retrieve", nodes.node_retrieve)  # type: ignore[call-overload]
    builder.add_node("node_rerank", nodes.node_rerank)  # type: ignore[call-overload]
    builder.add_node("node_grade_documents", nodes.node_grade_documents)  # type: ignore[call-overload]
    builder.add_node("node_generate", nodes.node_generate)  # type: ignore[call-overload]
    builder.add_node("node_guardrails_output", nodes.node_guardrails_output)  # type: ignore[call-overload]

    builder.set_entry_point("node_classify_intent")

    builder.add_conditional_edges(
        "node_classify_intent",
        route_after_classify,
        {
            "node_rewrite_query": "node_rewrite_query",
            "node_guardrails_output": "node_guardrails_output",
        },
    )

    # Language detection always follows rewrite; conditional translation before retrieval.
    builder.add_edge("node_rewrite_query", "node_detect_language")
    builder.add_conditional_edges(
        "node_detect_language",
        route_after_detect_language,
        {
            "node_translate_query": "node_translate_query",
            "node_retrieve": "node_retrieve",
        },
    )
    builder.add_edge("node_translate_query", "node_retrieve")

    if graph_rag_enabled:
        # Parallel KG branch: node_rewrite_query fans out to node_graph_retrieve;
        # converges with node_retrieve at node_rerank.
        builder.add_node(  # type: ignore[call-overload]
            "node_graph_retrieve", nodes.node_graph_retrieve
        )
        builder.add_edge("node_rewrite_query", "node_graph_retrieve")
        builder.add_edge("node_retrieve", "node_rerank")
        builder.add_edge("node_graph_retrieve", "node_rerank")
    else:
        builder.add_edge("node_retrieve", "node_rerank")

    builder.add_edge("node_rerank", "node_grade_documents")

    builder.add_conditional_edges(
        "node_grade_documents",
        route_after_grade,
        {
            "node_generate": "node_generate",
            "node_guardrails_output": "node_guardrails_output",
        },
    )

    builder.add_edge("node_generate", "node_guardrails_output")
    builder.add_edge("node_guardrails_output", END)

    return builder.compile(checkpointer=checkpointer)


def _assert_pipeline_collections_authorized(
    pipeline: RagPipeline,
    ctx: UserContext,
) -> None:
    """Verify that every collection referenced by the pipeline is readable by the user.

    Security invariant: pipeline.collection_ids ⊆ ctx.allowed_collection_ids.

    Admin users are represented by an empty allowed_collection_ids set (no restrictions),
    so when that set is empty we skip the check — admins can read all tenant collections.
    If allowed_collection_ids is non-empty the pipeline must not reference any collection
    outside that set; a violation raises TenantIsolationError (→ HTTP 403).

    Args:
        pipeline: The RagPipeline ORM object whose collection_ids are being verified.
        ctx: Verified UserContext from JWT.

    Raises:
        TenantIsolationError: If any pipeline collection_id is not in allowed_collection_ids.
    """
    # Empty allowed_collection_ids signals "no restriction" (admin / superuser).
    if not ctx.allowed_collection_ids:
        return

    pipeline_set = set(pipeline.collection_ids)
    allowed_set = set(ctx.allowed_collection_ids)

    forbidden = pipeline_set - allowed_set
    if forbidden:
        logger.error(
            "pipeline_collection_authorization_violation",
            tenant_id=str(ctx.tenant_id),
            user_id=str(ctx.user_id),
            pipeline_id=str(pipeline.id),
            # Log count only — do not log UUIDs that could confirm collection existence
            forbidden_count=len(forbidden),
        )
        raise TenantIsolationError("Access denied")


@observe(capture_input=False, capture_output=False)
async def invoke_query_graph(
    *,
    question: str,
    pipeline: RagPipeline,
    ctx: UserContext,
    db: AsyncSession,
    llm: LLMClient,
    retrieval: RetrievalService,
    conversation_history: list[dict[str, str]],
) -> tuple[str, list[dict[str, Any]], bool, int, int]:
    """Execute the query graph and return results.

    Args:
        question: The user's question (last user message).
        pipeline: RagPipeline ORM object with collection_ids, llm_model_id, etc.
        ctx: Verified UserContext from JWT.
        db: AsyncSession for DB queries within nodes.
        llm: LLMClient for LLM/embedding calls.
        retrieval: RetrievalService for Qdrant access.
        conversation_history: Last N messages as [{"role": ..., "content": ...}].

    Returns:
        Tuple of (answer, citations_as_dicts, no_results, prompt_tokens, completion_tokens).

    Raises:
        TenantIsolationError: If pipeline references collections the user cannot read.
        QueryNodeError: On node-level failures.
    """
    # SECURITY: Verify pipeline.collection_ids ⊆ ctx.allowed_collection_ids before
    # seeding the graph state.  A misconfigured pipeline must not grant access to
    # collections the requesting user is not authorised to read.
    _assert_pipeline_collections_authorized(pipeline, ctx)

    graph_rag_enabled: bool = bool((pipeline.prompt_config or {}).get("graph_rag_enabled", False))
    graph = build_query_graph(graph_rag_enabled=graph_rag_enabled)

    _lf_update_span(
        {
            "tenant_id": str(ctx.tenant_id),
            "pipeline_id": str(pipeline.id),
        }
    )

    initial_state = QueryState(
        question=question,
        conversation_id=UUID(int=0),  # placeholder; conversation tracked by ChatService
        tenant_id=ctx.tenant_id,
        allowed_collection_ids=list(ctx.allowed_collection_ids),
        pipeline_id=pipeline.id,
        llm_model_id=pipeline.llm_model_id,
        collection_ids=list(pipeline.collection_ids),
        prompt_config=pipeline.prompt_config or {},
        guardrails_config=pipeline.guardrails or {},
        conversation_history=conversation_history,
    )

    graph_config: dict[str, Any] = {
        "configurable": {
            "db": db,
            "llm": llm,
            "retrieval": retrieval,
        }
    }

    logger.info(
        "query_graph_started",
        tenant_id=str(ctx.tenant_id),
        pipeline_id=str(pipeline.id),
    )

    final_state: dict[str, Any] = await graph.ainvoke(
        initial_state.model_dump(), config=graph_config
    )

    answer: str = final_state.get("answer") or ""
    citations: list[dict[str, Any]] = final_state.get("citations") or []
    no_results: bool = final_state.get("no_results") or False
    prompt_tokens: int = final_state.get("prompt_tokens") or 0
    completion_tokens: int = final_state.get("completion_tokens") or 0

    _lf_update_span(
        {
            "no_results": no_results,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        }
    )

    logger.info(
        "query_graph_completed",
        tenant_id=str(ctx.tenant_id),
        pipeline_id=str(pipeline.id),
        no_results=no_results,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )

    return answer, citations, no_results, prompt_tokens, completion_tokens

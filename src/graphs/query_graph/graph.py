"""LangGraph query pipeline factory.

build_query_graph() compiles the 6-node pipeline with optional Postgres checkpointer.
invoke_query_graph() is the entry point called by ChatService._invoke_graph().

Graph topology (fixed — change requires ADR):
    node_classify_intent →[cond]→ node_rewrite_query → node_retrieve → node_grade_documents
    →[cond]→ node_generate → node_guardrails_output → END

Per docs/02-Architektura.md ADR-9.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clients.llm_client import LLMClient
from src.db.models.rag_pipeline import RagPipeline
from src.domain.auth import UserContext
from src.graphs.query_graph import nodes
from src.graphs.query_graph.routing import route_after_classify, route_after_grade
from src.graphs.query_graph.state import QueryState
from src.retrieval.service import RetrievalService

logger = structlog.get_logger(__name__)


def build_query_graph(checkpointer: Any = None) -> Any:
    """Compile and return the query StateGraph.

    Args:
        checkpointer: Optional AsyncPostgresSaver for checkpoint-based graph resumption.
                      Pass None for testing or when langgraph-checkpoint-postgres is
                      not configured.

    Returns:
        Compiled LangGraph CompiledStateGraph.
    """
    from langgraph.graph import END, StateGraph

    builder: StateGraph = StateGraph(QueryState)

    builder.add_node("node_classify_intent", nodes.node_classify_intent)
    builder.add_node("node_rewrite_query", nodes.node_rewrite_query)
    builder.add_node("node_retrieve", nodes.node_retrieve)
    builder.add_node("node_grade_documents", nodes.node_grade_documents)
    builder.add_node("node_generate", nodes.node_generate)
    builder.add_node("node_guardrails_output", nodes.node_guardrails_output)

    builder.set_entry_point("node_classify_intent")

    builder.add_conditional_edges(
        "node_classify_intent",
        route_after_classify,
        {
            "node_rewrite_query": "node_rewrite_query",
            "node_guardrails_output": "node_guardrails_output",
        },
    )

    builder.add_edge("node_rewrite_query", "node_retrieve")
    builder.add_edge("node_retrieve", "node_grade_documents")

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
        QueryNodeError: On node-level failures.
    """
    graph = build_query_graph()

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

    logger.info(
        "query_graph_completed",
        tenant_id=str(ctx.tenant_id),
        pipeline_id=str(pipeline.id),
        no_results=no_results,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )

    return answer, citations, no_results, prompt_tokens, completion_tokens

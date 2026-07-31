"""LangGraph research pipeline factory.

build_research_graph() compiles the 3-node iterative retrieval pipeline.
invoke_research_graph() is the entry point called by ChatService._invoke_research_graph().

Graph topology (fixed — change requires ADR and diagram update in docs/02-Architektura.md):

    node_research_plan →[sufficient or max_steps?]→ node_research_synthesize → END
           ↑                        ↓
           └──── node_research_retrieve ←──────────┘

Activated when pipeline.prompt_config["research_mode"] is True.
"""

from __future__ import annotations

from typing import Any

import structlog
from langfuse import observe
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clients.llm_client import LLMClient
from src.core.exceptions import TenantIsolationError
from src.core.langfuse_client import update_span_metadata as _lf_update_span
from src.db.models.rag_pipeline import RagPipeline
from src.domain.auth import UserContext
from src.graphs.research_graph import nodes
from src.graphs.research_graph.routing import route_after_plan
from src.graphs.research_graph.state import ResearchState
from src.retrieval.service import RetrievalService

logger = structlog.get_logger(__name__)


def build_research_graph(checkpointer: Any = None) -> Any:
    """Compile and return the research StateGraph.

    Args:
        checkpointer: Optional AsyncPostgresSaver for checkpoint-based graph resumption.
                      Pass None for testing or when langgraph-checkpoint-postgres is
                      not configured.

    Returns:
        Compiled LangGraph CompiledStateGraph.
    """
    from langgraph.graph import END, StateGraph

    builder: StateGraph = StateGraph(ResearchState)  # type: ignore[type-arg]

    builder.add_node("node_research_plan", nodes.node_research_plan)  # type: ignore[call-overload]
    builder.add_node("node_research_retrieve", nodes.node_research_retrieve)  # type: ignore[call-overload]
    builder.add_node("node_research_synthesize", nodes.node_research_synthesize)  # type: ignore[call-overload]

    builder.set_entry_point("node_research_plan")

    builder.add_conditional_edges(
        "node_research_plan",
        route_after_plan,
        {
            "node_research_retrieve": "node_research_retrieve",
            "node_research_synthesize": "node_research_synthesize",
        },
    )

    # After retrieve: always loop back to plan for the next iteration
    builder.add_edge("node_research_retrieve", "node_research_plan")

    builder.add_edge("node_research_synthesize", END)

    return builder.compile(checkpointer=checkpointer)


def _assert_pipeline_collections_authorized(
    pipeline: RagPipeline,
    ctx: UserContext,
) -> None:
    """Verify that every collection referenced by the pipeline is readable by the user.

    Security invariant: pipeline.collection_ids ⊆ ctx.allowed_collection_ids.
    Admin users (empty allowed_collection_ids) bypass the check.

    Args:
        pipeline: RagPipeline ORM object.
        ctx: Verified UserContext from JWT.

    Raises:
        TenantIsolationError: If any pipeline collection_id is not authorised.
    """
    if not ctx.allowed_collection_ids:
        return

    pipeline_set = set(pipeline.collection_ids)
    allowed_set = set(ctx.allowed_collection_ids)
    forbidden = pipeline_set - allowed_set

    if forbidden:
        logger.error(
            "research_graph.pipeline_collection_authorization_violation",
            tenant_id=str(ctx.tenant_id),
            user_id=str(ctx.user_id),
            pipeline_id=str(pipeline.id),
            forbidden_count=len(forbidden),
        )
        raise TenantIsolationError("Access denied")


@observe(capture_input=False, capture_output=False)
async def invoke_research_graph(
    *,
    question: str,
    pipeline: RagPipeline,
    ctx: UserContext,
    db: AsyncSession,
    llm: LLMClient,
    retrieval: RetrievalService,
    conversation_history: list[dict[str, str]],
) -> tuple[str, list[dict[str, Any]], int, int]:
    """Execute the research graph and return results.

    Args:
        question: The user's question (last user message).
        pipeline: RagPipeline ORM object with collection_ids, llm_model_id, prompt_config.
        ctx: Verified UserContext from JWT.
        db: AsyncSession for DB queries within nodes.
        llm: LLMClient for LLM/embedding calls.
        retrieval: RetrievalService for Qdrant access.
        conversation_history: Last N messages as [{"role": ..., "content": ...}].

    Returns:
        Tuple of (answer, citations_as_dicts, prompt_tokens, completion_tokens).

    Raises:
        TenantIsolationError: If pipeline references collections the user cannot read.
        QueryNodeError: On node-level failures.
    """
    _assert_pipeline_collections_authorized(pipeline, ctx)

    graph = build_research_graph()

    prompt_config: dict[str, Any] = pipeline.prompt_config or {}
    max_steps: int = int(prompt_config.get("research_max_steps", 3))

    _lf_update_span(
        {
            "tenant_id": str(ctx.tenant_id),
            "pipeline_id": str(pipeline.id),
            "research_mode": True,
            "max_steps": max_steps,
        }
    )

    initial_state = ResearchState(
        original_question=question,
        pipeline_id=pipeline.id,
        tenant_id=ctx.tenant_id,
        collection_ids=list(pipeline.collection_ids),
        allowed_collection_ids=list(ctx.allowed_collection_ids),
        llm_model_id=pipeline.llm_model_id,
        conversation_history=conversation_history,
        max_steps=max_steps,
    )

    graph_config: dict[str, Any] = {
        "configurable": {
            "db": db,
            "llm": llm,
            "retrieval": retrieval,
        }
    }

    logger.info(
        "research_graph_started",
        tenant_id=str(ctx.tenant_id),
        pipeline_id=str(pipeline.id),
        max_steps=max_steps,
    )

    final_state: dict[str, Any] = await graph.ainvoke(
        initial_state.model_dump(), config=graph_config
    )

    answer: str = final_state.get("final_answer") or ""
    citations: list[dict[str, Any]] = final_state.get("citations") or []
    prompt_tokens: int = final_state.get("prompt_tokens") or 0
    completion_tokens: int = final_state.get("completion_tokens") or 0
    steps_taken: int = final_state.get("steps_taken") or 0

    _lf_update_span(
        {
            "steps_taken": steps_taken,
            "total_chunks": len(final_state.get("all_retrieved_chunks") or []),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        }
    )

    logger.info(
        "research_graph_completed",
        tenant_id=str(ctx.tenant_id),
        pipeline_id=str(pipeline.id),
        steps_taken=steps_taken,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )

    return answer, citations, prompt_tokens, completion_tokens

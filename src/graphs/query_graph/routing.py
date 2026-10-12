"""Conditional edge routing functions for the query graph.

Each function inspects state fields and returns the next node name.
Called by add_conditional_edges() in build_query_graph().
"""

from __future__ import annotations

from src.graphs.query_graph.state import QueryState


def route_after_classify(state: QueryState) -> str:
    """Route after classify_intent node.

    Returns:
        "node_rewrite_query" if intent is topical or multi_hop.
        "node_guardrails_output" for chitchat or out_of_scope (halt=True set by classify node).

    Note: multi_hop questions route to node_rewrite_query just like topical ones.
    This function is NOT aware of research_mode. When research_mode=True, ChatService
    bypasses the query graph entirely (invoking research_graph directly) so this
    routing function is never called for research-mode pipelines (ADR-026).
    When research_mode=False, multi_hop falls back to the standard retrieval path.
    """
    if state.intent in {"topical", "multi_hop"}:
        return "node_rewrite_query"
    return "node_guardrails_output"


def route_after_detect_language(state: QueryState) -> str:
    """Route after detect_language node.

    Returns:
        "node_translate_query" when detected_language differs from collection primary language
        and cross-language retrieval has not already been performed.
        "node_retrieve" otherwise (same language or already translated).
    """
    if (
        state.detected_language
        and state.detected_language != "pol"
        and not state.cross_language_retrieval
    ):
        return "node_translate_query"
    return "node_retrieve"


def route_after_grade(state: QueryState) -> str:
    """Route after grade_documents node.

    Returns:
        "node_generate" if graded_chunks is non-empty.
        "node_guardrails_output" if no chunks passed grading (no_results=True set by grade node).
    """
    if state.graded_chunks:
        return "node_generate"
    return "node_guardrails_output"

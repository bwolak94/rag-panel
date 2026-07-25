"""Conditional edge routing functions for the query graph.

Each function inspects state fields and returns the next node name.
Called by add_conditional_edges() in build_query_graph().
"""

from __future__ import annotations

from src.graphs.query_graph.state import QueryState


def route_after_classify(state: QueryState) -> str:
    """Route after classify_intent node.

    Returns:
        "node_rewrite_query" if intent is topical.
        "node_guardrails_output" for chitchat or out_of_scope (halt=True set by classify node).
    """
    if state.intent == "topical":
        return "node_rewrite_query"
    return "node_guardrails_output"


def route_after_grade(state: QueryState) -> str:
    """Route after grade_documents node.

    Returns:
        "node_generate" if graded_chunks is non-empty.
        "node_guardrails_output" if no chunks passed grading (no_results=True set by grade node).
    """
    if state.graded_chunks:
        return "node_generate"
    return "node_guardrails_output"

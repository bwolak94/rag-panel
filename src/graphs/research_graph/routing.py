"""Conditional edge routing for the research graph.

Called by add_conditional_edges() in build_research_graph().
The router inspects the most recent ResearchIteration to decide whether to
retrieve more evidence or proceed to final synthesis.
"""

from __future__ import annotations

from src.graphs.research_graph.state import ResearchState


def route_after_plan(state: ResearchState) -> str:
    """Route after node_research_plan.

    Proceeds to synthesis when either:
    1. The planning node declared sufficient=True (enough evidence accumulated), or
    2. The step budget is exhausted (len(iterations) >= max_steps).

    Otherwise routes back to node_research_retrieve for another retrieval pass.

    Args:
        state: ResearchState after node_research_plan has run. Must have at least
               one entry in iterations.

    Returns:
        "node_research_synthesize" or "node_research_retrieve".
    """
    if not state.iterations:
        # Defensive: no iteration recorded — proceed to synthesize with no evidence
        return "node_research_synthesize"

    latest = state.iterations[-1]

    if latest.sufficient:
        return "node_research_synthesize"

    if len(state.iterations) >= state.max_steps:
        return "node_research_synthesize"

    return "node_research_retrieve"

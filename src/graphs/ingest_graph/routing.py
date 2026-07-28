"""Conditional edge routing functions for the ingest graph.

Each function inspects state.halt / state.status and returns the next node name
or END. Called by add_conditional_edges() in build_ingest_graph().
"""

from __future__ import annotations

from langgraph.graph import END

from src.graphs.ingest_graph.state import IngestState


def route_after_dedupe(state: IngestState) -> str:
    """Halt on duplicate (state.halt=True) → END; otherwise → node_validate."""
    if state.halt:
        return END
    return "node_validate"


def route_after_validate(state: IngestState) -> str:
    """Halt on low-quality or needs_review → END; otherwise → node_pii_scan."""
    if state.status == "needs_review" or state.halt:
        return END
    return "node_pii_scan"


def route_after_pii(state: IngestState) -> str:
    """Halt on PII needs_review → END (checkpoint saved); otherwise → node_chunk."""
    if state.status == "needs_review":
        return END
    return "node_chunk"

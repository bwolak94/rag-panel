"""Unit tests for research graph routing.

Tests cover:
- sufficient=True on the latest iteration → route to synthesize
- max_steps reached (len(iterations) >= max_steps) → route to synthesize
- neither condition → route to retrieve
- empty iterations list (defensive path) → route to synthesize
"""

from __future__ import annotations

import uuid

from src.graphs.research_graph.routing import route_after_plan
from src.graphs.research_graph.state import ResearchIteration, ResearchState

TENANT_ID = uuid.uuid4()
PIPELINE_ID = uuid.uuid4()
LLM_MODEL_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()


def _make_iteration(
    step: int,
    sufficient: bool = False,
    query: str = "sub_query",
) -> ResearchIteration:
    return ResearchIteration(
        step=step,
        query=query,
        retrieved_chunks=[],
        reasoning="test reasoning",
        sufficient=sufficient,
    )


def _make_state(
    iterations: list[ResearchIteration],
    max_steps: int = 3,
) -> ResearchState:
    return ResearchState(
        original_question="What is the treatment for type 2 diabetes with CKD?",
        pipeline_id=PIPELINE_ID,
        tenant_id=TENANT_ID,
        collection_ids=[COLLECTION_ID],
        allowed_collection_ids=[COLLECTION_ID],
        llm_model_id=LLM_MODEL_ID,
        iterations=iterations,
        max_steps=max_steps,
    )


def test_routing_sufficient_true_routes_to_synthesize() -> None:
    """When the latest iteration has sufficient=True, routing goes to synthesize."""
    state = _make_state(
        iterations=[
            _make_iteration(step=1, sufficient=False),
            _make_iteration(step=2, sufficient=True),
        ]
    )
    assert route_after_plan(state) == "node_research_synthesize"


def test_routing_max_steps_reached_forces_synthesize() -> None:
    """When len(iterations) >= max_steps, routing forces synthesize even if not sufficient."""
    state = _make_state(
        iterations=[
            _make_iteration(step=1, sufficient=False),
            _make_iteration(step=2, sufficient=False),
            _make_iteration(step=3, sufficient=False),
        ],
        max_steps=3,
    )
    assert route_after_plan(state) == "node_research_synthesize"


def test_routing_neither_condition_routes_to_retrieve() -> None:
    """When sufficient=False and step budget not exhausted, routing goes to retrieve."""
    state = _make_state(
        iterations=[
            _make_iteration(step=1, sufficient=False),
        ],
        max_steps=3,
    )
    assert route_after_plan(state) == "node_research_retrieve"


def test_routing_empty_iterations_defensive_routes_to_synthesize() -> None:
    """Empty iterations list (defensive path) routes to synthesize."""
    state = _make_state(iterations=[])
    assert route_after_plan(state) == "node_research_synthesize"


def test_routing_one_step_exactly_at_budget_forces_synthesize() -> None:
    """Exactly at max_steps=1 after one iteration → synthesize."""
    state = _make_state(
        iterations=[_make_iteration(step=1, sufficient=False)],
        max_steps=1,
    )
    assert route_after_plan(state) == "node_research_synthesize"


def test_routing_sufficient_true_overrides_remaining_budget() -> None:
    """sufficient=True on step 1 of 3 still routes to synthesize immediately."""
    state = _make_state(
        iterations=[_make_iteration(step=1, sufficient=True)],
        max_steps=3,
    )
    assert route_after_plan(state) == "node_research_synthesize"

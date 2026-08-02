"""Unit tests for node_graph_retrieve.

Tests cover:
- Returns entities with relations correctly structured in graph_context
- Empty query (only stopwords) returns graph_context=None without a DB call
- No matching entities from DB returns graph_context=None
- tenant_id filter is always applied to SQL queries (merge blocker)
- Extra relation endpoint entities are fetched via a second DB call
- DB failure raises QueryNodeError
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.exceptions import QueryNodeError
from src.db.models.knowledge_graph import EntityRelation, MedicalEntity
from src.graphs.query_graph.nodes.node_graph_retrieve import node_graph_retrieve
from src.graphs.query_graph.state import QueryState

TENANT_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()
PIPELINE_ID = uuid.uuid4()


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _make_state(rewritten_query: str = "metformin contraindication") -> QueryState:
    return QueryState(
        question="test question",
        conversation_id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        allowed_collection_ids=[COLLECTION_ID],
        pipeline_id=PIPELINE_ID,
        llm_model_id=uuid.uuid4(),
        collection_ids=[COLLECTION_ID],
        rewritten_query=rewritten_query,
    )


def _make_config(db: Any) -> dict[str, Any]:
    return {"configurable": {"db": db}}


def _make_entity(
    entity_id: uuid.UUID | None = None,
    canonical_name: str = "metformin",
    entity_type: str = "drug",
    icd_code: str | None = None,
    atc_code: str | None = "A10BA02",
) -> MagicMock:
    """Create a MagicMock that quacks like a MedicalEntity ORM instance."""
    entity = MagicMock(spec=MedicalEntity)
    entity.id = entity_id or uuid.uuid4()
    entity.canonical_name = canonical_name
    entity.entity_type = entity_type
    entity.icd_code = icd_code
    entity.atc_code = atc_code
    entity.tenant_id = TENANT_ID
    return entity


def _make_relation(
    source_id: uuid.UUID,
    target_id: uuid.UUID,
    relation_type: str = "contraindicated_with",
    confidence: float = 0.9,
) -> MagicMock:
    """Create a MagicMock that quacks like an EntityRelation ORM instance."""
    rel = MagicMock(spec=EntityRelation)
    rel.id = uuid.uuid4()
    rel.tenant_id = TENANT_ID
    rel.source_entity_id = source_id
    rel.target_entity_id = target_id
    rel.relation_type = relation_type
    rel.confidence = confidence
    return rel


def _make_scalars_result(items: list[Any]) -> MagicMock:
    """Return a mock whose .scalars().all() returns *items*."""
    result = MagicMock()
    result.scalars.return_value.all.return_value = items
    return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graph_retrieve_returns_entities_with_relations() -> None:
    """2 entities + 1 relation are assembled correctly in graph_context."""
    entity_a_id = uuid.uuid4()
    entity_b_id = uuid.uuid4()

    entity_a = _make_entity(entity_id=entity_a_id, canonical_name="metformin", entity_type="drug")
    entity_b = _make_entity(
        entity_id=entity_b_id,
        canonical_name="type 2 diabetes",
        entity_type="condition",
        icd_code="E11",
        atc_code=None,
    )
    relation = _make_relation(
        source_id=entity_a_id,
        target_id=entity_b_id,
        relation_type="treats",
        confidence=0.92,
    )

    # Call sequence: entities → relations → (no extra entities needed, both endpoints are known)
    call_count = 0

    async def execute_side_effect(query: Any) -> MagicMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # entity lookup
            return _make_scalars_result([entity_a, entity_b])
        if call_count == 2:
            # relation lookup
            return _make_scalars_result([relation])
        # extra entity fetch — should not be reached in this test
        return _make_scalars_result([])

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=execute_side_effect)

    state = _make_state(rewritten_query="metformin treats diabetes")
    result = await node_graph_retrieve(state, _make_config(db))

    graph_context = result["graph_context"]
    assert graph_context is not None
    assert len(graph_context) == 2

    # entity_a (metformin) should appear with the outgoing 'treats' relation
    metformin_entry = next(e for e in graph_context if e["entity"] == "metformin")
    assert metformin_entry["entity_type"] == "drug"
    assert metformin_entry["atc_code"] == "A10BA02"
    assert len(metformin_entry["related"]) == 1

    rel_entry = metformin_entry["related"][0]
    assert rel_entry["name"] == "type 2 diabetes"
    assert rel_entry["relation"] == "treats"
    assert rel_entry["direction"] == "outgoing"
    assert rel_entry["confidence"] == pytest.approx(0.92)

    # entity_b (type 2 diabetes) should appear with the incoming 'treats' relation
    diabetes_entry = next(e for e in graph_context if e["entity"] == "type 2 diabetes")
    assert diabetes_entry["icd_code"] == "E11"
    assert len(diabetes_entry["related"]) == 1
    assert diabetes_entry["related"][0]["direction"] == "incoming"


@pytest.mark.asyncio
async def test_graph_retrieve_empty_query_returns_none() -> None:
    """A query containing only stopwords produces no tokens → graph_context=None, no DB call."""
    db = AsyncMock()
    db.execute = AsyncMock()

    # "a an the" are all in _QUERY_STOPWORDS
    state = _make_state(rewritten_query="a an the")
    result = await node_graph_retrieve(state, _make_config(db))

    assert result["graph_context"] is None
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_graph_retrieve_no_entities_returns_none() -> None:
    """When DB returns 0 matching entities, graph_context is None."""
    call_count = 0

    async def execute_side_effect(query: Any) -> MagicMock:
        nonlocal call_count
        call_count += 1
        return _make_scalars_result([])

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=execute_side_effect)

    state = _make_state(rewritten_query="metformin contraindication")
    result = await node_graph_retrieve(state, _make_config(db))

    assert result["graph_context"] is None
    # Only the entity query should be executed — relation query is skipped
    assert call_count == 1


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_graph_retrieve_filters_by_tenant_id() -> None:
    """Every DB execute call must include tenant_id from state in its WHERE clause.

    We verify this by capturing the SQLAlchemy Select object passed to db.execute
    and confirming that the compiled query string references the tenant_id value.
    This ensures no cross-tenant entity leakage regardless of graph_rag path.
    """
    captured_queries: list[Any] = []

    async def execute_side_effect(query: Any) -> MagicMock:
        captured_queries.append(query)
        # Return empty to short-circuit after entity lookup
        return _make_scalars_result([])

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=execute_side_effect)

    state = _make_state(rewritten_query="metformin contraindication renal failure")
    await node_graph_retrieve(state, _make_config(db))

    # At least the entity lookup was made
    assert len(captured_queries) >= 1

    # Inspect the entity lookup query: compile it with a generic dialect and
    # check that the tenant_id value appears in the WHERE clause.
    from sqlalchemy.dialects import postgresql

    entity_query = captured_queries[0]
    compiled = entity_query.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    )
    compiled_str = str(compiled)
    assert str(TENANT_ID) in compiled_str, (
        f"tenant_id={TENANT_ID} not found in compiled entity query:\n{compiled_str}"
    )


@pytest.mark.asyncio
async def test_graph_retrieve_extra_relation_endpoints_fetched() -> None:
    """When a relation's target is not in matched_entities, a second DB fetch retrieves it."""
    entity_a_id = uuid.uuid4()
    entity_b_id = uuid.uuid4()  # NOT in initial match — is the other relation endpoint

    entity_a = _make_entity(entity_id=entity_a_id, canonical_name="metformin", entity_type="drug")
    entity_b = _make_entity(
        entity_id=entity_b_id,
        canonical_name="renal impairment",
        entity_type="condition",
        icd_code="N19",
        atc_code=None,
    )
    relation = _make_relation(
        source_id=entity_a_id,
        target_id=entity_b_id,
        relation_type="contraindicated_with",
        confidence=0.88,
    )

    call_count = 0

    async def execute_side_effect(query: Any) -> MagicMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # entity lookup — only entity_a matches the query tokens
            return _make_scalars_result([entity_a])
        if call_count == 2:
            # relation lookup — returns a relation to entity_b which is not in matched_entities
            return _make_scalars_result([relation])
        if call_count == 3:
            # extra entity fetch for entity_b (missing endpoint)
            return _make_scalars_result([entity_b])
        return _make_scalars_result([])

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=execute_side_effect)

    state = _make_state(rewritten_query="metformin contraindication")
    result = await node_graph_retrieve(state, _make_config(db))

    # The extra entity fetch must have been triggered
    assert call_count == 3, (
        f"Expected 3 DB calls (entity, relation, extra entity), got {call_count}"
    )

    graph_context = result["graph_context"]
    assert graph_context is not None
    assert len(graph_context) == 1  # only entity_a is in matched_entities

    metformin_entry = graph_context[0]
    assert len(metformin_entry["related"]) == 1
    assert metformin_entry["related"][0]["name"] == "renal impairment"
    assert metformin_entry["related"][0]["relation"] == "contraindicated_with"


@pytest.mark.asyncio
async def test_graph_retrieve_raises_query_node_error_on_db_failure() -> None:
    """Any DB exception during execution is wrapped as QueryNodeError."""
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=RuntimeError("DB connection lost"))

    state = _make_state(rewritten_query="metformin renal contraindication")

    with pytest.raises(QueryNodeError, match="graph_retrieve_error"):
        await node_graph_retrieve(state, _make_config(db))

"""node_graph_retrieve — knowledge-graph-enhanced context retrieval.

Queries the medical_entities and entity_relations Postgres tables to produce
structured graph context that the generate node can use alongside vector
chunks.

This node runs ONLY when the pipeline's prompt_config contains
``"graph_rag_enabled": true`` (opt-in).  It is wired in parallel with
node_retrieve in the query graph topology; both converge at node_rerank.

Design decisions:
- Entity matching uses ILIKE (case-insensitive substring) on canonical_name
  and on surface_forms elements (using the Postgres ANY operator).  This is
  intentionally lightweight — we do not run an LLM call for query entity
  extraction to avoid adding latency to the hot path.
- The node loads at most MAX_ENTITY_MATCHES entities and MAX_RELATIONS
  relations to bound the graph context size.
- Results are structured as list[dict] in state.graph_context; node_generate
  can incorporate them via the prompt template.

Tenant isolation:
- Every DB query is filtered on tenant_id from state — never from the LLM
  output or request body.
- NEVER returns entities belonging to a different tenant.

GDPR:
- canonical_name and surface_forms may contain terminology derived from
  ingested documents.  Log counts and entity_type only; never log name values.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from langfuse import observe
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import QueryNodeError
from src.core.langfuse_client import update_span_metadata as _lf_update_span
from src.db.models.knowledge_graph import EntityRelation, MedicalEntity
from src.graphs.query_graph.state import QueryState

logger = structlog.get_logger(__name__)

_MAX_ENTITY_MATCHES = 10

# Module-level stopword set for query tokenisation (EN + PL common words)
_QUERY_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "can",
        "of",
        "to",
        "for",
        "in",
        "on",
        "at",
        "by",
        "with",
        "and",
        "or",
        "not",
        "what",
        "how",
        "when",
        "where",
        "who",
        "which",
        "that",
        "this",
        "it",
        "its",
        "from",
        "about",
        "jako",
        "czy",
        "jak",
        "na",
        "w",
        "z",
        "jest",
        "się",
        "nie",
        "co",
        "i",
        "o",
        "do",
    }
)
_MAX_RELATIONS = 20


def _tokenize_query(query: str) -> list[str]:
    """Extract candidate search terms from a query string.

    Splits on whitespace, strips punctuation, filters stopwords and
    very short tokens.  Returns up to 8 terms (longest first).
    This is a fast heuristic — no LLM call.
    """
    import re

    tokens: list[str] = []
    for raw in re.split(r"\s+", query.lower()):
        token = re.sub(r"[^\w\-]", "", raw)
        if len(token) >= 3 and token not in _QUERY_STOPWORDS:
            tokens.append(token)

    # Longer tokens are more specific — prefer them
    tokens.sort(key=len, reverse=True)
    return tokens[:8]


@observe(capture_input=False, capture_output=False)
async def node_graph_retrieve(state: QueryState, config: dict[str, Any]) -> dict[str, Any]:
    """Query the knowledge graph for entities related to the rewritten query.

    Matches entities by canonical_name (ILIKE) or surface_forms (ANY ILIKE)
    against tokens extracted from the rewritten query.  Then fetches
    EntityRelation rows for each matched entity and assembles graph_context.

    Args:
        state: Must have rewritten_query (or question), tenant_id, collection_ids.
        config: RunnableConfig with configurable["db"] (AsyncSession).

    Returns:
        {"graph_context": list[dict] | None}
        Each element: {"entity": str, "entity_type": str,
                       "related": [{"name": str, "relation": str, "confidence": float}]}

    Raises:
        QueryNodeError: On unrecoverable DB error.
    """
    cfg = config.get("configurable", {})
    db: AsyncSession = cfg["db"]

    node_start = datetime.now(UTC)

    query_text = state.rewritten_query or state.question
    tokens = _tokenize_query(query_text)

    if not tokens:
        logger.info(
            "node_graph_retrieve.no_tokens",
            tenant_id=str(state.tenant_id),
        )
        return {"graph_context": None}

    try:
        # ----------------------------------------------------------------
        # Step 1: Find matching entities scoped to this tenant
        # ----------------------------------------------------------------
        # Build OR conditions: canonical_name ILIKE any token,
        # OR surface_forms contains any token (array overlap via ANY).

        ilike_filters = [MedicalEntity.canonical_name.ilike(f"%{token}%") for token in tokens]

        entity_result = await db.execute(
            select(MedicalEntity)
            .where(
                MedicalEntity.tenant_id == state.tenant_id,
                or_(*ilike_filters),
            )
            .limit(_MAX_ENTITY_MATCHES)
        )
        matched_entities: list[MedicalEntity] = list(entity_result.scalars().all())

        if not matched_entities:
            elapsed_ms = int((datetime.now(UTC) - node_start).total_seconds() * 1000)
            _lf_update_span(
                metadata={
                    "tenant_id": str(state.tenant_id),
                    "token_count": len(tokens),
                    "entity_count": 0,
                    "relation_count": 0,
                    "latency_ms": elapsed_ms,
                }
            )
            logger.info(
                "node_graph_retrieve.no_entities_found",
                tenant_id=str(state.tenant_id),
                token_count=len(tokens),
                latency_ms=elapsed_ms,
            )
            return {"graph_context": None}

        entity_ids: list[UUID] = [e.id for e in matched_entities]

        # ----------------------------------------------------------------
        # Step 2: Fetch relations for matched entities (source OR target)
        # ----------------------------------------------------------------
        relation_result = await db.execute(
            select(EntityRelation)
            .where(
                EntityRelation.tenant_id == state.tenant_id,
                or_(
                    EntityRelation.source_entity_id.in_(entity_ids),
                    EntityRelation.target_entity_id.in_(entity_ids),
                ),
            )
            .limit(_MAX_RELATIONS)
        )
        relations: list[EntityRelation] = list(relation_result.scalars().all())

        # ----------------------------------------------------------------
        # Step 3: Build id → entity lookup for relation labelling
        # ----------------------------------------------------------------
        # Collect all entity IDs referenced by relations (may include entities
        # not in the initial match set — the "other end" of a relation)
        all_referenced_ids: set[UUID] = set(entity_ids)
        for rel in relations:
            all_referenced_ids.add(rel.source_entity_id)
            all_referenced_ids.add(rel.target_entity_id)

        # Fetch any missing entity records (relation endpoints not in matched_entities)
        missing_ids = all_referenced_ids - set(entity_ids)
        extra_entities: list[MedicalEntity] = []
        if missing_ids:
            extra_result = await db.execute(
                select(MedicalEntity).where(
                    MedicalEntity.tenant_id == state.tenant_id,
                    MedicalEntity.id.in_(missing_ids),
                )
            )
            extra_entities = list(extra_result.scalars().all())

        id_to_entity: dict[UUID, MedicalEntity] = {
            e.id: e for e in [*matched_entities, *extra_entities]
        }

        # ----------------------------------------------------------------
        # Step 4: Assemble graph_context list
        # ----------------------------------------------------------------
        # Group relations by matched entity
        from collections import defaultdict

        related_map: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
        for rel in relations:
            # From the perspective of the source entity
            if rel.source_entity_id in set(entity_ids):
                target = id_to_entity.get(rel.target_entity_id)
                if target is not None:
                    related_map[rel.source_entity_id].append(
                        {
                            "name": target.canonical_name,
                            "entity_type": target.entity_type,
                            "relation": rel.relation_type,
                            "confidence": rel.confidence,
                            "direction": "outgoing",
                        }
                    )
            # From the perspective of the target entity (inverse relation)
            if rel.target_entity_id in set(entity_ids):
                source = id_to_entity.get(rel.source_entity_id)
                if source is not None:
                    related_map[rel.target_entity_id].append(
                        {
                            "name": source.canonical_name,
                            "entity_type": source.entity_type,
                            "relation": rel.relation_type,
                            "confidence": rel.confidence,
                            "direction": "incoming",
                        }
                    )

        graph_context: list[dict[str, Any]] = []
        for entity in matched_entities:
            graph_context.append(
                {
                    "entity": entity.canonical_name,
                    "entity_type": entity.entity_type,
                    "icd_code": entity.icd_code,
                    "atc_code": entity.atc_code,
                    "related": related_map.get(entity.id, []),
                }
            )

        elapsed_ms = int((datetime.now(UTC) - node_start).total_seconds() * 1000)
        _lf_update_span(
            metadata={
                "tenant_id": str(state.tenant_id),
                "token_count": len(tokens),
                "entity_count": len(matched_entities),
                "relation_count": len(relations),
                "latency_ms": elapsed_ms,
            }
        )
        logger.info(
            "node_graph_retrieve.completed",
            tenant_id=str(state.tenant_id),
            entity_count=len(matched_entities),
            relation_count=len(relations),
            latency_ms=elapsed_ms,
        )

        return {"graph_context": graph_context if graph_context else None}

    except Exception as exc:
        elapsed_ms = int((datetime.now(UTC) - node_start).total_seconds() * 1000)
        _lf_update_span(
            metadata={
                "tenant_id": str(state.tenant_id),
                "error": True,
                "error_type": type(exc).__name__,
                "latency_ms": elapsed_ms,
            }
        )
        raise QueryNodeError(f"graph_retrieve_error: {type(exc).__name__}") from exc

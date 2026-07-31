"""node_extract_entities — LLM-based medical entity and relation extraction.

Runs after node_chunk.  Processes chunks in batches of 5 to stay within LLM
context limits.  For each batch:
  1. First LLM call: entity extraction  → {"entities": [...]}
  2. Second LLM call: relation extraction → {"relations": [...]}

The node is fail-safe: any extraction failure logs a warning and continues;
entity extraction is enrichment, not required for the pipeline to succeed.

Graph RAG opt-in:
  This node is only inserted into the graph when the collection's
  chunk_config contains ``"graph_rag_enabled": true``.  When the collection
  does not opt in, the node is not registered in the graph topology at all
  (see graph.py).

Security / GDPR:
- Chunk text is passed to the LLM as untrusted data (prompt injection
  delimiters applied in the prompt file).
- Chunk content MUST NOT appear in logs; only counts and IDs are logged.
- surface_forms and evidence sentences contain text derived from documents —
  treated as GDPR-sensitive; use doc_id + chunk_index references in logs.
- Langfuse: capture_input=False, capture_output=False.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from langfuse import observe
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.langfuse_client import update_span_metadata as _lf_update_span
from src.graphs.ingest_graph.helpers import get_collection, get_model, update_step, utcnow
from src.graphs.ingest_graph.state import ChunkData, IngestState

logger = structlog.get_logger(__name__)

_PROMPT_PATH = (
    Path(__file__).parent.parent.parent.parent / "graphs" / "prompts" / "entity_extraction_v1.md"
)
_BATCH_SIZE = 5

_VALID_ENTITY_TYPES = frozenset({"drug", "condition", "procedure", "icd_code", "anatomy"})
_VALID_RELATION_TYPES = frozenset(
    {"treats", "contraindicated_with", "interacts_with", "causes", "diagnosed_by"}
)


def _load_prompt() -> str:
    """Load the entity extraction prompt from disk (not cached — allows hot-reload)."""
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _sha256_hex(text: str) -> str:
    """Return the SHA-256 hex digest of *text*."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _elapsed_ms(start: datetime) -> int:
    return int((datetime.now(UTC) - start).total_seconds() * 1000)


def _build_chunk_block(chunks: list[ChunkData]) -> str:
    """Format chunks with XML delimiters as untrusted-input context for the LLM."""
    parts: list[str] = []
    for chunk in chunks:
        parts.append(
            f'<chunk index="{chunk.chunk_index}" point_id="{chunk.point_id}">\n'
            f"{chunk.text}\n"
            f"</chunk>"
        )
    return "\n\n".join(parts)


def _normalise_entity(raw: dict[str, Any], chunk_ids: list[str]) -> dict[str, Any] | None:
    """Validate and normalise a raw entity dict returned by the LLM.

    Returns None if the entity is missing required fields or has an invalid type.
    """
    entity_type = raw.get("type", "")
    if entity_type not in _VALID_ENTITY_TYPES:
        return None
    name = raw.get("name", "").strip().lower()
    if not name:
        return None
    confidence = float(raw.get("confidence", 0.0))
    surface_forms: list[str] = [str(s) for s in raw.get("surface_forms", []) if s]
    if not surface_forms:
        surface_forms = [name]

    return {
        "type": entity_type,
        "name": name,
        "icd_code": raw.get("icd_code") or None,
        "atc_code": raw.get("atc_code") or None,
        "confidence": max(0.0, min(1.0, confidence)),
        "surface_forms": surface_forms,
        "chunk_ids": chunk_ids,
    }


def _normalise_relation(
    raw: dict[str, Any],
    entity_name_map: dict[str, Any],
) -> dict[str, Any] | None:
    """Validate and normalise a raw relation dict returned by the LLM.

    Filters out relations whose source or target are not among the extracted
    entities for this batch.
    """
    relation_type = raw.get("relation", "")
    if relation_type not in _VALID_RELATION_TYPES:
        return None

    source_name = raw.get("source", "").strip().lower()
    target_name = raw.get("target", "").strip().lower()

    if not source_name or not target_name:
        return None

    # Both endpoints must resolve to an extracted entity in this batch
    if source_name not in entity_name_map or target_name not in entity_name_map:
        return None

    evidence_sentence: str = raw.get("evidence_sentence", "")
    evidence_hash = _sha256_hex(evidence_sentence) if evidence_sentence else _sha256_hex("")

    return {
        "source_name": source_name,
        "target_name": target_name,
        "relation_type": relation_type,
        "confidence": max(0.0, min(1.0, float(raw.get("confidence", 0.0)))),
        # SHA-256 hash only — the sentence itself is NOT stored (GDPR)
        "evidence_text_hash": evidence_hash,
    }


async def _extract_batch(
    batch: list[ChunkData],
    llm: Any,
    model_id: str,
    endpoint_url: str,
    prompt_template: str,
    document_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run entity + relation extraction for a single chunk batch.

    Returns:
        (entities, relations) — both lists may be empty on failure.
    """
    chunk_block = _build_chunk_block(batch)
    chunk_ids = [str(c.point_id) for c in batch]

    # --- Entity extraction call ---
    entity_user_msg = (
        "Extract medical entities from the following document chunks. "
        "Return JSON with the format described in the system prompt.\n\n"
        f"{chunk_block}\n\n"
        "Return only the JSON object with key 'entities'."
    )
    try:
        entity_response = await llm.chat_completion(
            model=model_id,
            messages=[
                {"role": "system", "content": prompt_template},
                {"role": "user", "content": entity_user_msg},
            ],
            base_url=endpoint_url,
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        raw_entity_content = entity_response.choices[0].message.content or "{}"
        entity_data = json.loads(raw_entity_content)
        raw_entities: list[dict[str, Any]] = entity_data.get("entities", [])
    except Exception as exc:
        logger.warning(
            "node_extract_entities.entity_call_failed",
            document_id=document_id,
            batch_size=len(batch),
            error_type=type(exc).__name__,
        )
        return [], []

    # Normalise and filter entities
    entities: list[dict[str, Any]] = []
    for raw_e in raw_entities:
        normalised = _normalise_entity(raw_e, chunk_ids)
        if normalised is not None:
            entities.append(normalised)

    if not entities:
        return [], []

    # Build name → entity lookup for relation validation
    entity_name_map: dict[str, dict[str, Any]] = {e["name"]: e for e in entities}

    # --- Relation extraction call ---
    entity_list = "\n".join(f"- {e['name']}" for e in entities)
    relation_user_msg = (
        "Extract medical relations between the following entities found in the document chunks.\n\n"
        f"<entities>\n{entity_list}\n</entities>\n\n"
        f"{chunk_block}\n\n"
        "Return only the JSON object with key 'relations'."
    )
    relations: list[dict[str, Any]] = []
    try:
        relation_response = await llm.chat_completion(
            model=model_id,
            messages=[
                {"role": "system", "content": prompt_template},
                {"role": "user", "content": relation_user_msg},
            ],
            base_url=endpoint_url,
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        raw_relation_content = relation_response.choices[0].message.content or "{}"
        relation_data = json.loads(raw_relation_content)
        raw_relations: list[dict[str, Any]] = relation_data.get("relations", [])
        for raw_r in raw_relations:
            normalised_r = _normalise_relation(raw_r, entity_name_map)
            if normalised_r is not None:
                relations.append(normalised_r)
    except Exception as exc:
        logger.warning(
            "node_extract_entities.relation_call_failed",
            document_id=document_id,
            batch_size=len(batch),
            error_type=type(exc).__name__,
        )
        # Relation failure is non-fatal; return what we have from entity extraction

    return entities, relations


@observe(name="node_extract_entities", capture_input=False, capture_output=False)
async def node_extract_entities(state: IngestState, config: dict[str, Any]) -> dict[str, Any]:
    """Extract medical entities and relations from document chunks using an LLM.

    Processes state.chunks in batches of 5.  Results are accumulated into
    state.extracted_entities — a list of dicts with two top-level keys:
      "entities": [...] and "relations": [...].

    This node is fail-safe: any failure in extraction logs a warning and
    returns {"extracted_entities": None} so the pipeline continues.

    Args:
        state: Must have chunks (set by node_chunk) and collection_id, tenant_id.
        config: RunnableConfig with configurable["db"] and configurable["llm"].

    Returns:
        {"extracted_entities": list[dict] | None}
    """
    cfg = config.get("configurable", {})
    session: AsyncSession = cfg["db"]
    llm = cfg["llm"]
    step_start = utcnow()

    chunks = state.chunks or []
    if not chunks:
        logger.warning(
            "node_extract_entities.no_chunks",
            document_id=str(state.document_id),
        )
        return {"extracted_entities": None}

    try:
        collection = await get_collection(session, state.collection_id)
        chunk_config: dict[str, Any] = collection.chunk_config or {}

        # Guard: only run if graph_rag_enabled (belt-and-suspenders, graph also gates this)
        if not chunk_config.get("graph_rag_enabled", False):
            logger.info(
                "node_extract_entities.disabled_by_collection",
                document_id=str(state.document_id),
                collection_id=str(state.collection_id),
            )
            return {"extracted_entities": None}

        model_record = await get_model(session, collection.embedding_model_id)
        prompt_template = _load_prompt()

        all_entities: list[dict[str, Any]] = []
        all_relations: list[dict[str, Any]] = []

        # Process chunks in batches to respect LLM context limits
        for batch_start in range(0, len(chunks), _BATCH_SIZE):
            batch = chunks[batch_start : batch_start + _BATCH_SIZE]
            try:
                batch_entities, batch_relations = await _extract_batch(
                    batch=batch,
                    llm=llm,
                    model_id=model_record.model_id,
                    endpoint_url=model_record.endpoint_url,
                    prompt_template=prompt_template,
                    document_id=str(state.document_id),
                )
                all_entities.extend(batch_entities)
                all_relations.extend(batch_relations)
            except Exception as exc:
                # Batch-level failure is non-fatal
                logger.warning(
                    "node_extract_entities.batch_failed",
                    document_id=str(state.document_id),
                    batch_start=batch_start,
                    error_type=type(exc).__name__,
                )

        elapsed = _elapsed_ms(step_start)

        _lf_update_span(
            metadata={
                "document_id": str(state.document_id),
                "tenant_id": str(state.tenant_id),
                "chunk_count": len(chunks),
                "entity_count": len(all_entities),
                "relation_count": len(all_relations),
                "latency_ms": elapsed,
            }
        )
        logger.info(
            "node_extract_entities.completed",
            document_id=str(state.document_id),
            chunk_count=len(chunks),
            entity_count=len(all_entities),
            relation_count=len(all_relations),
            latency_ms=elapsed,
        )

        await update_step(
            session,
            state.job_id,
            stage="extract_entities",
            status="completed",
            started_at=step_start,
            meta={
                "chunk_count": len(chunks),
                "entity_count": len(all_entities),
                "relation_count": len(all_relations),
                "latency_ms": elapsed,
            },
        )

        extracted: list[dict[str, Any]] = []
        if all_entities:
            extracted = [
                {
                    "entities": all_entities,
                    "relations": all_relations,
                }
            ]

        return {"extracted_entities": extracted if extracted else None}

    except Exception as exc:
        # Node-level failure is non-fatal per spec — log warning and continue
        elapsed = _elapsed_ms(step_start)
        logger.warning(
            "node_extract_entities.failed_nonfatal",
            document_id=str(state.document_id),
            error_type=type(exc).__name__,
            latency_ms=elapsed,
        )
        _lf_update_span(
            metadata={
                "document_id": str(state.document_id),
                "tenant_id": str(state.tenant_id),
                "error": True,
                "error_type": type(exc).__name__,
                "latency_ms": elapsed,
            }
        )
        # Best-effort step update (may fail if DB is also unavailable)
        import contextlib

        async with contextlib.AsyncExitStack():
            with contextlib.suppress(Exception):
                await update_step(
                    session,
                    state.job_id,
                    stage="extract_entities",
                    status="warning",
                    started_at=step_start,
                    error=f"extract_entities_nonfatal: {type(exc).__name__}",
                )
        # Return None — pipeline continues without entity data
        return {"extracted_entities": None}

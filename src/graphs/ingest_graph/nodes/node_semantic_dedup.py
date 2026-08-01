"""node_semantic_dedup — embedding-based near-duplicate detection.

Runs AFTER node_embed (chunk embeddings available) and BEFORE node_upsert.
Mean-pools chunk embeddings to produce a document-level embedding, then
computes cosine similarity against existing documents in the same collection.

Activated only when collection.chunk_config["semantic_dedup_enabled"] = true.
If disabled, this node is not inserted into the graph (graph.py controls topology).

Config keys read from collection.chunk_config (all optional):
  "semantic_dedup_enabled": bool   — opt-in flag (default False — must be explicit)
  "dedup_threshold": float         — similarity threshold to flag as near-duplicate (default 0.95)
  "reject_near_duplicates": bool   — if True, reject immediately; else route to needs_review (default False)

Similarity algorithm: cosine similarity computed in Python over mean-pooled embeddings.
Scales linearly with collection size; suitable for up to ~5 000 documents per collection.
For larger collections, migrate to pgvector + IVFFlat index.

Security:
- Embedding stored as JSONB in documents.dedup_embedding — treated as document content.
- dedup_embedding is never logged (GDPR).
- Only UUID and float score are written to structlog / Langfuse (safe identifiers).
- Similarity query filtered by tenant_id + collection_id — no cross-tenant leakage.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from langfuse import observe
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import IngestNodeError
from src.core.langfuse_client import update_span_metadata as _lf_update_span
from src.db.models.document import Document
from src.graphs.ingest_graph.helpers import get_collection, update_step, utcnow
from src.graphs.ingest_graph.state import IngestState

logger = structlog.get_logger(__name__)

_DEFAULT_DEDUP_THRESHOLD = 0.95


def _mean_pool(embeddings: list[list[float]]) -> list[float]:
    """Compute the element-wise mean of a list of embedding vectors."""
    if not embeddings:
        raise ValueError("Cannot mean-pool an empty list of embeddings")
    dim = len(embeddings[0])
    result = [0.0] * dim
    for vec in embeddings:
        for i, v in enumerate(vec):
            result[i] += v
    n = len(embeddings)
    return [x / n for x in result]


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Return cosine similarity between two equal-length vectors."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


@observe(name="node_semantic_dedup", capture_input=False, capture_output=False)
async def node_semantic_dedup(state: IngestState, config: dict[str, Any]) -> dict[str, Any]:
    """Detect near-duplicate documents via embedding cosine similarity.

    Args:
        state: Must have embeddings populated by node_embed.
        config: RunnableConfig with configurable["db"].

    Returns:
        {} if unique (with dedup_embedding stored in DB).
        {"status": "needs_review" | "rejected", "halt": True} if near-duplicate found.

    Raises:
        IngestNodeError: On DB failure or embedding shape mismatch.
    """
    cfg = config.get("configurable", {})
    session: AsyncSession = cfg["db"]
    step_start = utcnow()

    try:
        embeddings = state.embeddings or []
        if not embeddings:
            raise IngestNodeError("no embeddings — node_embed must run before node_semantic_dedup")

        collection = await get_collection(session, state.collection_id)
        chunk_config: dict[str, Any] = collection.chunk_config or {}
        threshold: float = float(chunk_config.get("dedup_threshold", _DEFAULT_DEDUP_THRESHOLD))
        reject_on_dup: bool = bool(chunk_config.get("reject_near_duplicates", False))

        # Compute document-level embedding by mean-pooling chunk embeddings
        doc_embedding: list[float] = _mean_pool(embeddings)

        # Persist the document embedding before similarity search
        # (allows this document to be found by future dedup checks)
        await session.execute(
            update(Document)
            .where(
                Document.id == state.document_id,
                Document.tenant_id == state.tenant_id,
            )
            .values(dedup_embedding=doc_embedding)
        )
        await session.flush()

        # Query existing documents in the same collection that have an embedding
        # Exclude this document and already-rejected/failed ones
        existing_q = select(Document.id, Document.dedup_embedding).where(
            Document.tenant_id == state.tenant_id,
            Document.collection_id == state.collection_id,
            Document.id != state.document_id,
            Document.dedup_embedding.isnot(None),
            Document.status.notin_(["rejected", "failed", "deleted"]),
        )
        existing_rows = (await session.execute(existing_q)).all()

        # Find the most similar document
        best_similarity: float = 0.0
        best_doc_id: UUID | None = None
        for row_id, row_embedding in existing_rows:
            if row_embedding is None:
                continue
            try:
                sim = _cosine_similarity(doc_embedding, list(row_embedding))
            except (ValueError, TypeError):
                continue
            if sim > best_similarity:
                best_similarity = sim
                best_doc_id = row_id

        # Persist dedup audit metadata
        update_vals: dict[str, Any] = {"dedup_similarity": best_similarity}
        if best_doc_id is not None:
            update_vals["dedup_similar_doc_id"] = best_doc_id

        await session.execute(
            update(Document)
            .where(
                Document.id == state.document_id,
                Document.tenant_id == state.tenant_id,
            )
            .values(**update_vals)
        )

        meta = {
            "similarity": round(best_similarity, 4),
            "threshold": threshold,
            "similar_doc_id": str(best_doc_id) if best_doc_id else None,
            "result": "unique",
        }
        lf_meta = {
            "document_id": str(state.document_id),
            "tenant_id": str(state.tenant_id),
            "similarity": round(best_similarity, 4),
            "threshold": threshold,
        }

        if best_similarity >= threshold and best_doc_id is not None:
            # Near-duplicate detected
            meta["result"] = "near_duplicate"
            new_status = "rejected" if reject_on_dup else "needs_review"
            await session.execute(
                update(Document)
                .where(
                    Document.id == state.document_id,
                    Document.tenant_id == state.tenant_id,
                )
                .values(status=new_status)
            )
            await session.commit()

            await update_step(
                session,
                state.job_id,
                stage="semantic_dedup",
                status="completed",
                started_at=step_start,
                meta=meta,
            )
            _lf_update_span(metadata={**lf_meta, "result": "near_duplicate", "routed_to": new_status})
            logger.info(
                "node_semantic_dedup.near_duplicate",
                document_id=str(state.document_id),
                similar_doc_id=str(best_doc_id),
                similarity=round(best_similarity, 4),
                new_status=new_status,
            )
            return {"status": new_status, "halt": True}

        # Unique document — continue pipeline
        await session.commit()
        await update_step(
            session,
            state.job_id,
            stage="semantic_dedup",
            status="completed",
            started_at=step_start,
            meta=meta,
        )
        _lf_update_span(metadata={**lf_meta, "result": "unique"})
        logger.info(
            "node_semantic_dedup.unique",
            document_id=str(state.document_id),
            best_similarity=round(best_similarity, 4),
        )
        return {}

    except IngestNodeError:
        raise
    except Exception as exc:
        error_msg = f"semantic_dedup_error: {type(exc).__name__}"
        _lf_update_span(
            metadata={
                "document_id": str(state.document_id),
                "tenant_id": str(state.tenant_id),
                "error": True,
                "error_type": type(exc).__name__,
            }
        )
        await update_step(
            session,
            state.job_id,
            stage="semantic_dedup",
            status="failed",
            started_at=step_start,
            error=error_msg,
        )
        raise IngestNodeError(error_msg) from exc


def _elapsed_ms(start: datetime) -> int:
    return int((datetime.now(UTC) - start).total_seconds() * 1000)

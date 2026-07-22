"""node_embed — generate vector embeddings for all chunks.

Calls the OpenAI-compatible embedding API in batches of 32.
Model endpoint resolved from collections.embedding_model_id → models_registry.

Security:
- Chunk text is sent to the embedding model (must use internal network endpoint).
- Never log chunk content — only batch counts and latency.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import IngestNodeError
from src.graphs.ingest_graph.helpers import get_collection, get_model, update_step, utcnow
from src.graphs.ingest_graph.state import IngestState

logger = structlog.get_logger(__name__)

_BATCH_SIZE = 32


async def node_embed(state: IngestState, config: dict[str, Any]) -> dict[str, Any]:
    """Generate embeddings for all chunks using the collection's embedding model.

    Args:
        state: Must have chunks set by node_chunk.
        config: RunnableConfig with configurable["db"] and configurable["llm"].

    Returns:
        {"embeddings": list[list[float]]}

    Raises:
        IngestNodeError: On API failure or dimension mismatch.
    """
    cfg = config.get("configurable", {})
    session: AsyncSession = cfg["db"]
    llm = cfg["llm"]
    step_start = utcnow()

    try:
        chunks = state.chunks or []
        if not chunks:
            raise IngestNodeError("no chunks to embed — node_chunk must run first")

        collection = await get_collection(session, state.collection_id)
        model_record = await get_model(session, collection.embedding_model_id)

        texts = [c.text for c in chunks]
        all_embeddings: list[list[float]] = []
        batch_count = 0

        for i in range(0, len(texts), _BATCH_SIZE):
            batch = texts[i : i + _BATCH_SIZE]
            try:
                response = await llm.embeddings(
                    model=model_record.model_id,
                    input=batch,
                    base_url=model_record.endpoint_url,
                )
                all_embeddings.extend([item.embedding for item in response.data])
                batch_count += 1
            except Exception as exc:
                raise IngestNodeError(
                    f"embed_api_error on batch {batch_count}: {type(exc).__name__}"
                ) from exc

        await update_step(
            session,
            state.job_id,
            stage="embed",
            status="completed",
            started_at=step_start,
            meta={
                "model_id": str(model_record.id),
                "batch_count": batch_count,
                "total_vectors": len(all_embeddings),
                "latency_ms": _elapsed_ms(step_start),
            },
        )
        logger.info(
            "node_embed_completed",
            document_id=str(state.document_id),
            total_vectors=len(all_embeddings),
            batch_count=batch_count,
        )
        return {"embeddings": all_embeddings}

    except IngestNodeError as exc:
        await update_step(
            session,
            state.job_id,
            stage="embed",
            status="failed",
            started_at=step_start,
            error=str(exc),
        )
        raise
    except Exception as exc:
        error_msg = f"embed_error: {type(exc).__name__}"
        await update_step(
            session,
            state.job_id,
            stage="embed",
            status="failed",
            started_at=step_start,
            error=error_msg,
        )
        raise IngestNodeError(error_msg) from exc


def _elapsed_ms(start: datetime) -> int:
    return int((datetime.now(UTC) - start).total_seconds() * 1000)

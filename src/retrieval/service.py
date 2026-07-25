"""RetrievalService — the ONLY module with Qdrant access.

Architecture rule (ADR-1): AsyncQdrantClient is instantiated exclusively here.
Any import of qdrant_client outside src/retrieval/ fails CI.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from qdrant_client import AsyncQdrantClient
from qdrant_client.http.models import ScoredPoint
from qdrant_client.models import (
    Distance,
    FilterSelector,
    PayloadSchemaType,
    PointStruct,
    VectorParams,
)
from tenacity import (
    retry,
    retry_if_not_exception_type,
    stop_after_attempt,
    wait_combine,
    wait_exponential,
    wait_fixed,
    wait_random,
)

from src.retrieval.exceptions import EmptyCollectionListError, QdrantUnavailableError
from src.retrieval.filters import (
    build_document_delete_filter,
    build_mandatory_filter,
    build_tenant_delete_filter,
    merge_filters,
)
from src.retrieval.schemas import QdrantPoint, RetrievalResult, TenantContext

logger = structlog.get_logger(__name__)


def _validate_point_payload(ctx: TenantContext, point: QdrantPoint) -> None:
    """Verify point payload tenant_id matches context. Raises ValueError on mismatch."""
    payload_tenant = point.payload.get("tenant_id")
    if payload_tenant != str(ctx.tenant_id):
        raise ValueError(
            f"Point {point.id} payload tenant_id '{payload_tenant}' "
            f"does not match ctx.tenant_id '{ctx.tenant_id}'"
        )


class RetrievalService:
    """Single access point to Qdrant.

    Enforces tenant isolation and collection RBAC on every call.
    """

    def __init__(self, client: AsyncQdrantClient) -> None:
        self._client = client

    async def search(
        self,
        ctx: TenantContext,
        qdrant_collection: str,
        query_vector: list[float],
        top_k: int = 8,
        score_threshold: float = 0.0,
        additional_filter: object | None = None,
    ) -> list[RetrievalResult]:
        """Search for semantically similar chunks within tenant+collections scope.

        Args:
            ctx: Tenant context from verified JWT. Must have at least one allowed_collection_id.
            qdrant_collection: Physical Qdrant collection name (e.g., "emb_bge_m3").
            query_vector: Embedding vector (same model used to index).
            top_k: Maximum results. Default 8.
            score_threshold: Minimum similarity score. Default 0.0 (no cutoff).
            additional_filter: Optional Qdrant Filter merged with mandatory filter.

        Returns:
            List of RetrievalResult ordered by score descending.

        Raises:
            EmptyCollectionListError: If ctx.allowed_collection_ids is empty.
            QdrantUnavailableError: If Qdrant unreachable after retries.
        """
        from qdrant_client.models import Filter as QdrantFilter

        mandatory = build_mandatory_filter(
            tenant_id=str(ctx.tenant_id),
            collection_ids=[str(c) for c in ctx.allowed_collection_ids],
        )
        combined = merge_filters(
            mandatory,
            additional_filter if isinstance(additional_filter, QdrantFilter) else None,
        )

        logger.debug(
            "retrieval.search",
            tenant_id=str(ctx.tenant_id),
            collection_count=len(ctx.allowed_collection_ids),
            top_k=top_k,
            score_threshold=score_threshold,
        )

        hits: list[ScoredPoint]
        try:
            hits = await self._search_with_retry(
                qdrant_collection, query_vector, combined, top_k, score_threshold
            )
        except EmptyCollectionListError:
            raise
        except Exception as exc:
            raise QdrantUnavailableError(str(exc)) from exc

        results: list[RetrievalResult] = []
        for hit in hits:
            payload: dict[str, Any] = hit.payload or {}
            results.append(
                RetrievalResult(
                    point_id=UUID(str(hit.id)),
                    document_id=UUID(str(payload["document_id"])),
                    chunk_id=None,
                    score=hit.score,
                    payload=payload,
                    page_number=payload.get("page"),
                    highlight_text=payload.get("text"),
                    collection_id=UUID(str(payload["collection_id"]))
                    if payload.get("collection_id")
                    else None,
                )
            )
        return results

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_combine(wait_fixed(1), wait_random(0, 0.1)),
        retry=retry_if_not_exception_type((ValueError, TypeError, AttributeError, KeyError)),
        reraise=True,
    )
    async def _search_with_retry(
        self,
        collection_name: str,
        query_vector: list[float],
        query_filter: object,
        limit: int,
        score_threshold: float,
    ) -> list[ScoredPoint]:
        """Execute Qdrant query_points with retry logic."""
        from qdrant_client.models import Filter as QdrantFilter

        flt: QdrantFilter | None = query_filter if isinstance(query_filter, QdrantFilter) else None
        response = await self._client.query_points(
            collection_name=collection_name,
            query=query_vector,
            query_filter=flt,
            limit=limit,
            score_threshold=score_threshold,
            with_payload=True,
        )
        return list(response.points)

    async def upsert_batch(
        self,
        ctx: TenantContext,
        qdrant_collection: str,
        points: list[QdrantPoint],
    ) -> None:
        """Upsert a batch of points. Validates tenant_id in each payload before sending.

        Args:
            ctx: Tenant context. Used for payload validation and audit logging.
            qdrant_collection: Physical Qdrant collection name.
            points: Points to upsert. Each payload MUST include tenant_id, collection_id,
                document_id.

        Raises:
            ValueError: If any point payload tenant_id mismatches ctx.tenant_id.
            QdrantUnavailableError: If Qdrant unreachable after retries.
        """
        for point in points:
            _validate_point_payload(ctx, point)

        qdrant_points = [
            PointStruct(id=str(p.id), vector=p.vector, payload=p.payload) for p in points
        ]

        logger.debug(
            "retrieval.upsert_batch",
            tenant_id=str(ctx.tenant_id),
            count=len(qdrant_points),
            collection=qdrant_collection,
        )

        try:
            await self._upsert_with_retry(qdrant_collection, qdrant_points)
        except ValueError:
            raise
        except Exception as exc:
            raise QdrantUnavailableError(str(exc)) from exc

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_combine(wait_exponential(multiplier=1, min=1, max=10), wait_random(0, 0.2)),
        retry=retry_if_not_exception_type((ValueError, TypeError, AttributeError, KeyError)),
        reraise=True,
    )
    async def _upsert_with_retry(self, collection_name: str, points: list[PointStruct]) -> None:
        """Execute Qdrant upsert with retry logic."""
        await self._client.upsert(
            collection_name=collection_name,
            points=points,
            wait=True,
        )

    async def delete_by_document(
        self,
        ctx: TenantContext,
        qdrant_collection: str,
        document_id: UUID,
    ) -> int:
        """Delete all Qdrant points for a document within the tenant.

        Args:
            ctx: Tenant context for scoping the delete.
            qdrant_collection: Physical Qdrant collection name.
            document_id: The document whose chunks to delete.

        Returns:
            Approximate number of points deleted.

        Raises:
            QdrantUnavailableError: If Qdrant unreachable after retries.
        """
        delete_filter = build_document_delete_filter(
            tenant_id=str(ctx.tenant_id),
            document_id=str(document_id),
        )
        logger.info(
            "retrieval.delete_by_document",
            tenant_id=str(ctx.tenant_id),
            document_id=str(document_id),
        )
        try:
            result = await self._delete_with_retry(qdrant_collection, delete_filter)
        except Exception as exc:
            raise QdrantUnavailableError(str(exc)) from exc
        return getattr(getattr(result, "result", None), "count", 0) or 0

    async def delete_by_tenant(
        self,
        ctx: TenantContext,
        qdrant_collection: str,
    ) -> int:
        """Delete ALL points for a tenant. GDPR right-to-erasure at tenant offboarding.

        WARNING: Destructive. Call only from DeletionService.

        Args:
            ctx: Tenant context identifying which tenant's data to delete.
            qdrant_collection: Physical Qdrant collection name.

        Returns:
            Approximate number of points deleted.

        Raises:
            QdrantUnavailableError: If Qdrant unreachable after retries.
        """
        delete_filter = build_tenant_delete_filter(str(ctx.tenant_id))
        logger.warning(
            "retrieval.delete_by_tenant",
            tenant_id=str(ctx.tenant_id),
            collection=qdrant_collection,
        )
        try:
            result = await self._delete_with_retry(qdrant_collection, delete_filter)
        except Exception as exc:
            raise QdrantUnavailableError(str(exc)) from exc
        return getattr(getattr(result, "result", None), "count", 0) or 0

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_combine(wait_exponential(multiplier=1, min=1, max=10), wait_random(0, 0.2)),
        retry=retry_if_not_exception_type((ValueError, TypeError, AttributeError, KeyError)),
        reraise=True,
    )
    async def _delete_with_retry(self, collection_name: str, delete_filter: object) -> object:
        """Execute Qdrant delete with retry logic."""
        from qdrant_client.models import Filter as QdrantFilter

        flt = delete_filter if isinstance(delete_filter, QdrantFilter) else None
        return await self._client.delete(
            collection_name=collection_name,
            points_selector=FilterSelector(filter=flt),
            wait=True,
        )

    async def ensure_collection(
        self,
        embedding_model_slug: str,
        vector_size: int,
        distance: str = "Cosine",
    ) -> None:
        """Create Qdrant collection emb_{embedding_model_slug} if it does not exist.

        Also creates payload indexes for tenant_id, collection_id, and document_id on first
        creation. Idempotent — safe to call on every collection create.

        Args:
            embedding_model_slug: Slug for the embedding model (e.g., "bge_m3").
            vector_size: Dimensionality of the embedding vectors.
            distance: Distance metric — "Cosine" or "Dot". Default "Cosine".
        """
        collection_name = f"emb_{embedding_model_slug}"
        distance_enum = Distance.COSINE if distance == "Cosine" else Distance.DOT

        existing = {c.name for c in (await self._client.get_collections()).collections}
        if collection_name in existing:
            logger.debug("ensure_collection.already_exists", collection=collection_name)
            return

        await self._client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=vector_size, distance=distance_enum),
        )
        # Create payload indexes for mandatory filter fields
        await self._client.create_payload_index(
            collection_name=collection_name,
            field_name="tenant_id",
            field_schema=PayloadSchemaType.KEYWORD,
        )
        await self._client.create_payload_index(
            collection_name=collection_name,
            field_name="collection_id",
            field_schema=PayloadSchemaType.KEYWORD,
        )
        await self._client.create_payload_index(
            collection_name=collection_name,
            field_name="document_id",
            field_schema=PayloadSchemaType.KEYWORD,
        )
        logger.info(
            "ensure_collection.created",
            collection=collection_name,
            vector_size=vector_size,
            distance=distance,
        )


def get_retrieval_service(timeout: int = 10) -> "RetrievalService":
    """Factory: create a RetrievalService from application settings.

    This is the ONLY place outside RetrievalService itself that may instantiate
    AsyncQdrantClient. Call this from domain services instead of importing
    qdrant_client directly.

    Args:
        timeout: Qdrant client timeout in seconds.

    Returns:
        RetrievalService backed by an AsyncQdrantClient.
    """
    from src.core.config import settings

    client = AsyncQdrantClient(
        url=str(settings.QDRANT_URL),
        api_key=settings.QDRANT_API_KEY,
        timeout=timeout,
    )
    return RetrievalService(client=client)

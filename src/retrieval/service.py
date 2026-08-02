"""RetrievalService — the ONLY module with Qdrant access.

Architecture rule (ADR-1): AsyncQdrantClient is instantiated exclusively here.
Any import of qdrant_client outside src/retrieval/ fails CI.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
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
    SparseVectorParams,
    VectorParams,
)
from qdrant_client.models import (
    SparseVector as QdrantSparseVector,
)
from rank_bm25 import BM25Okapi
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
    build_read_filter,
    build_tenant_delete_filter,
    merge_filters,
)
from src.retrieval.reranker import reciprocal_rank_fusion
from src.retrieval.schemas import QdrantPoint, RetrievalResult, SearchMode, TenantContext

logger = structlog.get_logger(__name__)

# BM25 corpus fetch multiplier — fetch this many more candidates than top_k so
# BM25 has a meaningful corpus to rank over. Value of 5 balances recall vs. latency.
_BM25_CORPUS_MULTIPLIER = 5

# Thread pool for fastembed sparse encoding (CPU-bound, not async-friendly).
_SPARSE_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="fastembed")

# Module-level fastembed sparse encoder singleton — lazily initialised on first use.
# Double-checked locking pattern guards against a race when two threads from
# _SPARSE_EXECUTOR both observe _sparse_model is None simultaneously.
_sparse_model: Any | None = None
_sparse_model_lock = threading.Lock()


def _get_sparse_encoder() -> Any:
    """Return the fastembed SparseTextEmbedding singleton, initialising it if needed.

    Thread-safe: uses double-checked locking so initialisation happens exactly once
    even when called concurrently from multiple _SPARSE_EXECUTOR threads.
    The fastembed import is intentionally lazy to avoid a startup penalty when the
    library is absent (e.g., in environments that only use dense retrieval).
    """
    global _sparse_model  # noqa: PLW0603
    if _sparse_model is None:
        with _sparse_model_lock:
            if _sparse_model is None:
                from fastembed import SparseTextEmbedding

                _sparse_model = SparseTextEmbedding(model_name="Qdrant/bm25")
    return _sparse_model


def _encode_sparse_sync(text: str) -> QdrantSparseVector:
    """Encode *text* to a Qdrant SparseVector using the fastembed BM25 model.

    This is a synchronous function — call it from a ThreadPoolExecutor to avoid
    blocking the event loop.

    Args:
        text: The text to encode (chunk content or query string).

    Returns:
        QdrantSparseVector with indices and values arrays.
    """
    encoder = _get_sparse_encoder()
    result = list(encoder.embed([text]))[0]
    return QdrantSparseVector(indices=list(result.indices), values=list(result.values))


def _resolve_threshold(
    calibrated: float | None,
    explicit: float | None,
    default: float,
) -> float:
    """Return the effective score threshold using the precedence chain.

    Priority (first non-None wins):
      1. calibrated — from ThresholdCalibrationService persisted in DB
      2. explicit   — caller override (e.g. from collection search_config)
      3. default    — application-level constant

    Args:
        calibrated: Calibrated threshold from the last eval sweep, or None.
        explicit: Caller-supplied threshold override, or None.
        default: Application default to use as final fallback.

    Returns:
        The resolved threshold float.
    """
    if calibrated is not None:
        return calibrated
    if explicit is not None:
        return explicit
    return default


def _validate_point_payload(ctx: TenantContext, point: QdrantPoint) -> None:
    """Verify point payload tenant_id matches context. Raises ValueError on mismatch."""
    payload_tenant = point.payload.get("tenant_id")
    if payload_tenant != str(ctx.tenant_id):
        raise ValueError(
            f"Point {point.id} payload tenant_id '{payload_tenant}' "
            f"does not match ctx.tenant_id '{ctx.tenant_id}'"
        )


def _assert_write_allowed_for_public(
    ctx: TenantContext,
    collection_id: UUID | None,
) -> None:
    """Raise PermissionError if ctx.tenant_id may not write to a public collection.

    A collection is considered public if its ID appears in ctx.public_collection_ids.
    Public collections may only be written by the platform-admin tenant that created
    them (managed_by_tenant_id).  RetrievalService does NOT have DB access to look up
    managed_by_tenant_id directly, so the contract is enforced via TenantContext:
      - If collection_id is in public_collection_ids AND NOT in allowed_collection_ids,
        the caller does not own this collection and must not write.
      - If collection_id is in both lists, the caller owns the collection (they are the
        managing tenant) and write is permitted.

    Args:
        ctx: Caller's TenantContext from verified JWT.
        collection_id: The collection the write targets, or None (skipped).

    Raises:
        PermissionError: If the collection is public and the caller is not its owner.
    """
    if collection_id is None:
        return
    public_set = {str(c) for c in ctx.public_collection_ids}
    allowed_set = {str(c) for c in ctx.allowed_collection_ids}
    collection_str = str(collection_id)

    if collection_str in public_set and collection_str not in allowed_set:
        raise PermissionError(
            f"Tenant '{ctx.tenant_id}' does not own public collection '{collection_id}' "
            "and may not write to it. Only the managing tenant may upsert or delete "
            "points in a public collection."
        )


def _scored_points_to_results(hits: list[ScoredPoint]) -> list[RetrievalResult]:
    """Convert raw Qdrant ScoredPoint list to RetrievalResult list."""
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


def _run_bm25(
    query_text: str,
    corpus_results: list[RetrievalResult],
) -> list[RetrievalResult]:
    """Run BM25Okapi over the text payloads of corpus_results and return re-ranked list.

    This is intentionally synchronous — BM25 over top-k*5 documents is sub-millisecond
    and does not warrant an executor or async wrapper.

    Args:
        query_text: Original (not rewritten) user query for tokenization.
        corpus_results: Candidate results with text in payload["text"].

    Returns:
        RetrievalResult list re-ordered by BM25 score descending. Results without
        a text payload are scored 0 and sorted to the bottom.
    """
    if not corpus_results:
        return []

    texts = [str(r.payload.get("text", "")) for r in corpus_results]
    # Simple whitespace tokenization — adequate for BM25; language-agnostic.
    tokenized_corpus = [t.lower().split() for t in texts]
    tokenized_query = query_text.lower().split()

    bm25 = BM25Okapi(tokenized_corpus)
    scores: list[float] = list(bm25.get_scores(tokenized_query))

    indexed = sorted(
        zip(scores, corpus_results, strict=True),
        key=lambda pair: pair[0],
        reverse=True,
    )
    # Replace the dense score with the BM25 score so RRF receives the BM25 rank order.
    return [r.model_copy(update={"score": s}) for s, r in indexed]


class RetrievalService:
    """Single access point to Qdrant.

    Enforces tenant isolation and collection RBAC on every call.
    """

    def __init__(self, client: AsyncQdrantClient) -> None:
        self._client = client

    # Default score threshold used when neither a calibrated value nor an
    # explicit caller value is provided.  Kept as a class constant so it can
    # be referenced in tests and by the retrieve node without hard-coding 0.0.
    DEFAULT_SCORE_THRESHOLD: float = 0.0

    async def _encode_sparse(self, text: str) -> QdrantSparseVector:
        """Encode *text* to a sparse vector using fastembed BM25 in a thread pool.

        Args:
            text: Query or chunk text to encode.

        Returns:
            QdrantSparseVector with BM25 term frequencies.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_SPARSE_EXECUTOR, _encode_sparse_sync, text)

    async def search(
        self,
        ctx: TenantContext,
        qdrant_collection: str,
        query_vector: list[float],
        top_k: int = 8,
        score_threshold: float | None = None,
        additional_filter: object | None = None,
        search_mode: SearchMode = SearchMode.DENSE,
        query_text: str | None = None,
        calibrated_threshold: float | None = None,
    ) -> list[RetrievalResult]:
        """Search for semantically similar chunks within tenant+collections scope.

        Dispatches to hybrid search when search_mode=HYBRID and query_text is provided.

        Score threshold resolution order (first non-None wins):
          1. ``calibrated_threshold`` — value from ThresholdCalibrationService stored in DB,
             fetched by the caller (e.g. retrieve node) via ModelService.get_calibrated_threshold.
          2. ``score_threshold`` — explicit caller override (e.g. collection search_config).
          3. ``RetrievalService.DEFAULT_SCORE_THRESHOLD`` (0.0) — application default.

        Args:
            ctx: Tenant context from verified JWT. Must have at least one allowed_collection_id.
            qdrant_collection: Physical Qdrant collection name (e.g., "emb_bge_m3").
            query_vector: Embedding vector (same model used to index).
            top_k: Maximum results. Default 8.
            score_threshold: Explicit caller override for minimum similarity score.
                Pass None to allow calibrated_threshold or the application default to apply.
            additional_filter: Optional Qdrant Filter merged with mandatory filter.
            search_mode: DENSE (default) or HYBRID. HYBRID requires query_text.
            query_text: Original query text for BM25 tokenization (HYBRID only).
            calibrated_threshold: Calibrated threshold from the last eval sweep.
                Fetched by the caller from ModelService.get_calibrated_threshold().
                Takes precedence over score_threshold when not None.

        Returns:
            List of RetrievalResult ordered by score descending.

        Raises:
            EmptyCollectionListError: If ctx.allowed_collection_ids is empty.
            QdrantUnavailableError: If Qdrant unreachable after retries.
            ValueError: If search_mode=HYBRID but query_text is None or empty.
        """
        effective_threshold = _resolve_threshold(
            calibrated_threshold, score_threshold, self.DEFAULT_SCORE_THRESHOLD
        )

        if search_mode is SearchMode.HYBRID:
            if not query_text:
                raise ValueError("search_mode=HYBRID requires a non-empty query_text")
            return await self.search_hybrid(
                ctx=ctx,
                qdrant_collection=qdrant_collection,
                query_vector=query_vector,
                query_text=query_text,
                top_k=top_k,
                score_threshold=effective_threshold,
                additional_filter=additional_filter,
            )

        from qdrant_client.models import Filter as QdrantFilter

        read_filter = build_read_filter(
            tenant_id=str(ctx.tenant_id),
            allowed_collection_ids=[str(c) for c in ctx.allowed_collection_ids],
            public_collection_ids=[str(c) for c in ctx.public_collection_ids],
        )
        combined = merge_filters(
            read_filter,
            additional_filter if isinstance(additional_filter, QdrantFilter) else None,
        )

        logger.debug(
            "retrieval.search",
            tenant_id=str(ctx.tenant_id),
            collection_count=len(ctx.allowed_collection_ids),
            public_collection_count=len(ctx.public_collection_ids),
            top_k=top_k,
            score_threshold=effective_threshold,
            calibrated=calibrated_threshold is not None,
            search_mode=search_mode.value,
        )

        hits: list[ScoredPoint]
        try:
            hits = await self._search_with_retry(
                qdrant_collection, query_vector, combined, top_k, effective_threshold
            )
        except EmptyCollectionListError:
            raise
        except Exception as exc:
            raise QdrantUnavailableError(str(exc)) from exc

        return _scored_points_to_results(hits)

    async def search_hybrid(
        self,
        ctx: TenantContext,
        qdrant_collection: str,
        query_vector: list[float],
        query_text: str,
        top_k: int = 8,
        score_threshold: float = 0.0,
        additional_filter: object | None = None,
    ) -> list[RetrievalResult]:
        """Hybrid search using Qdrant-native sparse + dense prefetch with RRF fusion.

        Strategy (primary path — requires collection with sparse vector config):
        1. Encode ``query_text`` to a sparse BM25 vector via fastembed in a thread pool.
        2. Issue a single Qdrant ``query_points`` call with two Prefetch branches:
           one dense, one sparse. Qdrant performs RRF server-side.
        3. Return the fused top_k results.

        Fallback path (for collections without sparse vector config):
        If Qdrant rejects the prefetch query (e.g. collection was indexed before the
        sparse vector config was added), the method falls back to the legacy in-memory
        BM25 approach via ``_search_hybrid_bm25_fallback()``.

        Args:
            ctx: Tenant context from verified JWT.
            qdrant_collection: Physical Qdrant collection name.
            query_vector: Embedding vector for the rewritten query.
            query_text: Original user query text for sparse BM25 encoding.
            top_k: Number of final results to return. Default 8.
            score_threshold: Applied to the dense retrieval pass only.
            additional_filter: Optional Qdrant Filter merged with mandatory filter.

        Returns:
            List of RetrievalResult ordered by RRF score descending, length <= top_k.

        Raises:
            EmptyCollectionListError: If ctx.allowed_collection_ids is empty.
            QdrantUnavailableError: If Qdrant unreachable after retries.
        """
        from qdrant_client.models import Filter as QdrantFilter

        read_filter = build_read_filter(
            tenant_id=str(ctx.tenant_id),
            allowed_collection_ids=[str(c) for c in ctx.allowed_collection_ids],
            public_collection_ids=[str(c) for c in ctx.public_collection_ids],
        )
        combined = merge_filters(
            read_filter,
            additional_filter if isinstance(additional_filter, QdrantFilter) else None,
        )

        logger.debug(
            "retrieval.search_hybrid",
            tenant_id=str(ctx.tenant_id),
            collection_count=len(ctx.allowed_collection_ids),
            public_collection_count=len(ctx.public_collection_ids),
            top_k=top_k,
            score_threshold=score_threshold,
        )

        try:
            sparse_vector = await self._encode_sparse(query_text)
        except Exception as exc:
            logger.warning(
                "search_hybrid.sparse_encode_failed_fallback",
                error=str(exc),
            )
            return await self._search_hybrid_bm25_fallback(
                ctx, qdrant_collection, query_vector, query_text, top_k, score_threshold, combined
            )

        try:
            results = await self._query_hybrid_qdrant(
                qdrant_collection, query_vector, sparse_vector, combined, top_k
            )
        except EmptyCollectionListError:
            raise
        except Exception as exc:
            # Fall back to in-memory BM25 hybrid if Qdrant rejects the sparse query
            # (e.g. old collection without sparse_vectors_config).
            logger.warning(
                "search_hybrid.sparse_unavailable_fallback",
                error=str(exc),
            )
            return await self._search_hybrid_bm25_fallback(
                ctx, qdrant_collection, query_vector, query_text, top_k, score_threshold, combined
            )

        return results

    async def _query_hybrid_qdrant(
        self,
        collection_name: str,
        query_vector: list[float],
        sparse_vector: QdrantSparseVector,
        query_filter: object,
        limit: int,
    ) -> list[RetrievalResult]:
        """Execute a Qdrant-native hybrid query using Prefetch + RRF fusion.

        Args:
            collection_name: Physical Qdrant collection name.
            query_vector: Dense embedding vector.
            sparse_vector: Sparse BM25 vector from fastembed.
            query_filter: Combined Qdrant Filter (tenant + collection isolation).
            limit: Maximum results.

        Returns:
            RetrievalResult list ordered by server-side RRF score.
        """
        from qdrant_client.models import Filter as QdrantFilter
        from qdrant_client.models import Fusion, FusionQuery, Prefetch

        flt: QdrantFilter | None = (
            query_filter if isinstance(query_filter, QdrantFilter) else None
        )

        for attempt in range(2):
            try:
                response = await self._client.query_points(
                    collection_name=collection_name,
                    prefetch=[
                        Prefetch(
                            query=query_vector,
                            using="dense",
                            filter=flt,
                            limit=limit * 2,
                        ),
                        Prefetch(
                            query=QdrantSparseVector(
                                indices=sparse_vector.indices,
                                values=sparse_vector.values,
                            ),
                            using="sparse",
                            filter=flt,
                            limit=limit * 2,
                        ),
                    ],
                    query=FusionQuery(fusion=Fusion.RRF),
                    limit=limit,
                    with_payload=True,
                )
                return _scored_points_to_results(list(response.points))
            except (ValueError, TypeError, AttributeError, KeyError):
                raise  # non-transient errors — no retry
            except Exception:
                if attempt == 1:
                    raise
                await asyncio.sleep(1.0)
        raise RuntimeError("unreachable")

    async def _search_hybrid_bm25_fallback(
        self,
        ctx: TenantContext,
        qdrant_collection: str,
        query_vector: list[float],
        query_text: str,
        top_k: int,
        score_threshold: float,
        combined_filter: object,
    ) -> list[RetrievalResult]:
        """Legacy in-memory BM25 hybrid fallback for collections without sparse config.

        Issues two dense Qdrant queries (top_k + corpus), runs BM25 in-memory,
        then merges with RRF. Kept for backward compatibility with collections indexed
        before sparse vector support was added.

        Args:
            ctx: Tenant context (used only for logging counts).
            qdrant_collection: Physical Qdrant collection name.
            query_vector: Embedding vector.
            query_text: Query text for BM25 tokenization.
            top_k: Number of final results.
            score_threshold: Minimum dense similarity score.
            combined_filter: Pre-built Qdrant filter (tenant + collection).

        Returns:
            RetrievalResult list ordered by RRF score descending, length <= top_k.
        """
        corpus_k = top_k * _BM25_CORPUS_MULTIPLIER

        try:
            dense_hits, corpus_hits = await asyncio.gather(
                self._search_with_retry(
                    qdrant_collection, query_vector, combined_filter, top_k, score_threshold
                ),
                self._search_with_retry(
                    qdrant_collection, query_vector, combined_filter, corpus_k, 0.0
                ),
            )
        except EmptyCollectionListError:
            raise
        except Exception as exc:
            raise QdrantUnavailableError(str(exc)) from exc

        dense_results = _scored_points_to_results(dense_hits)
        corpus_results = _scored_points_to_results(corpus_hits)

        from src.core.config import settings

        bm25_results = _run_bm25(query_text, corpus_results)
        fused = reciprocal_rank_fusion(dense_results, bm25_results, k=settings.RETRIEVAL_RRF_K)
        return fused[:top_k]

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

        When a point has a ``sparse_vector``, it is upserted using the named-vector format
        (``{"dense": [...], "sparse": SparseVector(...)}``) so that Qdrant-native hybrid
        search can use both vector types. Points without a sparse_vector fall back to a
        plain flat vector for backward compatibility with older collections.

        Public collection write guard: if a point targets a public collection
        (collection_id in ctx.public_collection_ids) and the caller is NOT the
        owning tenant (collection_id NOT in ctx.allowed_collection_ids), a
        PermissionError is raised before any data reaches Qdrant.

        Args:
            ctx: Tenant context. Used for payload validation and audit logging.
            qdrant_collection: Physical Qdrant collection name.
            points: Points to upsert. Each payload MUST include tenant_id, collection_id,
                document_id.

        Raises:
            PermissionError: If caller attempts to write to a public collection they
                do not manage.
            ValueError: If any point payload tenant_id mismatches ctx.tenant_id.
            QdrantUnavailableError: If Qdrant unreachable after retries.
        """
        for point in points:
            # Public-collection write guard runs BEFORE payload validation so that
            # a non-owner tenant cannot sneak in writes by spoofing the tenant_id field.
            raw_collection_id = point.payload.get("collection_id")
            collection_uuid: UUID | None = (
                UUID(str(raw_collection_id)) if raw_collection_id else None
            )
            _assert_write_allowed_for_public(ctx, collection_uuid)
            _validate_point_payload(ctx, point)

        qdrant_points: list[PointStruct] = []
        for p in points:
            if p.sparse_vector is not None:
                # Named-vector format: enables Qdrant-native sparse + dense hybrid search.
                vectors: Any = {"dense": p.vector, "sparse": p.sparse_vector}
            else:
                # Flat vector format: backward-compatible with collections without sparse config.
                vectors = p.vector
            qdrant_points.append(PointStruct(id=str(p.id), vector=vectors, payload=p.payload))

        logger.debug(
            "retrieval.upsert_batch",
            tenant_id=str(ctx.tenant_id),
            count=len(qdrant_points),
            collection=qdrant_collection,
        )

        try:
            await self._upsert_with_retry(qdrant_collection, qdrant_points)
        except (ValueError, PermissionError):
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
        *,
        collection_id: UUID | None = None,
    ) -> int:
        """Delete all Qdrant points for a document within the tenant.

        Public collection write guard: callers that supply ``collection_id``
        trigger a check against ctx.public_collection_ids. If the collection is
        public and the caller is not the managing tenant, PermissionError is
        raised before any Qdrant call is made.

        Args:
            ctx: Tenant context for scoping the delete.
            qdrant_collection: Physical Qdrant collection name.
            document_id: The document whose chunks to delete.
            collection_id: Optional collection UUID to enable the public-collection
                write guard. Callers should always supply this for public collection
                safety. When None, the guard is skipped (private-only contexts).

        Returns:
            Approximate number of points deleted.

        Raises:
            PermissionError: If the collection is public and the caller is not its
                managing tenant.
            QdrantUnavailableError: If Qdrant unreachable after retries.
        """
        # Public-collection write guard — reject before touching Qdrant.
        _assert_write_allowed_for_public(ctx, collection_id)

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
        except PermissionError:
            raise
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

        The collection is created with named vector config:
          - "dense": standard dense vector of ``vector_size`` dimensions.
          - "sparse": sparse vector using SparseVectorParams (for fastembed BM25).

        Also creates payload indexes for tenant_id, collection_id, and document_id on
        first creation. Idempotent — safe to call on every collection create.

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
            vectors_config={"dense": VectorParams(size=vector_size, distance=distance_enum)},
            sparse_vectors_config={"sparse": SparseVectorParams()},
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


def get_retrieval_service(timeout: int = 10) -> RetrievalService:
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

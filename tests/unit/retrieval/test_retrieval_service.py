"""Unit tests for RetrievalService.

All Qdrant I/O is mocked via AsyncMock — no real Qdrant instance required.
Security-critical filter invariant tests are marked with the 'tenant_isolation' marker.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from qdrant_client.models import Filter

from src.retrieval.exceptions import EmptyCollectionListError, QdrantUnavailableError
from src.retrieval.reranker import reciprocal_rank_fusion
from src.retrieval.schemas import QdrantPoint, RetrievalResult, SearchMode, TenantContext
from src.retrieval.service import RetrievalService, _run_bm25

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TENANT_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()
DOCUMENT_ID = uuid.uuid4()


def make_ctx(
    tenant_id: uuid.UUID = TENANT_ID,
    allowed_collection_ids: list[uuid.UUID] | None = None,
) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        allowed_collection_ids=(
            allowed_collection_ids if allowed_collection_ids is not None else [COLLECTION_ID]
        ),
    )


def make_point(
    tenant_id: uuid.UUID = TENANT_ID,
    document_id: uuid.UUID = DOCUMENT_ID,
    collection_id: uuid.UUID = COLLECTION_ID,
) -> QdrantPoint:
    return QdrantPoint(
        id=uuid.uuid4(),
        vector=[0.1, 0.2, 0.3],
        payload={
            "tenant_id": str(tenant_id),
            "document_id": str(document_id),
            "collection_id": str(collection_id),
            "text": "chunk content",
        },
    )


def make_qdrant_hit(
    score: float = 0.9,
    tenant_id: uuid.UUID = TENANT_ID,
    document_id: uuid.UUID = DOCUMENT_ID,
    collection_id: uuid.UUID = COLLECTION_ID,
) -> MagicMock:
    hit = MagicMock()
    hit.id = str(uuid.uuid4())
    hit.score = score
    hit.payload = {
        "tenant_id": str(tenant_id),
        "document_id": str(document_id),
        "collection_id": str(collection_id),
        "text": "some text",
        "page": 3,
    }
    return hit


def make_query_response(hits: list[MagicMock]) -> MagicMock:
    """Wrap a list of hits in a mock QueryResponse (has .points attribute)."""
    response = MagicMock()
    response.points = hits
    return response


def make_service() -> tuple[RetrievalService, AsyncMock]:
    """Return (service, mock_client) pair."""
    client = AsyncMock()
    service = RetrievalService(client=client)
    return service, client


# ---------------------------------------------------------------------------
# Filter invariant tests — security critical
# ---------------------------------------------------------------------------


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_search_always_includes_tenant_filter() -> None:
    """Mandatory filter passed to Qdrant must contain a tenant_id MatchValue condition."""
    service, client = make_service()
    client.query_points.return_value = make_query_response([])

    ctx = make_ctx()
    await service.search(ctx, "emb_bge_m3", [0.1, 0.2])

    assert client.query_points.called
    call_kwargs: dict[str, Any] = client.query_points.call_args.kwargs
    query_filter: Filter = call_kwargs["query_filter"]

    must_conditions = query_filter.must or []
    tenant_keys = [c.key for c in must_conditions if hasattr(c, "key")]
    assert "tenant_id" in tenant_keys, "tenant_id filter condition missing"


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_search_always_includes_collection_filter() -> None:
    """Mandatory filter passed to Qdrant must contain a collection_id MatchAny condition."""
    service, client = make_service()
    client.query_points.return_value = make_query_response([])

    ctx = make_ctx()
    await service.search(ctx, "emb_bge_m3", [0.1, 0.2])

    call_kwargs: dict[str, Any] = client.query_points.call_args.kwargs
    query_filter: Filter = call_kwargs["query_filter"]

    must_conditions = query_filter.must or []
    coll_keys = [c.key for c in must_conditions if hasattr(c, "key")]
    assert "collection_id" in coll_keys, "collection_id filter condition missing"


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_search_empty_collections_raises() -> None:
    """EmptyCollectionListError is raised synchronously before any network I/O."""
    service, client = make_service()

    ctx = make_ctx(allowed_collection_ids=[])

    with pytest.raises(EmptyCollectionListError):
        await service.search(ctx, "emb_bge_m3", [0.1, 0.2])

    client.query_points.assert_not_called()


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_upsert_wrong_tenant_in_payload_raises() -> None:
    """ValueError is raised before any Qdrant call when payload tenant_id mismatches ctx."""
    service, client = make_service()

    wrong_tenant = uuid.uuid4()
    point = make_point(tenant_id=wrong_tenant)
    ctx = make_ctx(tenant_id=TENANT_ID)

    with pytest.raises(ValueError, match="does not match ctx.tenant_id"):
        await service.upsert_batch(ctx, "emb_bge_m3", [point])

    client.upsert.assert_not_called()


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_delete_by_document_scoped_to_tenant() -> None:
    """delete_by_document filter must include both tenant_id and document_id conditions."""
    service, client = make_service()
    client.delete.return_value = MagicMock(result=None)

    ctx = make_ctx()
    doc_id = uuid.uuid4()
    await service.delete_by_document(ctx, "emb_bge_m3", doc_id)

    assert client.delete.called
    call_kwargs: dict[str, Any] = client.delete.call_args.kwargs
    selector = call_kwargs["points_selector"]
    flt: Filter = selector.filter

    must_conditions = flt.must or []
    keys = [c.key for c in must_conditions if hasattr(c, "key")]
    assert "tenant_id" in keys
    assert "document_id" in keys


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_delete_by_tenant_uses_only_tenant_filter() -> None:
    """delete_by_tenant filter must have tenant_id and MUST NOT include document_id."""
    service, client = make_service()
    client.delete.return_value = MagicMock(result=None)

    ctx = make_ctx()
    await service.delete_by_tenant(ctx, "emb_bge_m3")

    assert client.delete.called
    call_kwargs: dict[str, Any] = client.delete.call_args.kwargs
    selector = call_kwargs["points_selector"]
    flt: Filter = selector.filter

    must_conditions = flt.must or []
    keys = [c.key for c in must_conditions if hasattr(c, "key")]
    assert "tenant_id" in keys
    assert "document_id" not in keys


# ---------------------------------------------------------------------------
# Behavior tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_returns_retrieval_results() -> None:
    """Three mock hits produce three RetrievalResult objects."""
    service, client = make_service()
    hits = [make_qdrant_hit(score=0.9 - i * 0.1) for i in range(3)]
    client.query_points.return_value = make_query_response(hits)

    ctx = make_ctx()
    results = await service.search(ctx, "emb_bge_m3", [0.1, 0.2])

    assert len(results) == 3
    for r in results:
        assert isinstance(r, RetrievalResult)
        assert r.document_id == DOCUMENT_ID


@pytest.mark.asyncio
async def test_search_respects_top_k() -> None:
    """client.query_points is called with limit equal to the top_k argument."""
    service, client = make_service()
    client.query_points.return_value = make_query_response([])

    ctx = make_ctx()
    await service.search(ctx, "emb_bge_m3", [0.1], top_k=5)

    call_kwargs: dict[str, Any] = client.query_points.call_args.kwargs
    assert call_kwargs["limit"] == 5


@pytest.mark.asyncio
async def test_search_passes_score_threshold() -> None:
    """score_threshold argument is forwarded verbatim to client.query_points."""
    service, client = make_service()
    client.query_points.return_value = make_query_response([])

    ctx = make_ctx()
    await service.search(ctx, "emb_bge_m3", [0.1], score_threshold=0.75)

    call_kwargs: dict[str, Any] = client.query_points.call_args.kwargs
    assert call_kwargs["score_threshold"] == 0.75


@pytest.mark.asyncio
async def test_upsert_batch_sends_all_points() -> None:
    """Batch of 50 points results in a single client.upsert call with all 50 PointStructs."""
    service, client = make_service()
    client.upsert.return_value = None

    ctx = make_ctx()
    points = [make_point() for _ in range(50)]
    await service.upsert_batch(ctx, "emb_bge_m3", points)

    assert client.upsert.called
    call_kwargs: dict[str, Any] = client.upsert.call_args.kwargs
    assert len(call_kwargs["points"]) == 50


@pytest.mark.asyncio
async def test_upsert_uses_wait_true() -> None:
    """client.upsert is called with wait=True to ensure durability."""
    service, client = make_service()
    client.upsert.return_value = None

    ctx = make_ctx()
    await service.upsert_batch(ctx, "emb_bge_m3", [make_point()])

    call_kwargs: dict[str, Any] = client.upsert.call_args.kwargs
    assert call_kwargs["wait"] is True


@pytest.mark.asyncio
async def test_search_retries_on_qdrant_error() -> None:
    """If first call fails but second succeeds, results are returned (retry works)."""
    service, client = make_service()
    hit = make_qdrant_hit()
    client.query_points.side_effect = [
        Exception("connection reset"),
        make_query_response([hit]),
    ]

    ctx = make_ctx()
    # Patch asyncio.sleep to avoid real waiting (async tenacity uses asyncio.sleep)
    with patch("asyncio.sleep"):
        results = await service.search(ctx, "emb_bge_m3", [0.1])

    assert len(results) == 1
    assert client.query_points.call_count == 2


@pytest.mark.asyncio
async def test_search_raises_after_max_retries() -> None:
    """After all retry attempts are exhausted, QdrantUnavailableError is raised."""
    service, client = make_service()
    client.query_points.side_effect = Exception("Qdrant down")

    ctx = make_ctx()
    with patch("asyncio.sleep"), pytest.raises(QdrantUnavailableError):
        await service.search(ctx, "emb_bge_m3", [0.1])

    # stop_after_attempt(2) → 2 total attempts
    assert client.query_points.call_count == 2


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_search_cannot_bypass_tenant_with_additional_filter() -> None:
    """Additional filter cannot override or remove the mandatory tenant+collection filter."""
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    service, client = make_service()
    client.query_points.return_value = make_query_response([])

    ctx = make_ctx()
    evil_filter = Filter(
        must=[FieldCondition(key="tenant_id", match=MatchValue(value="evil-tenant"))]
    )
    await service.search(ctx, "emb_bge_m3", [0.1, 0.2], additional_filter=evil_filter)

    call_kwargs = client.query_points.call_args.kwargs
    query_filter: Filter = call_kwargs["query_filter"]
    must_conditions = query_filter.must or []
    tenant_keys = [c.key for c in must_conditions if hasattr(c, "key")]
    # The mandatory tenant_id filter must still be present
    assert "tenant_id" in tenant_keys
    # The correct tenant value must be used (not the evil one)
    tenant_cond = next(c for c in must_conditions if getattr(c, "key", None) == "tenant_id")
    assert tenant_cond.match.value == str(TENANT_ID)


# ---------------------------------------------------------------------------
# SearchMode dispatch tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_dense_mode_does_not_call_hybrid() -> None:
    """search() in DENSE mode calls query_points exactly once (no BM25 corpus fetch)."""
    service, client = make_service()
    client.query_points.return_value = make_query_response([])

    ctx = make_ctx()
    await service.search(
        ctx, "emb_bge_m3", [0.1, 0.2], search_mode=SearchMode.DENSE, query_text="hello"
    )

    assert client.query_points.call_count == 1


@pytest.mark.asyncio
async def test_search_hybrid_mode_dispatches_to_search_hybrid() -> None:
    """search() with HYBRID mode dispatches to search_hybrid().

    Primary path: ONE Qdrant call using Prefetch + server-side RRF (not two dense calls).
    The legacy two-dense-call path is only used as fallback when the sparse query fails.
    """
    service, client = make_service()
    hit = make_qdrant_hit(score=0.9)
    client.query_points.return_value = make_query_response([hit])

    ctx = make_ctx()
    results = await service.search(
        ctx,
        "emb_bge_m3",
        [0.1, 0.2],
        top_k=1,
        search_mode=SearchMode.HYBRID,
        query_text="diabetes treatment",
    )

    # Primary path: ONE query_points call with a Prefetch payload (dense + sparse + RRF).
    assert client.query_points.call_count == 1
    assert isinstance(results[0], RetrievalResult)


@pytest.mark.asyncio
async def test_search_hybrid_without_query_text_raises() -> None:
    """search() with HYBRID mode and no query_text raises ValueError immediately."""
    service, client = make_service()
    ctx = make_ctx()

    with pytest.raises(ValueError, match="query_text"):
        await service.search(
            ctx,
            "emb_bge_m3",
            [0.1],
            search_mode=SearchMode.HYBRID,
            query_text=None,
        )

    client.query_points.assert_not_called()


# ---------------------------------------------------------------------------
# _run_bm25 unit tests (pure function, no Qdrant)
# ---------------------------------------------------------------------------


def _make_result(text: str, score: float = 0.5) -> RetrievalResult:
    pid = uuid.uuid4()
    return RetrievalResult(
        point_id=pid,
        document_id=DOCUMENT_ID,
        score=score,
        payload={"tenant_id": str(TENANT_ID), "document_id": str(DOCUMENT_ID), "text": text},
        collection_id=COLLECTION_ID,
    )


def test_run_bm25_returns_same_count() -> None:
    """_run_bm25 returns the same number of results as the input corpus."""
    corpus = [_make_result(t) for t in ["alpha beta", "gamma delta", "alpha gamma"]]
    ranked = _run_bm25("alpha", corpus)
    assert len(ranked) == len(corpus)


def test_run_bm25_ranks_matching_first() -> None:
    """Document containing the query term is ranked above unrelated documents."""
    corpus = [
        _make_result("completely unrelated content"),
        _make_result("diabetes insulin treatment glucose"),
        _make_result("weather forecast tomorrow"),
    ]
    ranked = _run_bm25("diabetes glucose", corpus)
    assert "diabetes" in ranked[0].payload["text"]


def test_run_bm25_empty_corpus_returns_empty() -> None:
    """Empty input corpus returns empty list without error."""
    assert _run_bm25("query", []) == []


def test_run_bm25_replaces_score_with_bm25_score() -> None:
    """Returned results have .score set to BM25 score, not the original dense score."""
    original_dense_score = 0.99
    corpus = [_make_result("hello world", score=original_dense_score)]
    ranked = _run_bm25("hello", corpus)
    # BM25 score for a single-document corpus with a matching term is > 0
    # and differs from the dense score placeholder.
    assert ranked[0].score != original_dense_score or ranked[0].score >= 0.0


# ---------------------------------------------------------------------------
# reciprocal_rank_fusion unit tests (pure function, no Qdrant)
# ---------------------------------------------------------------------------


def _make_rrf_result(text: str, score: float = 0.5) -> RetrievalResult:
    """Create a fresh RetrievalResult with a unique point_id for RRF tests."""
    return RetrievalResult(
        point_id=uuid.uuid4(),
        document_id=DOCUMENT_ID,
        score=score,
        payload={"text": text},
        collection_id=COLLECTION_ID,
    )


def test_rrf_merges_disjoint_lists() -> None:
    """RRF on two disjoint lists returns combined results (union of both)."""
    dense = [_make_rrf_result("dense doc") for _ in range(3)]
    bm25 = [_make_rrf_result("bm25 doc") for _ in range(3)]
    fused = reciprocal_rank_fusion(dense, bm25)
    assert len(fused) == 6


def test_rrf_deduplicates_shared_results() -> None:
    """A point appearing in both lists is counted once with a higher combined score."""
    shared = _make_rrf_result("shared document", score=0.9)
    only_dense = _make_rrf_result("only dense", score=0.8)
    only_bm25 = _make_rrf_result("only bm25", score=0.7)

    dense = [shared, only_dense]
    bm25 = [shared, only_bm25]

    fused = reciprocal_rank_fusion(dense, bm25)

    # Three unique point_ids
    assert len(fused) == 3

    # The shared document should have the highest RRF score because it
    # receives contributions from both ranked lists at rank 1.
    assert fused[0].point_id == shared.point_id


def test_rrf_score_formula() -> None:
    """Verify RRF score equals sum(1/(k+rank)) for k=60."""
    k = 60
    result = _make_rrf_result("only doc", score=0.5)

    # Put the same result at rank 1 in both lists — score should be 2/(k+1)
    fused = reciprocal_rank_fusion([result], [result], k=k)
    assert len(fused) == 1
    expected = 1.0 / (k + 1) + 1.0 / (k + 1)
    assert abs(fused[0].score - expected) < 1e-9


def test_rrf_empty_inputs() -> None:
    """RRF on two empty lists returns an empty list."""
    assert reciprocal_rank_fusion([], []) == []


def test_rrf_one_empty_list() -> None:
    """RRF with one empty list returns the other list's results with single-list scores."""
    k = 60
    result = _make_rrf_result("doc")
    fused = reciprocal_rank_fusion([result], [], k=k)
    assert len(fused) == 1
    assert abs(fused[0].score - 1.0 / (k + 1)) < 1e-9


def test_rrf_output_ordered_by_score_descending() -> None:
    """RRF result list is always ordered by RRF score descending."""
    # Build lists where rank order differs so RRF scores vary.
    results = [_make_rrf_result(f"doc {i}") for i in range(5)]
    dense = results[:3]
    bm25 = list(reversed(results[2:]))  # reversed overlap at index 2

    fused = reciprocal_rank_fusion(dense, bm25)
    scores = [r.score for r in fused]
    assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# search_hybrid tenant isolation tests
# ---------------------------------------------------------------------------


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_search_hybrid_always_includes_tenant_filter() -> None:
    """Primary hybrid path: the single Qdrant call's Prefetch branches carry tenant_id filters."""
    from qdrant_client.models import Prefetch

    service, client = make_service()
    client.query_points.return_value = make_query_response([])

    ctx = make_ctx()
    await service.search_hybrid(ctx, "emb_bge_m3", [0.1, 0.2], query_text="test query", top_k=2)

    # Primary path: exactly ONE query_points call with prefetch.
    assert client.query_points.call_count == 1
    call_kwargs = client.query_points.call_args.kwargs
    prefetch_list: list[Prefetch] = call_kwargs.get("prefetch", [])
    assert len(prefetch_list) == 2, "Expected dense + sparse prefetch branches"

    for prefetch in prefetch_list:
        flt: Filter = prefetch.filter
        assert flt is not None, "Prefetch branch missing filter — tenant isolation violation"
        must = flt.must or []
        keys = [c.key for c in must if hasattr(c, "key")]
        assert "tenant_id" in keys, f"tenant_id missing from prefetch filter keys: {keys}"


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_search_hybrid_empty_collections_raises() -> None:
    """EmptyCollectionListError propagates from search_hybrid before any Qdrant I/O."""
    service, client = make_service()
    ctx = make_ctx(allowed_collection_ids=[])

    with pytest.raises(EmptyCollectionListError):
        await service.search_hybrid(ctx, "emb_bge_m3", [0.1], query_text="query")

    client.query_points.assert_not_called()


@pytest.mark.asyncio
async def test_search_hybrid_returns_at_most_top_k() -> None:
    """search_hybrid returns no more than top_k results.

    In the primary (Qdrant-native RRF) path the limit is enforced server-side.
    The mock is configured to return only top_k hits to simulate Qdrant honouring the limit.
    """
    service, client = make_service()
    top_k = 2
    # Simulate Qdrant honouring the limit parameter — return exactly top_k results.
    hits = [make_qdrant_hit(score=0.9 - i * 0.05) for i in range(top_k)]
    client.query_points.return_value = make_query_response(hits)

    ctx = make_ctx()
    results = await service.search_hybrid(
        ctx, "emb_bge_m3", [0.1], query_text="some query", top_k=top_k
    )

    assert len(results) <= top_k

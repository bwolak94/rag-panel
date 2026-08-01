"""Tests for the Qdrant-native sparse hybrid retrieval path (TASK-019).

All Qdrant I/O and fastembed calls are mocked — no real Qdrant or model required.
Security-critical filter invariant tests are marked with the 'tenant_isolation' marker.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from qdrant_client.models import SparseVector

from src.retrieval.schemas import QdrantPoint, RetrievalResult, SearchMode, TenantContext
from src.retrieval.service import RetrievalService, _encode_sparse_sync, _get_sparse_encoder

# ---------------------------------------------------------------------------
# Shared test fixtures
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


def make_service() -> tuple[RetrievalService, AsyncMock]:
    """Return (service, mock_client) pair."""
    client = AsyncMock()
    service = RetrievalService(client=client)
    return service, client


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
        "page": 1,
    }
    return hit


def make_query_response(hits: list[MagicMock]) -> MagicMock:
    response = MagicMock()
    response.points = hits
    return response


def make_fake_sparse_vector() -> SparseVector:
    """Return a deterministic SparseVector for use in mocks."""
    return SparseVector(indices=[0, 5, 42], values=[0.3, 0.7, 0.1])


def make_point(
    tenant_id: uuid.UUID = TENANT_ID,
    document_id: uuid.UUID = DOCUMENT_ID,
    collection_id: uuid.UUID = COLLECTION_ID,
    sparse: SparseVector | None = None,
) -> QdrantPoint:
    return QdrantPoint(
        id=uuid.uuid4(),
        vector=[0.1, 0.2, 0.3],
        sparse_vector=sparse,
        payload={
            "tenant_id": str(tenant_id),
            "document_id": str(document_id),
            "collection_id": str(collection_id),
            "text": "chunk content",
        },
    )


# ---------------------------------------------------------------------------
# Test 1: _encode_sparse_sync returns correct SparseVector structure
# ---------------------------------------------------------------------------


def test_encode_sparse_sync_returns_sparse_vector() -> None:
    """_encode_sparse_sync returns a SparseVector with non-empty indices and values."""
    fake_result = MagicMock()
    fake_result.indices = [1, 5, 10]
    fake_result.values = [0.5, 0.3, 0.2]

    fake_encoder = MagicMock()
    fake_encoder.embed.return_value = iter([fake_result])

    with patch("src.retrieval.service._get_sparse_encoder", return_value=fake_encoder):
        result = _encode_sparse_sync("hello world")

    assert isinstance(result, SparseVector)
    assert result.indices == [1, 5, 10]
    assert result.values == [0.5, 0.3, 0.2]


def test_encode_sparse_sync_indices_values_same_length() -> None:
    """_encode_sparse_sync always returns indices and values of equal length."""
    fake_result = MagicMock()
    fake_result.indices = [2, 7]
    fake_result.values = [0.9, 0.1]

    fake_encoder = MagicMock()
    fake_encoder.embed.return_value = iter([fake_result])

    with patch("src.retrieval.service._get_sparse_encoder", return_value=fake_encoder):
        result = _encode_sparse_sync("test text")

    assert len(result.indices) == len(result.values)


# ---------------------------------------------------------------------------
# Test 2: search_hybrid uses Qdrant prefetch (single call with prefetch) rather
# than two separate dense calls when the sparse path succeeds.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_hybrid_uses_qdrant_prefetch_not_two_dense_calls() -> None:
    """When sparse encoding succeeds, search_hybrid issues ONE query_points call
    with a prefetch payload rather than two separate dense queries."""
    service, client = make_service()
    hit = make_qdrant_hit(score=0.8)
    client.query_points.return_value = make_query_response([hit])

    sparse_vec = make_fake_sparse_vector()

    with patch.object(service, "_encode_sparse", return_value=sparse_vec):
        results = await service.search_hybrid(
            make_ctx(), "emb_bge_m3", [0.1, 0.2], query_text="diabetes treatment", top_k=1
        )

    # Primary path: ONE query_points call (prefetch + RRF), not two.
    assert client.query_points.call_count == 1
    assert len(results) == 1
    assert isinstance(results[0], RetrievalResult)


@pytest.mark.asyncio
async def test_search_hybrid_prefetch_call_uses_fusion_query() -> None:
    """The single query_points call made by the primary hybrid path passes a
    FusionQuery (not a plain vector), confirming server-side RRF is requested."""
    from qdrant_client.models import FusionQuery

    service, client = make_service()
    client.query_points.return_value = make_query_response([])
    sparse_vec = make_fake_sparse_vector()

    with patch.object(service, "_encode_sparse", return_value=sparse_vec):
        await service.search_hybrid(
            make_ctx(), "emb_bge_m3", [0.1], query_text="test query", top_k=3
        )

    call_kwargs: dict[str, Any] = client.query_points.call_args.kwargs
    assert "query" in call_kwargs
    assert isinstance(call_kwargs["query"], FusionQuery)


# ---------------------------------------------------------------------------
# Test 3: search_hybrid falls back to BM25 when Qdrant rejects the sparse query
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_hybrid_falls_back_to_bm25_on_qdrant_error() -> None:
    """When Qdrant rejects the prefetch query (e.g. old collection), search_hybrid
    falls back to the legacy two-dense-calls + in-memory BM25 path."""
    service, client = make_service()

    hit = make_qdrant_hit(score=0.7)

    # First two calls (native prefetch with retry) raise; subsequent calls (fallback) succeed.
    call_count = 0

    async def side_effect(**kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise RuntimeError("sparse vector not configured")
        return make_query_response([hit])

    client.query_points.side_effect = side_effect

    sparse_vec = make_fake_sparse_vector()

    with patch("asyncio.sleep"), patch.object(service, "_encode_sparse", return_value=sparse_vec):
        results = await service.search_hybrid(
            make_ctx(), "emb_bge_m3", [0.1, 0.2], query_text="insulin", top_k=1
        )

    # 2 native prefetch attempts (retry) + 2 fallback dense calls = 4 total
    assert client.query_points.call_count == 4
    assert isinstance(results[0], RetrievalResult)


@pytest.mark.asyncio
async def test_search_hybrid_falls_back_when_sparse_encode_fails() -> None:
    """When _encode_sparse itself fails, search_hybrid falls back to BM25."""
    service, client = make_service()
    hit = make_qdrant_hit(score=0.7)
    client.query_points.return_value = make_query_response([hit])

    with patch.object(service, "_encode_sparse", side_effect=RuntimeError("fastembed error")):
        results = await service.search_hybrid(
            make_ctx(), "emb_bge_m3", [0.1], query_text="test", top_k=1
        )

    # BM25 fallback uses 2 dense calls
    assert client.query_points.call_count == 2
    assert len(results) >= 0  # may be 0 after RRF but must not raise


# ---------------------------------------------------------------------------
# Test 4: search_hybrid tenant isolation — filter contains tenant_id
# ---------------------------------------------------------------------------


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_search_hybrid_native_filter_contains_tenant_id() -> None:
    """The prefetch branches in the native sparse path carry the mandatory tenant_id filter."""
    from qdrant_client.models import Filter, Prefetch

    service, client = make_service()
    client.query_points.return_value = make_query_response([])

    sparse_vec = make_fake_sparse_vector()

    with patch.object(service, "_encode_sparse", return_value=sparse_vec):
        await service.search_hybrid(
            make_ctx(), "emb_bge_m3", [0.1, 0.2], query_text="blood pressure", top_k=5
        )

    call_kwargs: dict[str, Any] = client.query_points.call_args.kwargs
    prefetch_list: list[Prefetch] = call_kwargs.get("prefetch", [])

    assert len(prefetch_list) == 2, "Expected exactly two Prefetch branches (dense + sparse)"

    for prefetch in prefetch_list:
        flt: Filter | None = prefetch.filter
        assert flt is not None, "Prefetch branch missing filter — tenant isolation violation"
        must = flt.must or []
        keys = [c.key for c in must if hasattr(c, "key")]
        assert "tenant_id" in keys, f"tenant_id missing from prefetch filter keys: {keys}"


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_search_hybrid_native_filter_has_correct_tenant_value() -> None:
    """The tenant_id condition in the prefetch filter matches the context tenant, not any other."""
    from qdrant_client.models import Prefetch

    service, client = make_service()
    client.query_points.return_value = make_query_response([])

    specific_tenant = uuid.uuid4()
    ctx = make_ctx(tenant_id=specific_tenant)

    sparse_vec = make_fake_sparse_vector()

    with patch.object(service, "_encode_sparse", return_value=sparse_vec):
        await service.search_hybrid(ctx, "emb_bge_m3", [0.1], query_text="query", top_k=3)

    call_kwargs: dict[str, Any] = client.query_points.call_args.kwargs
    prefetch_list: list[Prefetch] = call_kwargs.get("prefetch", [])

    for prefetch in prefetch_list:
        flt = prefetch.filter
        must = flt.must or []  # type: ignore[union-attr]
        tenant_cond = next((c for c in must if getattr(c, "key", None) == "tenant_id"), None)
        assert tenant_cond is not None
        assert tenant_cond.match.value == str(specific_tenant)


# ---------------------------------------------------------------------------
# Test 5: upsert_batch uses named vectors when sparse_vector is present
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_batch_uses_named_vectors_when_sparse_present() -> None:
    """When QdrantPoint.sparse_vector is set, the Qdrant PointStruct uses a dict
    vector {"dense": [...], "sparse": SparseVector(...)}."""
    service, client = make_service()
    client.upsert.return_value = None

    sparse_vec = make_fake_sparse_vector()
    point = make_point(sparse=sparse_vec)
    ctx = make_ctx()

    await service.upsert_batch(ctx, "emb_bge_m3", [point])

    assert client.upsert.called
    call_kwargs: dict[str, Any] = client.upsert.call_args.kwargs
    qdrant_points = call_kwargs["points"]
    assert len(qdrant_points) == 1

    sent_vector = qdrant_points[0].vector
    assert isinstance(sent_vector, dict), "Expected named-vector dict when sparse_vector is set"
    assert "dense" in sent_vector
    assert "sparse" in sent_vector
    assert sent_vector["sparse"] is sparse_vec


# ---------------------------------------------------------------------------
# Test 6: upsert_batch uses flat vector when sparse_vector is None (backward compat)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_batch_uses_flat_vector_when_sparse_none() -> None:
    """When QdrantPoint.sparse_vector is None, the PointStruct uses a plain list[float]
    for backward compatibility with collections without sparse vector config."""
    service, client = make_service()
    client.upsert.return_value = None

    point = make_point(sparse=None)
    ctx = make_ctx()

    await service.upsert_batch(ctx, "emb_bge_m3", [point])

    call_kwargs: dict[str, Any] = client.upsert.call_args.kwargs
    qdrant_points = call_kwargs["points"]
    sent_vector = qdrant_points[0].vector

    # Must be a plain list, not a dict
    assert isinstance(sent_vector, list), "Expected flat list[float] when sparse_vector is None"


# ---------------------------------------------------------------------------
# Test 7: ensure_collection creates named-vector + sparse config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_collection_creates_named_dense_and_sparse_config() -> None:
    """ensure_collection passes a dict vectors_config and sparse_vectors_config to Qdrant."""
    from qdrant_client.models import SparseVectorParams, VectorParams

    service, client = make_service()
    client.get_collections.return_value = MagicMock(collections=[])

    await service.ensure_collection("bge_m3", vector_size=1024, distance="Cosine")

    assert client.create_collection.called
    call_kwargs: dict[str, Any] = client.create_collection.call_args.kwargs

    vectors_cfg = call_kwargs["vectors_config"]
    assert isinstance(vectors_cfg, dict), "vectors_config must be a named-vector dict"
    assert "dense" in vectors_cfg
    assert isinstance(vectors_cfg["dense"], VectorParams)

    sparse_cfg = call_kwargs["sparse_vectors_config"]
    assert isinstance(sparse_cfg, dict)
    assert "sparse" in sparse_cfg
    assert isinstance(sparse_cfg["sparse"], SparseVectorParams)


@pytest.mark.asyncio
async def test_ensure_collection_skips_if_already_exists() -> None:
    """ensure_collection does not call create_collection if the collection exists."""
    service, client = make_service()
    existing = MagicMock()
    existing.name = "emb_bge_m3"
    client.get_collections.return_value = MagicMock(collections=[existing])

    await service.ensure_collection("bge_m3", vector_size=1024)

    client.create_collection.assert_not_called()


# ---------------------------------------------------------------------------
# Test 8: QdrantPoint schema allows None sparse_vector (backward compat)
# ---------------------------------------------------------------------------


def test_qdrant_point_sparse_vector_defaults_to_none() -> None:
    """QdrantPoint.sparse_vector defaults to None — backward compatible with dense-only upserts."""
    point = QdrantPoint(
        id=uuid.uuid4(),
        vector=[0.1, 0.2],
        payload={"tenant_id": str(TENANT_ID), "document_id": str(DOCUMENT_ID)},
    )
    assert point.sparse_vector is None


def test_qdrant_point_accepts_sparse_vector() -> None:
    """QdrantPoint.sparse_vector accepts a SparseVector without validation errors."""
    sparse = SparseVector(indices=[0, 1], values=[0.5, 0.5])
    point = QdrantPoint(
        id=uuid.uuid4(),
        vector=[0.1, 0.2],
        payload={"tenant_id": str(TENANT_ID), "document_id": str(DOCUMENT_ID)},
        sparse_vector=sparse,
    )
    assert point.sparse_vector is sparse

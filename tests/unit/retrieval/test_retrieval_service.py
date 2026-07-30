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
from src.retrieval.schemas import QdrantPoint, RetrievalResult, TenantContext
from src.retrieval.service import RetrievalService

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

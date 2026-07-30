"""Integration tests for RetrievalService against a real Qdrant instance.

Uses testcontainers to spin up Qdrant. Tests are skipped automatically if Docker
is unavailable.

Mark: @pytest.mark.integration
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

qdrant_client = pytest.importorskip("qdrant_client", reason="qdrant-client not installed")

try:
    from testcontainers.core.container import DockerContainer  # type: ignore[import-untyped]
    from testcontainers.core.waiting_utils import wait_for_logs  # type: ignore[import-untyped]
except ImportError:
    pytest.skip("testcontainers not installed", allow_module_level=True)

from qdrant_client import AsyncQdrantClient  # noqa: E402

from src.retrieval.exceptions import EmptyCollectionListError  # noqa: E402
from src.retrieval.schemas import QdrantPoint, TenantContext  # noqa: E402
from src.retrieval.service import RetrievalService  # noqa: E402

VECTOR_SIZE = 3
COLLECTION_NAME = "emb_test_model"

TENANT_A = uuid.uuid4()
TENANT_B = uuid.uuid4()
COLLECTION_A = uuid.uuid4()
COLLECTION_B = uuid.uuid4()
DOCUMENT_A = uuid.uuid4()
DOCUMENT_B = uuid.uuid4()


def make_ctx(
    tenant_id: uuid.UUID = TENANT_A,
    allowed_collection_ids: list[uuid.UUID] | None = None,
) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        allowed_collection_ids=(
            allowed_collection_ids if allowed_collection_ids is not None else [COLLECTION_A]
        ),
    )


def make_point(
    tenant_id: uuid.UUID = TENANT_A,
    document_id: uuid.UUID = DOCUMENT_A,
    collection_id: uuid.UUID = COLLECTION_A,
    vector: list[float] | None = None,
) -> QdrantPoint:
    return QdrantPoint(
        id=uuid.uuid4(),
        vector=vector or [0.1, 0.2, 0.3],
        payload={
            "tenant_id": str(tenant_id),
            "document_id": str(document_id),
            "collection_id": str(collection_id),
            "text": "test chunk",
        },
    )


@pytest.fixture(scope="module")
def qdrant_container() -> Any:
    """Start a Qdrant container for the test module."""
    container = DockerContainer("qdrant/qdrant:latest")
    container.with_exposed_ports(6333)
    container.start()
    wait_for_logs(container, "Qdrant gRPC listening", timeout=30)
    yield container
    container.stop()


@pytest.fixture(scope="module")
async def qdrant_service(qdrant_container: Any) -> RetrievalService:
    """Return a RetrievalService connected to the test Qdrant container."""
    host = qdrant_container.get_container_host_ip()
    port = int(qdrant_container.get_exposed_port(6333))
    client = AsyncQdrantClient(host=host, port=port)
    service = RetrievalService(client=client)
    await service.ensure_collection(
        embedding_model_slug="test_model",
        vector_size=VECTOR_SIZE,
        distance="Cosine",
    )
    return service


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_then_search_returns_correct_results(
    qdrant_service: RetrievalService,
) -> None:
    """Upserted points are retrievable by similarity search within the same tenant."""
    service = qdrant_service
    ctx = make_ctx()
    target_vector = [1.0, 0.0, 0.0]
    point = make_point(vector=target_vector)

    await service.upsert_batch(ctx, COLLECTION_NAME, [point])

    results = await service.search(
        ctx, COLLECTION_NAME, target_vector, top_k=5, score_threshold=0.0
    )

    assert len(results) >= 1
    found_ids = [r.point_id for r in results]
    assert point.id in found_ids


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_by_document_removes_only_target_chunks(
    qdrant_service: RetrievalService,
) -> None:
    """delete_by_document removes only chunks belonging to that document, not others."""
    service = qdrant_service
    ctx = make_ctx()

    doc_to_delete = uuid.uuid4()
    doc_to_keep = uuid.uuid4()

    point_del_1 = make_point(document_id=doc_to_delete, vector=[0.9, 0.1, 0.0])
    point_del_2 = make_point(document_id=doc_to_delete, vector=[0.8, 0.1, 0.1])
    point_keep = make_point(document_id=doc_to_keep, vector=[0.0, 0.0, 1.0])

    await service.upsert_batch(ctx, COLLECTION_NAME, [point_del_1, point_del_2, point_keep])

    await service.delete_by_document(ctx, COLLECTION_NAME, doc_to_delete)

    # Search with a vector close to deleted points — none should appear
    results_deleted = await service.search(
        ctx, COLLECTION_NAME, [0.9, 0.1, 0.0], top_k=10, score_threshold=0.0
    )
    deleted_ids = {r.point_id for r in results_deleted}
    assert point_del_1.id not in deleted_ids
    assert point_del_2.id not in deleted_ids

    # The kept point must still exist
    results_kept = await service.search(
        ctx, COLLECTION_NAME, [0.0, 0.0, 1.0], top_k=10, score_threshold=0.0
    )
    kept_ids = {r.point_id for r in results_kept}
    assert point_keep.id in kept_ids


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tenant_isolation_with_real_qdrant(
    qdrant_service: RetrievalService,
) -> None:
    """Tenant B's points are never returned in Tenant A's search results."""
    service = qdrant_service

    ctx_a = TenantContext(tenant_id=TENANT_A, allowed_collection_ids=[COLLECTION_A])
    ctx_b = TenantContext(tenant_id=TENANT_B, allowed_collection_ids=[COLLECTION_B])

    # Upsert a point for Tenant B with a distinctive vector
    point_b = make_point(
        tenant_id=TENANT_B,
        collection_id=COLLECTION_B,
        document_id=DOCUMENT_B,
        vector=[0.5, 0.5, 0.0],
    )
    await service.upsert_batch(ctx_b, COLLECTION_NAME, [point_b])

    # Search as Tenant A with the same vector — must NOT return Tenant B's point
    results = await service.search(
        ctx_a, COLLECTION_NAME, [0.5, 0.5, 0.0], top_k=10, score_threshold=0.0
    )
    result_ids = {r.point_id for r in results}
    assert point_b.id not in result_ids, (
        "Tenant isolation violated: Tenant B's point appeared in Tenant A's search results"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_empty_collection_list_never_reaches_qdrant(
    qdrant_service: RetrievalService,
) -> None:
    """EmptyCollectionListError is raised before any I/O when allowed_collection_ids is empty."""
    service = qdrant_service
    ctx = TenantContext(tenant_id=TENANT_A, allowed_collection_ids=[])

    with pytest.raises(EmptyCollectionListError):
        await service.search(ctx, COLLECTION_NAME, [0.1, 0.2, 0.3])

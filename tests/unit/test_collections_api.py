"""Unit tests for Collections API endpoints.

Dependencies (get_current_ctx, get_db_session) are overridden.
CollectionService is patched at the router level — no DB or Qdrant needed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies.auth import get_current_ctx
from src.api.dependencies.retrieval import get_retrieval_service
from src.api.schemas.collection import (
    ChunkConfig,
    CollectionListResponse,
    CollectionResponse,
    EmbeddingModelRef,
    ValidationConfig,
)
from src.core.database import get_db_session
from src.domain.auth import UserContext

TENANT_ID = uuid.uuid4()
USER_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()
MODEL_ID = uuid.uuid4()


def _make_user_context(permissions: frozenset[str], allowed_ids: frozenset[uuid.UUID] | None = None) -> UserContext:
    return UserContext(
        user_id=USER_ID,
        keycloak_sub="kc-user",
        email="user@example.com",
        display_name="Test User",
        tenant_id=TENANT_ID,
        roles=frozenset({"admin"} if "admin:collections" in permissions else {"viewer"}),
        permissions=permissions,
        allowed_collection_ids=allowed_ids if allowed_ids is not None else frozenset({COLLECTION_ID}),
        writable_collection_ids=frozenset({COLLECTION_ID}) if "admin:collections" in permissions else frozenset(),
    )


def _admin_ctx() -> UserContext:
    return _make_user_context(frozenset({"admin:collections", "documents:read"}))


def _viewer_ctx() -> UserContext:
    return _make_user_context(frozenset({"documents:read"}))


def _make_embedding_model_ref() -> EmbeddingModelRef:
    return EmbeddingModelRef(
        id=MODEL_ID,
        name="bge-m3",
        model_id="BAAI/bge-m3",
        provider="ollama",
        params={"dimensions": 1024},
    )


def _make_collection_response(**overrides: Any) -> CollectionResponse:
    now = datetime.now(UTC)
    defaults: dict[str, Any] = {
        "id": COLLECTION_ID,
        "tenant_id": TENANT_ID,
        "name": "Test Collection",
        "description": None,
        "embedding_model": _make_embedding_model_ref(),
        "chunk_config": ChunkConfig(),
        "validation_config": ValidationConfig(),
        "is_active": True,
        "document_count": 0,
        "created_at": now,
        "updated_at": now,
    }
    defaults.update(overrides)
    return CollectionResponse(**defaults)


def _make_app(ctx: UserContext):
    from src.main import create_app

    app = create_app()

    async def _fake_session():
        yield MagicMock(spec=AsyncSession)

    app.dependency_overrides[get_current_ctx] = lambda: ctx
    app.dependency_overrides[get_db_session] = _fake_session
    app.dependency_overrides[get_retrieval_service] = lambda: MagicMock()
    return app


@pytest.mark.asyncio
async def test_create_collection_valid_returns_201() -> None:
    """POST /collections with valid body → 201 CollectionResponse."""
    app = _make_app(_admin_ctx())
    response = _make_collection_response()

    with patch("src.api.routers.collections._get_service") as mock_get_svc:
        mock_svc = AsyncMock()
        mock_get_svc.return_value = mock_svc
        mock_svc.create_collection.return_value = response

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/collections/",
                json={
                    "name": "Test Collection",
                    "embedding_model_id": str(MODEL_ID),
                    "chunk_config": {"chunk_size": 512, "overlap": 64},
                },
            )

    assert resp.status_code == 201
    assert resp.json()["name"] == "Test Collection"


@pytest.mark.asyncio
async def test_create_collection_as_viewer_returns_403() -> None:
    """POST /collections without admin:collections → 403."""
    app = _make_app(_viewer_ctx())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/collections/",
            json={"name": "x", "embedding_model_id": str(MODEL_ID)},
        )

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_create_collection_invalid_chunk_config_returns_422() -> None:
    """POST /collections with overlap >= chunk_size → 422."""
    app = _make_app(_admin_ctx())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/collections/",
            json={
                "name": "Bad Config",
                "embedding_model_id": str(MODEL_ID),
                "chunk_config": {"chunk_size": 100, "overlap": 100},
            },
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_collection_conflict_returns_409() -> None:
    """POST /collections with duplicate name → 409."""
    from src.core.exceptions import ConflictError

    app = _make_app(_admin_ctx())

    with patch("src.api.routers.collections._get_service") as mock_get_svc:
        mock_svc = AsyncMock()
        mock_get_svc.return_value = mock_svc
        mock_svc.create_collection.side_effect = ConflictError("already exists")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/collections/",
                json={"name": "Dup", "embedding_model_id": str(MODEL_ID)},
            )

    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_list_collections_as_viewer_returns_200_filtered() -> None:
    """GET /collections as Viewer → 200, filtered to allowed_collection_ids."""
    app = _make_app(_viewer_ctx())
    list_resp = CollectionListResponse(
        items=[_make_collection_response()],
        total=1,
        page=1,
        page_size=20,
    )

    with patch("src.api.routers.collections._get_service") as mock_get_svc:
        mock_svc = AsyncMock()
        mock_get_svc.return_value = mock_svc
        mock_svc.list_collections.return_value = list_resp

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/collections/")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert len(data["items"]) == 1


@pytest.mark.asyncio
async def test_list_collections_include_inactive_without_admin_returns_403() -> None:
    """GET /collections?include_inactive=true as Viewer → 403."""
    app = _make_app(_viewer_ctx())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/v1/collections/?include_inactive=true")

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_list_collections_include_inactive_as_admin_returns_200() -> None:
    """GET /collections?include_inactive=true as Admin → 200."""
    app = _make_app(_admin_ctx())
    list_resp = CollectionListResponse(items=[], total=0, page=1, page_size=20)

    with patch("src.api.routers.collections._get_service") as mock_get_svc:
        mock_svc = AsyncMock()
        mock_get_svc.return_value = mock_svc
        mock_svc.list_collections.return_value = list_resp

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/collections/?include_inactive=true")

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_get_collection_in_allowed_ids_returns_200() -> None:
    """GET /collections/{id} within allowed_collection_ids → 200."""
    app = _make_app(_viewer_ctx())
    col_resp = _make_collection_response()

    with patch("src.api.routers.collections._get_service") as mock_get_svc:
        mock_svc = AsyncMock()
        mock_get_svc.return_value = mock_svc
        mock_svc.get_collection.return_value = col_resp

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(f"/api/v1/collections/{COLLECTION_ID}")

    assert resp.status_code == 200
    assert resp.json()["id"] == str(COLLECTION_ID)


@pytest.mark.asyncio
async def test_get_collection_not_in_allowed_ids_returns_403() -> None:
    """GET /collections/{id} for collection not in allowed_collection_ids → 403."""
    from src.core.exceptions import PermissionDeniedError

    other_id = uuid.uuid4()
    # Viewer with empty allowed_collection_ids
    ctx = _make_user_context(frozenset({"documents:read"}), allowed_ids=frozenset())
    app = _make_app(ctx)

    with patch("src.api.routers.collections._get_service") as mock_get_svc:
        mock_svc = AsyncMock()
        mock_get_svc.return_value = mock_svc
        mock_svc.get_collection.side_effect = PermissionDeniedError("Access denied")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(f"/api/v1/collections/{other_id}")

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_patch_collection_valid_returns_200() -> None:
    """PATCH /collections/{id} with valid fields → 200."""
    app = _make_app(_admin_ctx())
    col_resp = _make_collection_response(name="Updated")

    with patch("src.api.routers.collections._get_service") as mock_get_svc:
        mock_svc = AsyncMock()
        mock_get_svc.return_value = mock_svc
        mock_svc.update_collection.return_value = col_resp

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.patch(
                f"/api/v1/collections/{COLLECTION_ID}",
                json={"name": "Updated"},
            )

    assert resp.status_code == 200
    assert resp.json()["name"] == "Updated"


@pytest.mark.asyncio
async def test_patch_collection_as_viewer_returns_403() -> None:
    """PATCH /collections/{id} as Viewer → 403."""
    app = _make_app(_viewer_ctx())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.patch(
            f"/api/v1/collections/{COLLECTION_ID}",
            json={"name": "Nope"},
        )

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_delete_collection_as_admin_returns_204() -> None:
    """DELETE /collections/{id} as Admin → 204."""
    app = _make_app(_admin_ctx())

    with patch("src.api.routers.collections._get_service") as mock_get_svc:
        mock_svc = AsyncMock()
        mock_get_svc.return_value = mock_svc
        mock_svc.delete_collection.return_value = None

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.delete(f"/api/v1/collections/{COLLECTION_ID}")

    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_collection_as_viewer_returns_403() -> None:
    """DELETE /collections/{id} as Viewer → 403."""
    app = _make_app(_viewer_ctx())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.delete(f"/api/v1/collections/{COLLECTION_ID}")

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_get_collection_not_found_returns_404() -> None:
    """GET /collections/{id} for nonexistent collection → 404."""
    from src.core.exceptions import NotFoundError

    app = _make_app(_admin_ctx())

    with patch("src.api.routers.collections._get_service") as mock_get_svc:
        mock_svc = AsyncMock()
        mock_get_svc.return_value = mock_svc
        mock_svc.get_collection.side_effect = NotFoundError("not found")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(f"/api/v1/collections/{uuid.uuid4()}")

    assert resp.status_code == 404

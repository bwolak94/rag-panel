"""Security tests for tenant isolation enforcement.

Verifies that cross-tenant resource access is impossible via assert_tenant_owns_resource,
and that TenantIsolationError always maps to HTTP 403, never 404.

Mark: @pytest.mark.tenant_isolation
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.dependencies.auth import assert_tenant_owns_resource
from src.api.exception_handlers import register_exception_handlers
from src.core.exceptions import TenantIsolationError
from src.domain.auth import UserContext


def make_ctx(**kwargs: Any) -> UserContext:
    defaults: dict[str, Any] = {
        "user_id": uuid.uuid4(),
        "keycloak_sub": "kc-sub",
        "email": "u@example.com",
        "display_name": "User",
        "tenant_id": uuid.uuid4(),
        "roles": frozenset(),
        "permissions": frozenset(),
        "allowed_collection_ids": frozenset(),
        "writable_collection_ids": frozenset(),
    }
    defaults.update(kwargs)
    return UserContext(**defaults)


TENANT_A = uuid.uuid4()
TENANT_B = uuid.uuid4()
DOC_ID = uuid.uuid4()


@pytest.fixture
def isolation_test_app() -> FastAPI:
    """Minimal app with tenant isolation check on a document endpoint."""
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/documents/{doc_id}")
    def get_document(doc_id: uuid.UUID) -> dict[str, str]:
        # Simulate document belonging to TENANT_B
        ctx = make_ctx(tenant_id=TENANT_A)
        assert_tenant_owns_resource(TENANT_B, ctx)  # TENANT_A != TENANT_B → raises
        return {"id": str(doc_id)}

    return app


@pytest.mark.tenant_isolation
class TestTenantIsolation:
    def test_assert_tenant_owns_resource_raises_on_mismatch(self) -> None:
        """assert_tenant_owns_resource raises TenantIsolationError on tenant mismatch."""
        ctx = make_ctx(tenant_id=TENANT_A)
        with pytest.raises(TenantIsolationError):
            assert_tenant_owns_resource(TENANT_B, ctx)

    def test_assert_tenant_owns_resource_passes_on_match(self) -> None:
        """assert_tenant_owns_resource does not raise when tenant_ids match."""
        ctx = make_ctx(tenant_id=TENANT_A)
        assert_tenant_owns_resource(TENANT_A, ctx)  # should not raise

    def test_tenant_isolation_error_maps_to_403(self, isolation_test_app: FastAPI) -> None:
        """TenantIsolationError → HTTP 403 (not 404, to avoid cross-tenant resource leakage)."""
        client = TestClient(isolation_test_app, raise_server_exceptions=False)
        response = client.get(f"/documents/{DOC_ID}")
        assert response.status_code == 403

    def test_tenant_isolation_error_body_is_generic(self, isolation_test_app: FastAPI) -> None:
        """403 response body must not expose resource IDs or cross-tenant details."""
        client = TestClient(isolation_test_app, raise_server_exceptions=False)
        response = client.get(f"/documents/{DOC_ID}")
        data = response.json()
        assert data["detail"] == "Access denied"
        # Ensure resource ID or tenant UUID not leaked in response
        assert str(TENANT_B) not in response.text
        assert str(DOC_ID) not in response.text

    def test_tenant_a_cannot_access_tenant_b_document(self) -> None:
        """Tenant A user gets TenantIsolationError when accessing tenant B's document."""
        ctx_a = make_ctx(tenant_id=TENANT_A)
        tenant_b_resource_tenant_id = TENANT_B

        with pytest.raises(TenantIsolationError):
            assert_tenant_owns_resource(tenant_b_resource_tenant_id, ctx_a)

    def test_tenant_a_cannot_access_tenant_b_collections(self) -> None:
        """Tenant A user cannot access tenant B's collection (checked via can_read)."""
        collection_owned_by_b = uuid.uuid4()
        # ctx for tenant A — collection from B was never added to allowed_collection_ids
        ctx_a = make_ctx(
            tenant_id=TENANT_A,
            allowed_collection_ids=frozenset(),  # only B's collections allowed for B's users
        )
        assert not ctx_a.can_read_collection(collection_owned_by_b)

    def test_id_guessing_returns_403_not_404(self, isolation_test_app: FastAPI) -> None:
        """Guessing a resource ID from another tenant returns 403, not 404."""
        guessed_id = uuid.uuid4()
        client = TestClient(isolation_test_app, raise_server_exceptions=False)
        response = client.get(f"/documents/{guessed_id}")
        # 403 (tenant check fires), not 404 (which would confirm ID existence)
        assert response.status_code == 403
        assert response.status_code != 404


@pytest.mark.tenant_isolation
class TestTenantAPIIsolation:
    """TASK-004-specific tenant isolation tests.

    Verifies that tenant API endpoints enforce cross-tenant isolation,
    audit logs are written for write operations, and role changes
    invalidate the auth cache immediately.
    """

    @pytest.mark.asyncio
    async def test_tenant_a_user_cannot_get_tenant_b_via_api(self) -> None:
        """GET /tenants/{tenant_B_id} by tenant-A user → 403, not 404."""
        from unittest.mock import MagicMock

        from httpx import ASGITransport, AsyncClient
        from sqlalchemy.ext.asyncio import AsyncSession

        from src.api.dependencies.auth import get_current_ctx
        from src.core.database import get_db_session
        from src.main import create_app

        tenant_a_id = uuid.uuid4()
        tenant_b_id = uuid.uuid4()

        ctx_a = make_ctx(tenant_id=tenant_a_id)
        app = create_app()

        from collections.abc import AsyncGenerator

        async def _fake_session() -> AsyncGenerator[MagicMock, None]:
            yield MagicMock(spec=AsyncSession)

        app.dependency_overrides[get_current_ctx] = lambda: ctx_a
        app.dependency_overrides[get_db_session] = _fake_session

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get(f"/api/v1/tenants/{tenant_b_id}")

        assert resp.status_code == 403
        assert resp.json()["detail"] == "Access denied"

    @pytest.mark.asyncio
    async def test_role_change_invalidates_user_context_cache(self) -> None:
        """PUT .../role calls invalidate_user_context_cache for immediate revocation."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sqlalchemy.ext.asyncio import AsyncSession

        from src.api.schemas.tenant import AssignRoleRequest
        from src.domain.tenant_service import TenantService

        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()
        keycloak_sub = "kc-demoted-user"

        ctx = make_ctx(tenant_id=tenant_id)
        session = MagicMock(spec=AsyncSession)
        svc = TenantService(session)

        existing_member = {
            "user_id": user_id,
            "keycloak_sub": keycloak_sub,
            "email": "demoted@example.com",
            "display_name": "Demoted",
            "role": "admin",
            "joined_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        }
        updated_member = {**existing_member, "role": "viewer"}

        with (
            patch.object(svc._repo, "get_member_with_role",
                         AsyncMock(side_effect=[existing_member, updated_member])),
            patch.object(svc._repo, "assign_role", AsyncMock()),
            patch.object(svc._audit, "log", AsyncMock()),
            patch(
                "src.domain.tenant_service.invalidate_user_context_cache"
            ) as mock_invalidate,
        ):
            body = AssignRoleRequest(role="viewer")
            result = await svc.assign_role(tenant_id, user_id, body, ctx, ip=None)

        mock_invalidate.assert_called_once_with(keycloak_sub, tenant_id)
        assert result.role == "viewer"

    @pytest.mark.asyncio
    async def test_remove_user_cascades_user_roles(self) -> None:
        """DELETE .../users/{id} also deletes user_roles (no orphan permissions)."""
        from unittest.mock import AsyncMock, MagicMock

        from sqlalchemy.ext.asyncio import AsyncSession

        from src.db.repositories.tenant_repository import TenantRepository

        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()
        session = MagicMock(spec=AsyncSession)
        repo = TenantRepository(session)

        membership_mock = MagicMock()
        # First execute: find membership; Second execute: delete user_roles
        execute_results = [
            MagicMock(**{"scalar_one_or_none.return_value": membership_mock}),
            MagicMock(),  # delete result
        ]
        session.execute = AsyncMock(side_effect=execute_results)
        session.delete = AsyncMock()
        session.flush = AsyncMock()

        await repo.remove_user(tenant_id, user_id)

        # session.delete called for the membership
        session.delete.assert_called_once_with(membership_mock)
        # session.execute called twice: find membership + delete user_roles
        assert session.execute.call_count == 2
        # The second call is the sa_delete — verify it targets UserRole
        second_call_stmt = str(session.execute.call_args_list[1][0][0].compile())
        assert "user_roles" in second_call_stmt.lower()

    @pytest.mark.asyncio
    async def test_audit_log_written_for_tenant_create(self) -> None:
        """AuditService.log is called on tenant creation with no PII in details."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sqlalchemy.ext.asyncio import AsyncSession

        from src.api.schemas.tenant import TenantCreate
        from src.db.models.tenant import Tenant
        from src.domain.tenant_service import TenantService

        tenant_id = uuid.uuid4()
        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)

        tenant_obj = MagicMock(spec=Tenant)
        tenant_obj.id = tenant_id
        tenant_obj.name = "Clinic"
        tenant_obj.slug = "clinic"
        tenant_obj.status = "active"
        tenant_obj.settings = {}
        tenant_obj.created_at = now
        tenant_obj.updated_at = now

        session = MagicMock(spec=AsyncSession)
        svc = TenantService(session)

        ctx = make_ctx(tenant_id=tenant_id)
        body = TenantCreate(name="Clinic", slug="clinic")

        with (
            patch.object(svc._repo, "create", AsyncMock(return_value=tenant_obj)),
            patch("src.domain.tenant_service.create_tenant_bucket", AsyncMock()),
            patch.object(svc._audit, "log", AsyncMock()) as mock_log,
        ):
            await svc.create_tenant(body, ctx, ip="127.0.0.1")

        mock_log.assert_called_once()
        call_kwargs = mock_log.call_args.kwargs
        assert call_kwargs["action"] == "tenant.created"
        assert call_kwargs["resource_type"] == "tenant"
        # details must NOT contain email or display_name
        details = call_kwargs["details"]
        assert "email" not in details
        assert "display_name" not in details
        assert "slug" in details

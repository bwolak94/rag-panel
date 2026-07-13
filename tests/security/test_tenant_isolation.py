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

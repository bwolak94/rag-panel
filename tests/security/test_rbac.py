"""Security tests for role-based access control.

Tests permission guard behavior across different user roles.

Mark: @pytest.mark.auth
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import HTTPException

from src.api.dependencies.auth import (
    assert_tenant_owns_resource,
    require_collection_read,
    require_collection_write,
    require_permission,
)
from src.core.exceptions import PermissionDeniedError, TenantIsolationError
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


COLLECTION_ID = uuid.uuid4()
TENANT_A = uuid.uuid4()
TENANT_B = uuid.uuid4()


@pytest.mark.auth
class TestRBAC:
    @pytest.mark.asyncio
    async def test_viewer_lacks_upload_permission(self) -> None:
        """Viewer role without documents:upload → require_permission raises 403."""
        ctx = make_ctx(
            roles=frozenset({"viewer"}),
            permissions=frozenset({"documents:read"}),
        )
        check = require_permission("documents:upload")
        with pytest.raises(HTTPException) as exc_info:
            await check(ctx)  # type: ignore[call-arg]
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_contributor_can_upload(self) -> None:
        """Contributor with documents:upload → require_permission passes."""
        ctx = make_ctx(
            roles=frozenset({"contributor"}),
            permissions=frozenset({"documents:upload", "documents:read"}),
        )
        check = require_permission("documents:upload")
        await check(ctx)  # type: ignore[call-arg]

    @pytest.mark.asyncio
    async def test_admin_can_delete(self) -> None:
        """Admin with documents:delete → require_permission passes."""
        ctx = make_ctx(
            roles=frozenset({"admin"}),
            permissions=frozenset({"documents:upload", "documents:read", "documents:delete"}),
        )
        check = require_permission("documents:delete")
        await check(ctx)  # type: ignore[call-arg]

    @pytest.mark.asyncio
    async def test_owner_has_all_permissions(self) -> None:
        """Owner with all permissions → all permission guards pass."""
        all_perms = frozenset(
            {
                "documents:upload",
                "documents:read",
                "documents:delete",
                "admin:all",
                "collections:manage",
            }
        )
        ctx = make_ctx(roles=frozenset({"owner"}), permissions=all_perms)

        for perm in all_perms:
            check = require_permission(perm)
            await check(ctx)  # type: ignore[call-arg]

    def test_cross_tenant_token_rejected_by_assert_tenant(self) -> None:
        """Token for tenant A used against tenant B's resource → TenantIsolationError."""
        ctx = make_ctx(tenant_id=TENANT_A)
        with pytest.raises(TenantIsolationError):
            assert_tenant_owns_resource(TENANT_B, ctx)

    def test_same_tenant_passes_assert_tenant(self) -> None:
        """assert_tenant_owns_resource passes when tenant_id matches."""
        ctx = make_ctx(tenant_id=TENANT_A)
        assert_tenant_owns_resource(TENANT_A, ctx)  # should not raise

    def test_require_collection_read_passes(self) -> None:
        """User with read access to collection → require_collection_read passes."""
        ctx = make_ctx(allowed_collection_ids=frozenset({COLLECTION_ID}))
        require_collection_read(COLLECTION_ID, ctx)  # should not raise

    def test_require_collection_read_fails(self) -> None:
        """User without read access → PermissionDeniedError."""
        ctx = make_ctx(allowed_collection_ids=frozenset())
        with pytest.raises(PermissionDeniedError):
            require_collection_read(COLLECTION_ID, ctx)

    def test_require_collection_write_passes(self) -> None:
        """User with write access → require_collection_write passes."""
        ctx = make_ctx(
            allowed_collection_ids=frozenset({COLLECTION_ID}),
            writable_collection_ids=frozenset({COLLECTION_ID}),
        )
        require_collection_write(COLLECTION_ID, ctx)  # should not raise

    def test_require_collection_write_fails_for_read_only(self) -> None:
        """User with only read access → require_collection_write raises."""
        ctx = make_ctx(
            allowed_collection_ids=frozenset({COLLECTION_ID}),
            writable_collection_ids=frozenset(),  # read-only
        )
        with pytest.raises(PermissionDeniedError):
            require_collection_write(COLLECTION_ID, ctx)

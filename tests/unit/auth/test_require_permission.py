"""Unit tests for require_permission dependency factory."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from src.api.dependencies.auth import require_permission
from src.domain.auth import UserContext


def make_ctx(**kwargs: Any) -> UserContext:
    defaults: dict[str, Any] = {
        "user_id": uuid.uuid4(),
        "keycloak_sub": "kc-sub",
        "email": "u@example.com",
        "display_name": "User",
        "tenant_id": uuid.uuid4(),
        "roles": frozenset({"contributor"}),
        "permissions": frozenset({"documents:upload", "documents:read"}),
        "allowed_collection_ids": frozenset(),
        "writable_collection_ids": frozenset(),
    }
    defaults.update(kwargs)
    return UserContext(**defaults)


class TestRequirePermission:
    @pytest.mark.asyncio
    async def test_user_with_permission_passes(self) -> None:
        """User holding the required permission → dependency succeeds (no exception)."""
        ctx = make_ctx(permissions=frozenset({"documents:upload"}))
        check = require_permission("documents:upload")

        with patch("src.api.dependencies.auth.get_current_ctx", return_value=ctx):
            # Call the inner _check function directly with the ctx
            await check(ctx)  # type: ignore[call-arg]

    @pytest.mark.asyncio
    async def test_user_without_permission_raises_403(self) -> None:
        """User lacking the required permission → HTTP 403."""
        ctx = make_ctx(permissions=frozenset({"documents:read"}))
        check = require_permission("documents:upload")

        with pytest.raises(HTTPException) as exc_info:
            await check(ctx)  # type: ignore[call-arg]

        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == "Insufficient permissions"

    @pytest.mark.asyncio
    async def test_empty_permissions_raises_403(self) -> None:
        """User with empty permissions frozenset → 403 for any permission."""
        ctx = make_ctx(permissions=frozenset())
        check = require_permission("documents:upload")

        with pytest.raises(HTTPException) as exc_info:
            await check(ctx)  # type: ignore[call-arg]

        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_different_permission_codes_are_independent(self) -> None:
        """Permission guard is specific to the code passed to require_permission."""
        ctx = make_ctx(permissions=frozenset({"admin:all"}))

        check_upload = require_permission("documents:upload")
        check_admin = require_permission("admin:all")

        with pytest.raises(HTTPException):
            await check_upload(ctx)  # type: ignore[call-arg]

        # Should not raise
        await check_admin(ctx)  # type: ignore[call-arg]

"""Unit tests for collection access resolution in UserContext."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from src.domain.auth import UserContext


def make_ctx(**kwargs: Any) -> UserContext:
    defaults: dict[str, Any] = {
        "user_id": uuid.uuid4(),
        "keycloak_sub": "kc-sub",
        "email": "u@example.com",
        "display_name": "User",
        "tenant_id": uuid.uuid4(),
        "roles": frozenset({"contributor"}),
        "permissions": frozenset(),
        "allowed_collection_ids": frozenset(),
        "writable_collection_ids": frozenset(),
    }
    defaults.update(kwargs)
    return UserContext(**defaults)


COLLECTION_A = uuid.uuid4()
COLLECTION_B = uuid.uuid4()
COLLECTION_OTHER_TENANT = uuid.uuid4()


class TestCollectionAccess:
    def test_read_access_to_collection_a(self) -> None:
        """User with read access to A → allowed_collection_ids contains A."""
        ctx = make_ctx(
            allowed_collection_ids=frozenset({COLLECTION_A}),
            writable_collection_ids=frozenset(),
        )
        assert ctx.can_read_collection(COLLECTION_A)
        assert not ctx.can_write_collection(COLLECTION_A)

    def test_write_access_implies_read_access(self) -> None:
        """User with write access to B → both allowed and writable sets contain B."""
        ctx = make_ctx(
            allowed_collection_ids=frozenset({COLLECTION_B}),
            writable_collection_ids=frozenset({COLLECTION_B}),
        )
        assert ctx.can_read_collection(COLLECTION_B)
        assert ctx.can_write_collection(COLLECTION_B)

    def test_inactive_collection_not_in_allowed(self) -> None:
        """Inactive collections are excluded from the context (enforced at build time)."""
        # AuthRepository filters out inactive collections.
        # This test verifies UserContext correctly reflects what's provided.
        inactive_collection = uuid.uuid4()
        ctx = make_ctx(
            allowed_collection_ids=frozenset(),  # inactive excluded by repo
            writable_collection_ids=frozenset(),
        )
        assert not ctx.can_read_collection(inactive_collection)

    def test_collection_from_other_tenant_not_accessible(self) -> None:
        """Collection belonging to a different tenant must not be accessible."""
        # The repo filters by Collection.tenant_id == tenant_id.
        # UserContext reflects what the repo returned — cross-tenant collection not included.
        ctx = make_ctx(
            allowed_collection_ids=frozenset({COLLECTION_A}),
            writable_collection_ids=frozenset(),
        )
        assert not ctx.can_read_collection(COLLECTION_OTHER_TENANT)

    def test_user_with_no_collections(self) -> None:
        """User with no collection access → both sets empty."""
        ctx = make_ctx()
        assert not ctx.can_read_collection(COLLECTION_A)
        assert not ctx.can_write_collection(COLLECTION_A)

    def test_has_permission_method(self) -> None:
        """UserContext.has_permission returns True only for granted codes."""
        ctx = make_ctx(permissions=frozenset({"documents:read", "collections:list"}))
        assert ctx.has_permission("documents:read")
        assert ctx.has_permission("collections:list")
        assert not ctx.has_permission("documents:upload")
        assert not ctx.has_permission("admin:all")

    def test_user_context_is_frozen(self) -> None:
        """UserContext must be immutable (frozen dataclass)."""
        ctx = make_ctx()
        with pytest.raises((AttributeError, TypeError)):
            ctx.email = "hacked@example.com"  # type: ignore[misc]

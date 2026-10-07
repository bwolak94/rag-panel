"""Tests for ADR-020: public_collection_ids in UserContext and AuthRepository.

Covers:
  1. UserContext.public_collection_ids field (default, explicit value).
  2. UserContext.can_read_collection recognises public collections.
  3. AuthRepository._load_user_context populates public_collection_ids from DB.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.repositories.auth_repository import AuthRepository
from src.domain.auth import UserContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TENANT_ID = uuid.uuid4()
USER_ID = uuid.uuid4()
PUBLIC_COL = uuid.uuid4()
PRIVATE_COL = uuid.uuid4()


def make_ctx(**kwargs: Any) -> UserContext:
    defaults: dict[str, Any] = {
        "user_id": USER_ID,
        "keycloak_sub": "kc-sub",
        "email": "u@example.com",
        "display_name": "User",
        "tenant_id": TENANT_ID,
        "roles": frozenset({"viewer"}),
        "permissions": frozenset({"chat:query"}),
        "allowed_collection_ids": frozenset(),
        "writable_collection_ids": frozenset(),
    }
    defaults.update(kwargs)
    return UserContext(**defaults)


# ---------------------------------------------------------------------------
# UserContext.public_collection_ids field tests
# ---------------------------------------------------------------------------


class TestUserContextPublicCollectionIds:
    def test_default_is_empty_frozenset(self) -> None:
        """public_collection_ids defaults to frozenset() for backward-compat."""
        ctx = make_ctx()
        assert ctx.public_collection_ids == frozenset()

    def test_explicit_value_is_stored(self) -> None:
        """Explicit public_collection_ids value is preserved."""
        ctx = make_ctx(public_collection_ids=frozenset({PUBLIC_COL}))
        assert PUBLIC_COL in ctx.public_collection_ids

    def test_is_immutable_frozenset(self) -> None:
        """public_collection_ids is a frozenset (immutable)."""
        ctx = make_ctx(public_collection_ids=frozenset({PUBLIC_COL}))
        with pytest.raises((TypeError, AttributeError)):
            ctx.public_collection_ids.add(uuid.uuid4())  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# UserContext.can_read_collection with public collections
# ---------------------------------------------------------------------------


class TestCanReadCollectionPublic:
    def test_public_collection_is_readable_without_allowed_ids(self) -> None:
        """A public collection is readable even when allowed_collection_ids is empty."""
        ctx = make_ctx(
            allowed_collection_ids=frozenset(),
            public_collection_ids=frozenset({PUBLIC_COL}),
        )
        assert ctx.can_read_collection(PUBLIC_COL) is True

    def test_private_collection_in_allowed_ids_is_readable(self) -> None:
        """Private collection in allowed_collection_ids is still readable."""
        ctx = make_ctx(
            allowed_collection_ids=frozenset({PRIVATE_COL}),
            public_collection_ids=frozenset({PUBLIC_COL}),
        )
        assert ctx.can_read_collection(PRIVATE_COL) is True

    def test_unknown_collection_is_not_readable(self) -> None:
        """A collection not in allowed_collection_ids or public_collection_ids is denied."""
        unknown = uuid.uuid4()
        ctx = make_ctx(
            allowed_collection_ids=frozenset({PRIVATE_COL}),
            public_collection_ids=frozenset({PUBLIC_COL}),
        )
        assert ctx.can_read_collection(unknown) is False

    def test_no_collections_returns_false(self) -> None:
        """Empty allowed and public — nothing is readable."""
        ctx = make_ctx(
            allowed_collection_ids=frozenset(),
            public_collection_ids=frozenset(),
        )
        assert ctx.can_read_collection(PUBLIC_COL) is False


# ---------------------------------------------------------------------------
# AuthRepository._load_user_context populates public_collection_ids
# ---------------------------------------------------------------------------


def _make_user_db_row() -> MagicMock:
    user = MagicMock()
    user.id = USER_ID
    user.email = "u@example.com"
    user.display_name = "User"
    user.is_active = True
    return user


_SENTINEL = object()


def _make_db_result(
    scalars_return: Any = _SENTINEL,
    scalar_one_or_none_return: Any = _SENTINEL,
) -> MagicMock:
    """Create a mock SQLAlchemy result object."""
    result = MagicMock()
    if scalars_return is not _SENTINEL:
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = scalars_return
        result.scalars.return_value = scalars_mock
    if scalar_one_or_none_return is not _SENTINEL:
        result.scalar_one_or_none.return_value = scalar_one_or_none_return
    return result


class TestAuthRepositoryPublicCollectionIds:
    @pytest.mark.asyncio
    async def test_load_user_context_includes_public_collection_ids(self) -> None:
        """_load_user_context populates public_collection_ids from is_public collections."""
        session = MagicMock(spec=AsyncSession)

        user = _make_user_db_row()

        # Sequence of execute() returns:
        #   1. user membership query → user row
        #   2. flush (profile sync — skipped, email matches)
        #   3. permissions query → []
        #   4. roles query → []
        #   5. collection access query → []
        #   6. public collections query → [PUBLIC_COL]

        call_results = [
            _make_db_result(scalar_one_or_none_return=user),  # user membership
            _make_db_result(scalars_return=[]),  # permissions
            _make_db_result(scalars_return=[]),  # roles
            _make_db_result(scalars_return=[]),  # collection access
            _make_db_result(scalars_return=[PUBLIC_COL]),  # public collections
        ]
        session.execute = AsyncMock(side_effect=call_results)
        session.flush = AsyncMock()

        repo = AuthRepository(session)
        ctx = await repo._load_user_context(
            keycloak_sub="kc-sub",
            tenant_id=TENANT_ID,
            email="u@example.com",
            display_name="User",
        )

        assert ctx is not None
        assert PUBLIC_COL in ctx.public_collection_ids

    @pytest.mark.asyncio
    async def test_load_user_context_empty_when_no_public_collections(self) -> None:
        """public_collection_ids is empty when no public collections exist."""
        session = MagicMock(spec=AsyncSession)

        user = _make_user_db_row()

        call_results = [
            _make_db_result(scalar_one_or_none_return=user),  # user membership
            _make_db_result(scalars_return=[]),  # permissions
            _make_db_result(scalars_return=[]),  # roles
            _make_db_result(scalars_return=[]),  # collection access
            _make_db_result(scalars_return=[]),  # public collections — empty
        ]
        session.execute = AsyncMock(side_effect=call_results)
        session.flush = AsyncMock()

        repo = AuthRepository(session)
        ctx = await repo._load_user_context(
            keycloak_sub="kc-sub",
            tenant_id=TENANT_ID,
            email="u@example.com",
            display_name="User",
        )

        assert ctx is not None
        assert ctx.public_collection_ids == frozenset()

    @pytest.mark.asyncio
    async def test_load_user_context_returns_none_for_non_member(self) -> None:
        """When user is not a tenant member, returns None (public collections not fetched)."""
        session = MagicMock(spec=AsyncSession)

        call_results = [
            _make_db_result(scalar_one_or_none_return=None),  # user membership → not found
        ]
        session.execute = AsyncMock(side_effect=call_results)

        repo = AuthRepository(session)
        result = await repo._load_user_context(
            keycloak_sub="kc-nobody",
            tenant_id=TENANT_ID,
            email="nobody@example.com",
            display_name="Nobody",
        )

        assert result is None

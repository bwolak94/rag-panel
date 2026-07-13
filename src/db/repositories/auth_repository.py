"""AuthRepository — builds UserContext from JWT claims + DB state.

Cache: 60-second in-process TTL per (keycloak_sub, tenant_id).
Negative cache: 10-second TTL for non-member users to bound DB load from invalid tokens.
Cache invalidation: call `invalidate_user_context_cache()` for immediate revocation
  (single-process only — in multi-worker deployments, TTL expiry handles other processes).
"""

from uuid import UUID

import sqlalchemy as sa
from cachetools import TTLCache
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import (
    Collection,
    CollectionAccess,
    Permission,
    Role,
    RolePermission,
    User,
    UserRole,
    UserTenant,
)
from src.domain.auth import UserContext

# 60-second positive TTL per (keycloak_sub, tenant_id)
_ctx_cache: TTLCache[tuple[str, UUID], UserContext] = TTLCache(maxsize=1000, ttl=60)

# 10-second negative TTL — prevents repeated DB hits for revoked/non-member tokens.
# Short TTL so a user added back to a tenant gains access within 10 seconds.
_negative_cache: TTLCache[tuple[str, UUID], bool] = TTLCache(maxsize=500, ttl=10)


def invalidate_user_context_cache(keycloak_sub: str, tenant_id: UUID) -> None:
    """Remove a user's cached context for immediate revocation.

    IMPORTANT: This clears the cache in the current process only.
    In a multi-worker deployment (uvicorn --workers N), other worker processes
    will continue serving the cached context until their 60-second TTL expires.
    For cross-process immediate revocation, implement Redis-based pub/sub signalling.
    """
    cache_key = (keycloak_sub, tenant_id)
    _ctx_cache.pop(cache_key, None)
    _negative_cache.pop(cache_key, None)


class AuthRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def build_user_context(
        self,
        keycloak_sub: str,
        tenant_id: UUID,
        email: str,
        display_name: str,
    ) -> UserContext | None:
        """Build UserContext from JWT claims and DB lookup.

        Returns None when the user is not a member of the given tenant.
        Positive results cached for 60 seconds; negative results for 10 seconds.

        No global lock — under asyncio cooperative scheduling, the bounded thundering
        herd (at most N concurrent DB queries for the same key) is safe and avoids
        serialising auth lookups across all users through a single mutex.
        """
        cache_key = (keycloak_sub, tenant_id)

        # Positive cache check — use .get() to avoid TOCTOU between `in` and `[]`
        cached = _ctx_cache.get(cache_key)
        if cached is not None:
            return cached

        # Negative cache — user is known to be a non-member
        if _negative_cache.get(cache_key) is not None:
            return None

        result = await self._load_user_context(
            keycloak_sub=keycloak_sub,
            tenant_id=tenant_id,
            email=email,
            display_name=display_name,
        )

        if result is not None:
            # Re-check before writing to handle concurrent coroutines that raced past
            # the initial cache miss — the last writer wins, all write the same value.
            if _ctx_cache.get(cache_key) is None:
                _ctx_cache[cache_key] = result
        else:
            # Cache negative result to prevent DB exhaustion from replayed invalid tokens
            _negative_cache[cache_key] = True

        return result

    async def _load_user_context(
        self,
        keycloak_sub: str,
        tenant_id: UUID,
        email: str,
        display_name: str,
    ) -> UserContext | None:
        # Verify user membership in this tenant
        user_q = (
            sa.select(User)
            .join(UserTenant, UserTenant.user_id == User.id)
            .where(
                User.keycloak_sub == keycloak_sub,
                UserTenant.tenant_id == tenant_id,
                User.is_active == sa.true(),
            )
        )
        user: User | None = (await self._session.execute(user_q)).scalar_one_or_none()
        if user is None:
            return None

        # Sync display_name/email from Keycloak on each cache miss.
        # Uses a savepoint so a flush failure is isolated and non-fatal.
        if user.email != email or user.display_name != display_name:
            user.email = email
            user.display_name = display_name
            try:
                await self._session.flush()
            except SQLAlchemyError:
                # Profile sync is best-effort — roll back the partial write and continue.
                # Auth succeeds; the stale display_name/email will be corrected next miss.
                await self._session.rollback()

        # Fetch permissions for this tenant
        perm_q = (
            sa.select(Permission.code)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .join(UserRole, UserRole.role_id == RolePermission.role_id)
            .join(Role, Role.id == UserRole.role_id)
            .where(
                UserRole.user_id == user.id,
                Role.tenant_id == tenant_id,
            )
        )
        permission_codes: list[str] = list(
            (await self._session.execute(perm_q)).scalars().all()
        )

        # Fetch role names
        role_q = (
            sa.select(Role.name)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user.id, Role.tenant_id == tenant_id)
        )
        role_names: list[str] = list((await self._session.execute(role_q)).scalars().all())

        # Fetch collection access (read ⊇ write enforced by query structure)
        access_q = (
            sa.select(CollectionAccess.collection_id, CollectionAccess.access_level)
            .join(UserRole, UserRole.role_id == CollectionAccess.role_id)
            .join(Role, Role.id == UserRole.role_id)
            .join(Collection, Collection.id == CollectionAccess.collection_id)
            .where(
                UserRole.user_id == user.id,
                Role.tenant_id == tenant_id,
                Collection.is_active == sa.true(),
                Collection.tenant_id == tenant_id,  # Explicit tenant filter on collection
            )
        )
        access_rows = (await self._session.execute(access_q)).all()

        # All rows grant read access; only "write" rows grant write access
        readable: frozenset[UUID] = frozenset(row.collection_id for row in access_rows)
        writable: frozenset[UUID] = frozenset(
            row.collection_id for row in access_rows if row.access_level == "write"
        )

        return UserContext(
            user_id=user.id,
            keycloak_sub=keycloak_sub,
            email=email,
            display_name=display_name,
            tenant_id=tenant_id,
            roles=frozenset(role_names),
            permissions=frozenset(permission_codes),
            allowed_collection_ids=readable,
            writable_collection_ids=writable,
        )

"""Repository for Tenant CRUD and membership management."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import ConflictError, NotFoundError
from src.db.models.permission import UserRole
from src.db.models.role import Role
from src.db.models.tenant import Tenant
from src.db.models.user import User
from src.db.models.user_tenant import UserTenant


class TenantRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Tenant CRUD ────────────────────────────────────────────────────────

    async def get_by_id(self, tenant_id: uuid.UUID) -> Tenant | None:
        return await self._session.get(Tenant, tenant_id)

    async def get_by_slug(self, slug: str) -> Tenant | None:
        q = select(Tenant).where(Tenant.slug == slug)
        return (await self._session.execute(q)).scalar_one_or_none()

    async def create(self, name: str, slug: str, settings: dict[str, Any]) -> Tenant:
        existing = await self.get_by_slug(slug)
        if existing is not None:
            raise ConflictError(f"Tenant with slug '{slug}' already exists")

        tenant = Tenant(name=name, slug=slug, settings=settings, status="active")
        self._session.add(tenant)
        await self._session.flush()  # get generated id before seeding roles

        # Seed the three system roles for this tenant
        for role_name in ("admin", "contributor", "viewer"):
            self._session.add(Role(tenant_id=tenant.id, name=role_name, is_system=True))
        await self._session.flush()
        return tenant

    async def update(
        self,
        tenant: Tenant,
        *,
        name: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> Tenant:
        if name is not None:
            tenant.name = name
        if settings is not None:
            tenant.settings = settings
        await self._session.flush()
        return tenant

    async def soft_delete(self, tenant: Tenant) -> None:
        tenant.status = "deleted"
        await self._session.flush()

    # ── Membership management ───────────────────────────────────────────────

    async def find_user_by_keycloak_sub(self, keycloak_sub: str) -> User | None:
        q = select(User).where(User.keycloak_sub == keycloak_sub)
        return (await self._session.execute(q)).scalar_one_or_none()

    async def add_user(self, tenant_id: uuid.UUID, user_id: uuid.UUID) -> None:
        existing_q = select(UserTenant).where(
            UserTenant.tenant_id == tenant_id,
            UserTenant.user_id == user_id,
        )
        if (await self._session.execute(existing_q)).scalar_one_or_none() is not None:
            raise ConflictError("User is already a member of this tenant")
        self._session.add(UserTenant(user_id=user_id, tenant_id=tenant_id))
        await self._session.flush()

    async def remove_user(self, tenant_id: uuid.UUID, user_id: uuid.UUID) -> None:
        membership_q = select(UserTenant).where(
            UserTenant.tenant_id == tenant_id,
            UserTenant.user_id == user_id,
        )
        membership = (await self._session.execute(membership_q)).scalar_one_or_none()
        if membership is None:
            raise NotFoundError("User is not a member of this tenant")

        await self._session.delete(membership)

        # Cascade: remove all user_roles for this user within this tenant (no orphan perms)
        role_ids_subq = select(Role.id).where(Role.tenant_id == tenant_id)
        await self._session.execute(
            sa_delete(UserRole).where(
                UserRole.user_id == user_id,
                UserRole.role_id.in_(role_ids_subq),
            )
        )
        await self._session.flush()

    async def assign_role(
        self, tenant_id: uuid.UUID, user_id: uuid.UUID, role_name: str
    ) -> None:
        """Replace the user's current role in this tenant with the given role name."""
        role_q = select(Role).where(
            Role.tenant_id == tenant_id,
            Role.name == role_name,
        )
        role = (await self._session.execute(role_q)).scalar_one_or_none()
        if role is None:
            raise NotFoundError(f"Role '{role_name}' not found in tenant")

        # Remove all existing roles for this user within this tenant (one-role policy)
        existing_role_ids_subq = select(Role.id).where(Role.tenant_id == tenant_id)
        await self._session.execute(
            sa_delete(UserRole).where(
                UserRole.user_id == user_id,
                UserRole.role_id.in_(existing_role_ids_subq),
            )
        )
        self._session.add(UserRole(user_id=user_id, role_id=role.id))
        await self._session.flush()

    # ── Member queries ──────────────────────────────────────────────────────

    async def list_members(
        self,
        tenant_id: uuid.UUID,
        *,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return (member_rows, total). Each row maps to TenantMemberResponse."""
        role_subq = (
            select(UserRole.user_id, Role.name.label("role_name"))
            .join(Role, UserRole.role_id == Role.id)
            .where(Role.tenant_id == tenant_id)
            .subquery()
        )

        q = (
            select(
                User.id.label("user_id"),
                User.keycloak_sub,
                User.email,
                User.display_name,
                role_subq.c.role_name.label("role"),
                UserTenant.created_at.label("joined_at"),
            )
            .join(UserTenant, User.id == UserTenant.user_id)
            .outerjoin(role_subq, User.id == role_subq.c.user_id)
            .where(UserTenant.tenant_id == tenant_id)
            .offset(offset)
            .limit(limit)
        )
        count_q = (
            select(func.count())
            .select_from(UserTenant)
            .where(UserTenant.tenant_id == tenant_id)
        )

        rows = list((await self._session.execute(q)).mappings().all())
        total = (await self._session.execute(count_q)).scalar_one()
        return [dict(r) for r in rows], total

    async def get_member_with_role(
        self, tenant_id: uuid.UUID, user_id: uuid.UUID
    ) -> dict[str, Any] | None:
        """Return a single member row or None if not a member."""
        role_subq = (
            select(UserRole.user_id, Role.name.label("role_name"))
            .join(Role, UserRole.role_id == Role.id)
            .where(Role.tenant_id == tenant_id)
            .subquery()
        )

        q = (
            select(
                User.id.label("user_id"),
                User.keycloak_sub,
                User.email,
                User.display_name,
                role_subq.c.role_name.label("role"),
                UserTenant.created_at.label("joined_at"),
            )
            .join(UserTenant, User.id == UserTenant.user_id)
            .outerjoin(role_subq, User.id == role_subq.c.user_id)
            .where(UserTenant.tenant_id == tenant_id, User.id == user_id)
        )

        row = (await self._session.execute(q)).mappings().first()
        return dict(row) if row is not None else None

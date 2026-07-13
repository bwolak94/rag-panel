"""TenantService — business logic for tenant lifecycle.

Routers call this; they do not call repositories directly.
Services do NOT call session.commit() — the router owns the transaction boundary.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas.tenant import (
    AddUserRequest,
    AssignRoleRequest,
    TenantCreate,
    TenantMemberResponse,
    TenantResponse,
    TenantUpdate,
)
from src.core.clients.minio_client import create_tenant_bucket
from src.core.exceptions import NotFoundError
from src.db.repositories.auth_repository import invalidate_user_context_cache
from src.db.repositories.tenant_repository import TenantRepository
from src.domain.audit_service import AuditService
from src.domain.auth import UserContext


class TenantService:
    def __init__(self, session: AsyncSession) -> None:
        self._repo = TenantRepository(session)
        self._audit = AuditService(session)

    async def create_tenant(
        self, body: TenantCreate, ctx: UserContext, ip: str | None
    ) -> TenantResponse:
        tenant = await self._repo.create(
            name=body.name,
            slug=body.slug,
            settings=body.settings.model_dump(),
        )
        # Provision MinIO bucket — idempotent, runs in executor (sync minio-py)
        await create_tenant_bucket(tenant.slug)

        await self._audit.log(
            ctx=ctx,
            action="tenant.created",
            resource_type="tenant",
            resource_id=tenant.id,
            details={"slug": tenant.slug},
            ip=ip,
        )
        return TenantResponse.model_validate(tenant)

    async def get_tenant(self, tenant_id: uuid.UUID) -> TenantResponse:
        tenant = await self._repo.get_by_id(tenant_id)
        if tenant is None:
            raise NotFoundError("Tenant not found")
        return TenantResponse.model_validate(tenant)

    async def update_tenant(
        self,
        tenant_id: uuid.UUID,
        body: TenantUpdate,
        ctx: UserContext,
        ip: str | None,
    ) -> TenantResponse:
        tenant = await self._repo.get_by_id(tenant_id)
        if tenant is None:
            raise NotFoundError("Tenant not found")

        tenant = await self._repo.update(
            tenant,
            name=body.name,
            settings=body.settings.model_dump() if body.settings is not None else None,
        )
        await self._audit.log(
            ctx=ctx,
            action="tenant.updated",
            resource_type="tenant",
            resource_id=tenant_id,
            details={"fields": list(body.model_fields_set)},
            ip=ip,
        )
        return TenantResponse.model_validate(tenant)

    async def delete_tenant(
        self, tenant_id: uuid.UUID, ctx: UserContext, ip: str | None
    ) -> None:
        tenant = await self._repo.get_by_id(tenant_id)
        if tenant is None:
            raise NotFoundError("Tenant not found")

        await self._repo.soft_delete(tenant)
        await self._audit.log(
            ctx=ctx,
            action="tenant.deleted",
            resource_type="tenant",
            resource_id=tenant_id,
            details={"slug": tenant.slug},
            ip=ip,
        )

    async def list_members(
        self,
        tenant_id: uuid.UUID,
        *,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[TenantMemberResponse], int]:
        rows, total = await self._repo.list_members(tenant_id, offset=offset, limit=limit)
        return [TenantMemberResponse.model_validate(r) for r in rows], total

    async def add_user(
        self,
        tenant_id: uuid.UUID,
        body: AddUserRequest,
        ctx: UserContext,
        ip: str | None,
    ) -> TenantMemberResponse:
        user = await self._repo.find_user_by_keycloak_sub(body.keycloak_sub)
        if user is None:
            raise NotFoundError(f"User with keycloak_sub '{body.keycloak_sub}' not found")

        await self._repo.add_user(tenant_id, user.id)

        await self._audit.log(
            ctx=ctx,
            action="admin.user_added",
            resource_type="user",
            resource_id=user.id,
            # details: IDs only — no PII (email/display_name excluded)
            details={"user_id": str(user.id), "tenant_id": str(tenant_id)},
            ip=ip,
        )

        member = await self._repo.get_member_with_role(tenant_id, user.id)
        assert member is not None
        return TenantMemberResponse.model_validate(member)

    async def remove_user(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        ctx: UserContext,
        ip: str | None,
    ) -> None:
        await self._repo.remove_user(tenant_id, user_id)
        await self._audit.log(
            ctx=ctx,
            action="admin.user_removed",
            resource_type="user",
            resource_id=user_id,
            details={"user_id": str(user_id), "tenant_id": str(tenant_id)},
            ip=ip,
        )

    async def assign_role(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        body: AssignRoleRequest,
        ctx: UserContext,
        ip: str | None,
    ) -> TenantMemberResponse:
        # Fetch member first (need keycloak_sub for cache invalidation)
        member = await self._repo.get_member_with_role(tenant_id, user_id)
        if member is None:
            raise NotFoundError("User not found in this tenant")

        await self._repo.assign_role(tenant_id, user_id, body.role)

        # Immediately invalidate the 60-second auth cache for this user
        invalidate_user_context_cache(member["keycloak_sub"], tenant_id)

        await self._audit.log(
            ctx=ctx,
            action="admin.role_change",
            resource_type="user",
            resource_id=user_id,
            details={"user_id": str(user_id), "new_role": body.role},
            ip=ip,
        )

        updated = await self._repo.get_member_with_role(tenant_id, user_id)
        assert updated is not None
        return TenantMemberResponse.model_validate(updated)

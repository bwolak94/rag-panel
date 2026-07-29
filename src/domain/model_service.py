"""ModelService — business logic for the Models Registry API.

Orchestrates ModelRepository and AuditService. All tenant isolation
enforcement is done here — never in the repository layer.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas.model import ModelCreate, ModelListResponse, ModelResponse, ModelUpdate
from src.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError
from src.db.models.models_registry import ModelsRegistry
from src.db.repositories.model_repository import ModelRepository
from src.db.repositories.pipeline_repository import PipelineRepository
from src.domain.audit_service import AuditService
from src.domain.auth import UserContext


class ModelService:
    """Service for Models Registry CRUD operations with tenant isolation."""

    def __init__(self, session: AsyncSession) -> None:
        self._repo = ModelRepository(session)
        self._pipeline_repo = PipelineRepository(session)
        self._audit = AuditService(session)

    async def list_models(
        self,
        ctx: UserContext,
        *,
        include_inactive: bool,
        offset: int,
        limit: int,
        page: int,
        page_size: int,
    ) -> ModelListResponse:
        """List models visible to the requesting tenant (system-wide + tenant-private).

        Args:
            ctx: Authenticated user context from JWT.
            include_inactive: When True, include models with is_active=False.
            offset: Number of records to skip.
            limit: Maximum number of records to return.
            page: Current page number for response metadata.
            page_size: Page size for response metadata.

        Returns:
            Paginated ModelListResponse.
        """
        items, total = await self._repo.list_for_tenant(
            ctx.tenant_id,
            include_inactive=include_inactive,
            offset=offset,
            limit=limit,
        )
        return ModelListResponse(
            items=[ModelResponse.model_validate(m) for m in items],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def create_model(
        self, body: ModelCreate, ctx: UserContext, ip: str | None
    ) -> ModelResponse:
        """Create a new tenant-private model entry.

        System-wide models (tenant_id=None) must be seeded via DB migrations.
        This endpoint always creates tenant-scoped models.

        Args:
            body: Validated creation payload.
            ctx: Authenticated user context from JWT.
            ip: Client IP address for audit log.

        Returns:
            The created ModelResponse.
        """
        model = ModelsRegistry(
            tenant_id=ctx.tenant_id,
            name=body.name,
            type=body.type,
            provider=body.provider,
            endpoint_url=body.endpoint_url,
            model_id=body.model_id,
            params=body.params,
            allowed_roles=body.allowed_roles,
        )
        model = await self._repo.create(model)

        await self._audit.log(
            ctx=ctx,
            action="model.created",
            resource_type="model",
            resource_id=model.id,
            details={"type": model.type, "provider": model.provider},
            ip=ip,
        )

        return ModelResponse.model_validate(model)

    async def get_model(self, model_id: uuid.UUID, ctx: UserContext) -> ModelResponse:
        """Fetch a single model visible to the requesting tenant.

        System-wide models (tenant_id=None) are visible to all tenants.
        Tenant-private models are only visible to their owning tenant.

        Args:
            model_id: The model UUID to retrieve.
            ctx: Authenticated user context from JWT.

        Returns:
            The ModelResponse.

        Raises:
            NotFoundError: If the model does not exist or is not visible to this tenant.
        """
        model = await self._repo.get_visible_by_id(model_id, ctx.tenant_id)
        if model is None:
            raise NotFoundError("Model not found")
        return ModelResponse.model_validate(model)

    async def update_model(
        self,
        model_id: uuid.UUID,
        body: ModelUpdate,
        ctx: UserContext,
        ip: str | None,
    ) -> ModelResponse:
        """Partially update a tenant-owned model.

        System-wide models (tenant_id=None) cannot be updated through this endpoint.
        Deactivating a model that has active pipelines raises ConflictError.

        Args:
            model_id: The model UUID to update.
            body: Partial update payload.
            ctx: Authenticated user context from JWT.
            ip: Client IP address for audit log.

        Returns:
            The updated ModelResponse.

        Raises:
            NotFoundError: If the model does not exist or is not visible to this tenant.
            PermissionDeniedError: If the model is system-wide (tenant_id=None).
            ConflictError: If trying to deactivate a model with active pipelines.
        """
        model = await self._repo.get_visible_by_id(model_id, ctx.tenant_id)
        if model is None:
            raise NotFoundError("Model not found")
        if model.tenant_id is None:
            raise PermissionDeniedError("System-wide models cannot be modified through this API")
        if model.tenant_id != ctx.tenant_id:
            raise NotFoundError("Model not found")

        # Guard: cannot deactivate if active pipelines reference this model
        if body.is_active is False:
            pipeline_count = await self._pipeline_repo.count_referencing_model(model_id)
            if pipeline_count > 0:
                raise ConflictError("Cannot deactivate model: active pipelines reference it")

        if body.name is not None:
            model.name = body.name
        if body.endpoint_url is not None:
            model.endpoint_url = body.endpoint_url
        if body.params is not None:
            model.params = body.params
        if body.allowed_roles is not None:
            model.allowed_roles = body.allowed_roles
        if body.is_active is not None:
            model.is_active = body.is_active

        await self._audit.log(
            ctx=ctx,
            action="model.updated",
            resource_type="model",
            resource_id=model_id,
            details={"fields": list(body.model_fields_set)},
            ip=ip,
        )

        return ModelResponse.model_validate(model)

    async def deactivate_model(
        self, model_id: uuid.UUID, ctx: UserContext, ip: str | None
    ) -> None:
        """Soft-delete a model by setting is_active=False.

        Cannot deactivate system-wide models or models from other tenants.
        Cannot deactivate if any active pipeline references this model.

        Args:
            model_id: The model UUID to deactivate.
            ctx: Authenticated user context from JWT.
            ip: Client IP address for audit log.

        Raises:
            NotFoundError: If the model does not exist or is not visible to this tenant.
            PermissionDeniedError: If the model is system-wide.
            ConflictError: If active pipelines reference this model.
        """
        model = await self._repo.get_visible_by_id(model_id, ctx.tenant_id)
        if model is None:
            raise NotFoundError("Model not found")
        if model.tenant_id is None:
            raise PermissionDeniedError("System-wide models cannot be deactivated through this API")
        if model.tenant_id != ctx.tenant_id:
            raise NotFoundError("Model not found")

        pipeline_count = await self._pipeline_repo.count_referencing_model(model_id)
        if pipeline_count > 0:
            raise ConflictError("Cannot deactivate model: active pipelines reference it")

        model.is_active = False

        await self._audit.log(
            ctx=ctx,
            action="model.deactivated",
            resource_type="model",
            resource_id=model_id,
            details={},
            ip=ip,
        )

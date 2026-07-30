"""ModelService — business logic for the Models Registry API.

Orchestrates ModelRepository and AuditService. All tenant isolation
enforcement is done here — never in the repository layer.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas.model import ModelCreate, ModelListResponse, ModelResponse, ModelUpdate
from src.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError
from src.db.models.models_registry import ModelsRegistry
from src.db.repositories.model_repository import ModelRepository
from src.db.repositories.pipeline_repository import PipelineRepository
from src.domain.audit_service import AuditService
from src.domain.auth import UserContext

if TYPE_CHECKING:
    from openai import AsyncOpenAI

logger = structlog.get_logger(__name__)


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

    async def deactivate_model(self, model_id: uuid.UUID, ctx: UserContext, ip: str | None) -> None:
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

    # ------------------------------------------------------------------
    # Client-builder methods
    # ------------------------------------------------------------------

    @staticmethod
    def _build_openai_client(record: ModelsRegistry) -> AsyncOpenAI:
        """Instantiate an OpenAI-compatible async client from a registry record.

        Args:
            record: The ModelsRegistry ORM instance.

        Returns:
            A configured AsyncOpenAI client pointing at record.endpoint_url.
        """
        from openai import AsyncOpenAI

        return AsyncOpenAI(
            base_url=record.endpoint_url,
            api_key=record.params.get("api_key", "ollama"),
            timeout=30.0,
        )

    async def get_llm_client(self, model_id: uuid.UUID, ctx: UserContext) -> AsyncOpenAI:
        """Return an OpenAI-compatible async client for an LLM model.

        Args:
            model_id: The model UUID.
            ctx: Authenticated user context from JWT.

        Returns:
            A configured AsyncOpenAI client.

        Raises:
            NotFoundError: If the model does not exist or is inactive.
            PermissionDeniedError: If the model type is not 'llm'.
        """
        record = await self._repo.get_visible_by_id(model_id, ctx.tenant_id)
        if record is None or not record.is_active:
            raise NotFoundError("Model not found")
        if record.type != "llm":
            raise PermissionDeniedError("Model is not of type 'llm'")
        return self._build_openai_client(record)

    async def get_embedding_client(self, model_id: uuid.UUID, ctx: UserContext) -> AsyncOpenAI:
        """Return an OpenAI-compatible async client for an embedding model.

        Args:
            model_id: The model UUID.
            ctx: Authenticated user context from JWT.

        Returns:
            A configured AsyncOpenAI client.

        Raises:
            NotFoundError: If the model does not exist or is inactive.
            PermissionDeniedError: If the model type is not 'embedding'.
        """
        record = await self._repo.get_visible_by_id(model_id, ctx.tenant_id)
        if record is None or not record.is_active:
            raise NotFoundError("Model not found")
        if record.type != "embedding":
            raise PermissionDeniedError("Model is not of type 'embedding'")
        return self._build_openai_client(record)

    async def update_score_threshold(
        self,
        model_id: uuid.UUID,
        threshold: float,
        sample_count: int,
    ) -> None:
        """Persist a calibrated score threshold on the model record.

        Called exclusively by ThresholdCalibrationService after a sweep. Uses
        the unscoped get_by_id path so system-wide models can also be calibrated
        without tenant ownership checks (calibration is an internal platform op).

        Args:
            model_id: The model UUID to update.
            threshold: Calibrated threshold in [0.0, 1.0] from F1-maximising sweep.
            sample_count: Number of EvalResult items used to derive the threshold.

        Raises:
            NotFoundError: If no model with model_id exists.
        """
        result = await self._repo.update_calibration(model_id, threshold, sample_count)
        if result is None:
            raise NotFoundError("Model not found")
        logger.info(
            "model.threshold_calibrated",
            model_id=str(model_id),
            threshold=threshold,
            sample_count=sample_count,
        )

    async def get_calibrated_threshold(self, model_id: uuid.UUID) -> float | None:
        """Return the last calibrated score threshold for a model.

        Returns None when no calibration run has been persisted yet, signalling
        the caller to fall back to the application or collection default.

        Args:
            model_id: The model UUID.

        Returns:
            Calibrated threshold float or None.
        """
        return await self._repo.get_calibrated_threshold(model_id)

    async def validate_model_reachable(self, model_id: uuid.UUID, ctx: UserContext) -> bool:
        """Check if the model endpoint responds to GET /models.

        Performs an HTTP GET to {endpoint_url}/models with a 5-second timeout.
        Returns False on any error (timeout, connection refused, non-2xx response).
        Never raises.

        Args:
            model_id: The model UUID.
            ctx: Authenticated user context from JWT.

        Returns:
            True if the endpoint returned a 2xx response, False otherwise.
        """
        import httpx

        record = await self._repo.get_visible_by_id(model_id, ctx.tenant_id)
        if record is None or not record.is_active:
            raise NotFoundError("Model not found")

        base_url = record.endpoint_url.rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
                resp = await client.get(f"{base_url}/models")
                return resp.is_success
        except Exception:
            logger.warning("model.validate_unreachable", model_id=str(model_id))
            return False

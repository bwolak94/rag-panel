"""PipelineService — business logic for the RAG Pipelines API.

Orchestrates PipelineRepository, ModelRepository, and AuditService.
All tenant isolation enforcement is done here — never in the repository layer.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas.pipeline import (
    PipelineCreate,
    PipelineListResponse,
    PipelineResponse,
    PipelineUpdate,
)
from src.core.exceptions import DomainValidationError, NotFoundError
from src.db.models.rag_pipeline import RagPipeline
from src.db.repositories.collection_repository import CollectionRepository
from src.db.repositories.model_repository import ModelRepository
from src.db.repositories.pipeline_repository import PipelineRepository
from src.domain.audit_service import AuditService
from src.domain.auth import UserContext


class PipelineService:
    """Service for RAG Pipeline CRUD operations with tenant isolation."""

    def __init__(self, session: AsyncSession) -> None:
        self._repo = PipelineRepository(session)
        self._model_repo = ModelRepository(session)
        self._collection_repo = CollectionRepository(session)
        self._audit = AuditService(session)

    async def list_pipelines(
        self,
        ctx: UserContext,
        *,
        include_inactive: bool,
        offset: int,
        limit: int,
        page: int,
        page_size: int,
    ) -> PipelineListResponse:
        """List pipelines belonging to the requesting tenant.

        Args:
            ctx: Authenticated user context from JWT.
            include_inactive: When True, include pipelines with is_active=False.
            offset: Number of records to skip.
            limit: Maximum number of records to return.
            page: Current page number for response metadata.
            page_size: Page size for response metadata.

        Returns:
            Paginated PipelineListResponse.
        """
        items, total = await self._repo.list_for_tenant(
            ctx.tenant_id,
            include_inactive=include_inactive,
            offset=offset,
            limit=limit,
        )
        return PipelineListResponse(
            items=[PipelineResponse.model_validate(p) for p in items],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def create_pipeline(
        self, body: PipelineCreate, ctx: UserContext, ip: str | None
    ) -> PipelineResponse:
        """Create a new RAG pipeline for the requesting tenant.

        Validates that the referenced LLM model exists, is of type 'llm', is active,
        and is visible to the requesting tenant (system-wide or tenant-private).

        Args:
            body: Validated creation payload.
            ctx: Authenticated user context from JWT.
            ip: Client IP address for audit log.

        Returns:
            The created PipelineResponse.

        Raises:
            DomainValidationError: If llm_model_id is invalid or not accessible.
        """
        await self._validate_llm_model(body.llm_model_id, ctx)
        await self._validate_collection_ids(body.collection_ids, ctx)

        pipeline = RagPipeline(
            tenant_id=ctx.tenant_id,
            name=body.name,
            collection_ids=body.collection_ids,
            llm_model_id=body.llm_model_id,
            prompt_config=body.prompt_config.model_dump(),
            guardrails=body.guardrails.model_dump(),
        )
        pipeline = await self._repo.create(pipeline)

        await self._audit.log(
            ctx=ctx,
            action="pipeline.created",
            resource_type="pipeline",
            resource_id=pipeline.id,
            details={"collection_count": len(body.collection_ids)},
            ip=ip,
        )

        return PipelineResponse.model_validate(pipeline)

    async def get_pipeline(self, pipeline_id: uuid.UUID, ctx: UserContext) -> PipelineResponse:
        """Fetch a single pipeline scoped to the requesting tenant.

        Args:
            pipeline_id: The pipeline UUID to retrieve.
            ctx: Authenticated user context from JWT.

        Returns:
            The PipelineResponse.

        Raises:
            NotFoundError: If the pipeline does not exist or belongs to another tenant.
        """
        pipeline = await self._repo.get_by_id(pipeline_id, ctx.tenant_id)
        if pipeline is None:
            raise NotFoundError(f"Pipeline {pipeline_id} not found")
        return PipelineResponse.model_validate(pipeline)

    async def update_pipeline(
        self,
        pipeline_id: uuid.UUID,
        body: PipelineUpdate,
        ctx: UserContext,
        ip: str | None,
    ) -> PipelineResponse:
        """Partially update a pipeline owned by the requesting tenant.

        If llm_model_id is included in the update, it is re-validated
        (must be type='llm', is_active=True, and visible to the tenant).

        Args:
            pipeline_id: The pipeline UUID to update.
            body: Partial update payload.
            ctx: Authenticated user context from JWT.
            ip: Client IP address for audit log.

        Returns:
            The updated PipelineResponse.

        Raises:
            NotFoundError: If the pipeline does not exist or belongs to another tenant.
            DomainValidationError: If the updated llm_model_id is invalid/inaccessible.
        """
        pipeline = await self._repo.get_by_id(pipeline_id, ctx.tenant_id)
        if pipeline is None:
            raise NotFoundError(f"Pipeline {pipeline_id} not found")

        if body.llm_model_id is not None:
            await self._validate_llm_model(body.llm_model_id, ctx)
        if body.collection_ids is not None:
            await self._validate_collection_ids(body.collection_ids, ctx)

        if body.name is not None:
            pipeline.name = body.name
        if body.collection_ids is not None:
            pipeline.collection_ids = body.collection_ids
        if body.llm_model_id is not None:
            pipeline.llm_model_id = body.llm_model_id
        if body.prompt_config is not None:
            pipeline.prompt_config = body.prompt_config.model_dump()
        if body.guardrails is not None:
            pipeline.guardrails = body.guardrails.model_dump()
        if body.is_active is not None:
            pipeline.is_active = body.is_active

        await self._audit.log(
            ctx=ctx,
            action="pipeline.updated",
            resource_type="pipeline",
            resource_id=pipeline_id,
            details={"fields": list(body.model_fields_set)},
            ip=ip,
        )

        return PipelineResponse.model_validate(pipeline)

    async def delete_pipeline(
        self, pipeline_id: uuid.UUID, ctx: UserContext, ip: str | None
    ) -> None:
        """Hard-delete a pipeline owned by the requesting tenant.

        Args:
            pipeline_id: The pipeline UUID to delete.
            ctx: Authenticated user context from JWT.
            ip: Client IP address for audit log.

        Raises:
            NotFoundError: If the pipeline does not exist or belongs to another tenant.
        """
        pipeline = await self._repo.get_by_id(pipeline_id, ctx.tenant_id)
        if pipeline is None:
            raise NotFoundError(f"Pipeline {pipeline_id} not found")

        await self._repo.delete(pipeline)

        await self._audit.log(
            ctx=ctx,
            action="pipeline.deleted",
            resource_type="pipeline",
            resource_id=pipeline_id,
            details={},
            ip=ip,
        )

    async def _validate_collection_ids(
        self, collection_ids: list[uuid.UUID], ctx: UserContext
    ) -> None:
        """Verify that all collection IDs belong to the requesting tenant.

        Security invariant: a pipeline must only reference collections owned by ctx.tenant_id.
        This prevents cross-tenant retrieval at query-graph execution time.

        Args:
            collection_ids: The list of collection UUIDs to validate.
            ctx: Authenticated user context from JWT.

        Raises:
            DomainValidationError: If any collection is not found for this tenant.
        """
        for cid in collection_ids:
            row = await self._collection_repo.get_by_id(cid, ctx.tenant_id)
            if row is None:
                raise DomainValidationError(
                    "One or more collection IDs are not accessible for this tenant"
                )

    async def _validate_llm_model(self, model_id: uuid.UUID, ctx: UserContext) -> None:
        """Validate that the model is a visible, active LLM.

        Args:
            model_id: The model UUID to validate.
            ctx: Authenticated user context for tenant visibility check.

        Raises:
            DomainValidationError: If the model is not found, not of type 'llm',
                not active, or not visible to the tenant.
        """
        model = await self._model_repo.get_visible_by_id(model_id, ctx.tenant_id)
        if model is None:
            raise DomainValidationError("The referenced LLM model is not available for this tenant")
        if model.type != "llm":
            raise DomainValidationError("The referenced LLM model is not available for this tenant")
        if not model.is_active:
            raise DomainValidationError("The referenced LLM model is not available for this tenant")

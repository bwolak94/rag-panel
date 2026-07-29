"""DeletionService — GDPR Art. 17 cascading deletion across Postgres, Qdrant, MinIO."""

from __future__ import annotations

import asyncio
import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clients.minio_client import get_minio_client
from src.core.exceptions import NotFoundError
from src.db.models.collection import Collection
from src.db.models.conversation import Conversation
from src.db.models.document import Document
from src.db.models.models_registry import ModelsRegistry
from src.db.repositories.tenant_repository import TenantRepository
from src.domain.audit_service import AuditService
from src.domain.auth import UserContext
from src.retrieval.schemas import TenantContext
from src.retrieval.service import RetrievalService

logger = structlog.get_logger(__name__)


def _model_name_to_slug(name: str) -> str:
    """Convert model name to Qdrant collection slug.

    Args:
        name: Model name from models_registry.name, e.g. "BGE-M3".

    Returns:
        Slug like "bge_m3" → Qdrant collection "emb_bge_m3".
    """
    return name.lower().replace("-", "_").replace(" ", "_")


class DeletionService:
    """Cascading hard-deletion across Postgres, Qdrant, and MinIO.

    Implements GDPR Art. 17 right-to-erasure. Ordering per security.md:
    1. Postgres row    (flush only — router commits)
    2. Qdrant vectors  (swallowed on any error — log WARNING)
    3. MinIO object    (swallowed on any error — log WARNING)
    4. Audit log entry
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)
        self._tenant_repo = TenantRepository(session)

    async def delete_document(
        self,
        *,
        document_id: uuid.UUID,
        ctx: UserContext,
        retrieval_svc: RetrievalService,
    ) -> None:
        """Hard-delete a document and all associated data (GDPR Art. 17).

        Steps (in order):
          1. Load document; 404 if not found, 403 if wrong tenant.
          2. Resolve Qdrant collection name via collection → embedding_model.
          3. Delete Qdrant vectors (swallow QdrantUnavailableError).
          4. Delete MinIO object (swallow errors).
          5. Hard-delete Postgres row (cascades to chunks_registry via FK).
          6. Write audit log.

        Args:
            document_id: UUID of the document to delete.
            ctx: Authenticated user context from JWT.
            retrieval_svc: RetrievalService instance for Qdrant operations.

        Raises:
            NotFoundError: If document does not exist or does not belong to this tenant.
        """
        # 1. Load document — SQL-scoped to tenant_id to prevent IDOR
        doc = (
            await self._session.execute(
                select(Document).where(
                    Document.id == document_id,
                    Document.tenant_id == ctx.tenant_id,
                )
            )
        ).scalar_one_or_none()

        if doc is None:
            raise NotFoundError(f"Document {document_id} not found")

        # Save values needed for out-of-band cleanup before Postgres row is gone
        minio_key = doc.minio_key
        collection_id = doc.collection_id

        # 2. Resolve Qdrant collection name (before Postgres delete so joins still work)
        qdrant_collection: str | None = None
        try:
            collection = (
                await self._session.execute(
                    select(Collection).where(Collection.id == collection_id)
                )
            ).scalar_one_or_none()

            if collection is not None:
                model = (
                    await self._session.execute(
                        select(ModelsRegistry).where(
                            ModelsRegistry.id == collection.embedding_model_id
                        )
                    )
                ).scalar_one_or_none()

                if model is not None:
                    slug = _model_name_to_slug(model.name)
                    qdrant_collection = f"emb_{slug}"
        except Exception as exc:
            logger.warning(
                "deletion_service.resolve_qdrant_collection_failed",
                document_id=str(document_id),
                error=str(exc),
            )

        # Resolve tenant slug for MinIO bucket before Postgres row is gone
        bucket: str | None = None
        try:
            tenant = await self._tenant_repo.get_by_id(ctx.tenant_id)
            if tenant is not None:
                bucket = f"tenant-{tenant.slug}"
            else:
                logger.warning(
                    "deletion_service.tenant_not_found_skip_minio",
                    document_id=str(document_id),
                )
        except Exception as exc:
            logger.warning(
                "deletion_service.resolve_tenant_failed",
                document_id=str(document_id),
                error=str(exc),
            )

        # 3. Hard-delete from Postgres first (chunks_registry cascades via FK)
        await self._session.delete(doc)
        await self._session.flush()

        logger.info(
            "deletion_service.document_hard_deleted",
            document_id=str(document_id),
            tenant_id=str(ctx.tenant_id),
        )

        # 4. Delete Qdrant vectors (best-effort — swallow all errors)
        if qdrant_collection is not None:
            try:
                tenant_ctx = TenantContext(
                    tenant_id=ctx.tenant_id,
                    allowed_collection_ids=[collection_id],
                )
                await retrieval_svc.delete_by_document(tenant_ctx, qdrant_collection, document_id)
                logger.info(
                    "deletion_service.qdrant_deleted",
                    document_id=str(document_id),
                    collection=qdrant_collection,
                )
            except Exception as exc:
                logger.warning(
                    "deletion_service.qdrant_delete_failed_swallowed",
                    document_id=str(document_id),
                    error=str(exc),
                )
        else:
            logger.warning(
                "deletion_service.qdrant_collection_not_resolved_skip",
                document_id=str(document_id),
            )

        # 5. Delete MinIO object (best-effort — swallow all errors)
        if bucket is not None:
            try:
                minio_client = get_minio_client()
                await asyncio.to_thread(minio_client.remove_object, bucket, minio_key)
                logger.info(
                    "deletion_service.minio_deleted",
                    document_id=str(document_id),
                    bucket=bucket,
                )
            except Exception as exc:
                logger.warning(
                    "deletion_service.minio_delete_failed_swallowed",
                    document_id=str(document_id),
                    error=str(exc),
                )

        # 6. Write audit log
        await self._audit.log(
            ctx=ctx,
            action="document.hard_deleted",
            resource_type="document",
            resource_id=document_id,
            details={"collection_id": str(collection_id)},
        )

    async def delete_conversation(
        self,
        *,
        conversation_id: uuid.UUID,
        user_id: uuid.UUID,
        ctx: UserContext,
    ) -> None:
        """Hard-delete a conversation and all associated messages (GDPR Art. 17).

        Uses NotFoundError (not TenantIsolationError) to avoid IDOR — leaking
        whether the conversation exists in another tenant is itself a violation.

        Args:
            conversation_id: UUID of the conversation to delete.
            user_id: Must match conversation.user_id.
            ctx: Authenticated user context from JWT.

        Raises:
            NotFoundError: If conversation not found, wrong tenant, or wrong user.
        """
        conv = (
            await self._session.execute(
                select(Conversation).where(
                    Conversation.id == conversation_id,
                    Conversation.tenant_id == ctx.tenant_id,
                )
            )
        ).scalar_one_or_none()

        if conv is None or conv.user_id != user_id:
            raise NotFoundError(f"Conversation {conversation_id} not found")

        await self._session.delete(conv)
        await self._session.flush()

        logger.info(
            "deletion_service.conversation_hard_deleted",
            conversation_id=str(conversation_id),
            tenant_id=str(ctx.tenant_id),
        )

        await self._audit.log(
            ctx=ctx,
            action="conversation.hard_deleted",
            resource_type="conversation",
            resource_id=conversation_id,
            details={},
        )

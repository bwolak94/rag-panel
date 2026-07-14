"""MinIO bucket notification webhook handler.

Parses ObjectCreated:Put events, verifies document in DB,
updates status to 'validating', and publishes to Redis Streams.

Security: bucket and key path are untrusted external input — every parse
step is guarded with try/except; failures are logged and skipped (no crash).
Logs MUST NOT contain document content, filenames with PII, or SHA-256 hashes.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clients.redis_client import get_redis_client
from src.db.repositories.document_repository import DocumentRepository
from src.db.repositories.tenant_repository import TenantRepository

logger = structlog.get_logger(__name__)


class WebhookHandler:
    STREAM_NAME = "ingest_events"

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def handle_minio_event(self, payload: dict[str, Any]) -> None:
        """
        Processes MinIO ObjectCreated:Put notification.
        Key path format: raw/{collection_id}/{document_id}/{filename}
        Bucket format: tenant-{slug}
        """
        for record in payload.get("Records", []):
            event_name: str = record.get("eventName", "")
            if "ObjectCreated" not in event_name:
                continue

            s3 = record.get("s3", {})
            bucket: str = s3.get("bucket", {}).get("name", "")
            key: str = s3.get("object", {}).get("key", "")
            size_bytes: int = s3.get("object", {}).get("size", 0)
            content_type: str = s3.get("object", {}).get("contentType", "")

            # Parse bucket → tenant slug → tenant_id
            if not bucket.startswith("tenant-"):
                logger.warning("webhook_unknown_bucket", bucket=bucket)
                continue
            slug = bucket.removeprefix("tenant-")

            tenant = await TenantRepository(self._session).get_by_slug(slug)
            if tenant is None:
                logger.error("webhook_tenant_not_found", slug=slug)
                continue

            # Parse key → collection_id, document_id
            # Expected: raw/{collection_id}/{document_id}/{filename}
            parts = key.split("/")
            if len(parts) < 4 or parts[0] != "raw":
                logger.warning("webhook_unexpected_key_format")
                continue

            try:
                collection_id = uuid.UUID(parts[1])
                document_id = uuid.UUID(parts[2])
            except ValueError:
                logger.error("webhook_invalid_ids_in_key")
                continue

            # Verify document exists in DB
            doc_repo = DocumentRepository(self._session)
            doc = await doc_repo.get_by_id(document_id, tenant.id)
            if doc is None:
                logger.error(
                    "webhook_document_not_found",
                    document_id=str(document_id),
                    tenant_id=str(tenant.id),
                )
                continue

            # Update document status to 'validating'
            await doc_repo.update_status(document_id, tenant.id, "validating")
            await self._session.commit()

            # Publish enriched event to Redis Streams
            event = {
                "schema_version": "1",
                "event_type": "document.uploaded",
                "tenant_id": str(tenant.id),
                "document_id": str(document_id),
                "collection_id": str(collection_id),
                "minio_bucket": bucket,
                "minio_key": key,
                "size_bytes": str(size_bytes),
                "content_type": content_type,
                "published_at": datetime.now(UTC).isoformat(),
            }
            redis = await get_redis_client()
            await redis.xadd(self.STREAM_NAME, event)
            logger.info(
                "ingest_event_published",
                document_id=str(document_id),
                tenant_id=str(tenant.id),
            )

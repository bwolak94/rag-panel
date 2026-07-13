"""MinIO client and bucket provisioning helpers.

All synchronous minio-py calls are wrapped in run_in_executor to avoid
blocking the asyncio event loop.
"""

from __future__ import annotations

import asyncio
from functools import partial

import structlog
from minio import Minio

from src.core.config import settings

logger = structlog.get_logger(__name__)


def get_minio_client() -> Minio:
    return Minio(
        settings.MINIO_ENDPOINT,
        access_key=settings.MINIO_ACCESS_KEY,
        secret_key=settings.MINIO_SECRET_KEY,
        secure=settings.MINIO_USE_TLS,
    )


def _create_bucket_sync(client: Minio, bucket_name: str) -> None:
    """Synchronous bucket creation — runs in executor."""
    if client.bucket_exists(bucket_name):
        logger.info("minio_bucket_already_exists", bucket=bucket_name)
        return
    client.make_bucket(bucket_name)
    logger.info("minio_bucket_created", bucket=bucket_name)

    # Enable object versioning for audit / accidental-delete recovery
    from minio.versioningconfig import VersioningConfig

    client.set_bucket_versioning(bucket_name, VersioningConfig("Enabled"))
    logger.info("minio_bucket_versioning_enabled", bucket=bucket_name)


async def create_tenant_bucket(slug: str) -> None:
    """Idempotently create the MinIO bucket for a tenant.

    Bucket name: tenant-{slug}
    Slug must match ^[a-z0-9-]+$ (validated at schema level) to ensure
    a valid S3 bucket name and prevent path traversal.
    """
    bucket_name = f"tenant-{slug}"
    client = get_minio_client()
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, partial(_create_bucket_sync, client, bucket_name))

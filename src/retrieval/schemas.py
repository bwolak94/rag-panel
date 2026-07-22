"""Retrieval service data transfer objects.

QdrantPoint and TenantContext are used by RetrievalService.upsert_batch()
and node_upsert to pass Qdrant point data without importing qdrant_client directly.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel


class TenantContext(BaseModel):
    """Tenant scope for all Qdrant operations.

    Always passed to RetrievalService; never read from request body.
    """

    tenant_id: UUID


class QdrantPoint(BaseModel):
    """Single Qdrant vector point ready for upsert.

    Payload must always include tenant_id to allow tenant-scoped queries.
    """

    id: UUID
    vector: list[float]
    payload: dict[str, Any]

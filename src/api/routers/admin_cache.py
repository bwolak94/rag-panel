"""Admin cache stats router.

GET /api/v1/admin/cache/stats — retrieval and response cache key counts for tenant.
DELETE /api/v1/admin/cache/invalidate/{collection_id} — force-invalidate a collection's cache.

Security:
- Requires admin:analytics permission.
- tenant_id always from JWT — admins cannot see other tenants' cache stats.
- Key counts only — no cached content is exposed.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies.auth import get_current_ctx, require_permission
from src.core.cache import _RESPONSE_PREFIX, _RETRIEVAL_PREFIX, get_rag_cache
from src.core.clients.redis_client import get_redis_client
from src.core.database import get_db_session
from src.domain.auth import UserContext

router = APIRouter(tags=["admin-cache"])

_Ctx = Annotated[UserContext, Depends(get_current_ctx)]
_Session = Annotated[AsyncSession, Depends(get_db_session)]
_RequireAdmin = Annotated[None, Depends(require_permission("admin:analytics"))]


class CacheStatsResponse(BaseModel):
    tenant_id: str
    retrieval_cache_keys: int
    response_cache_keys: int


class CacheInvalidateResponse(BaseModel):
    collection_id: str
    keys_deleted: int


@router.get(
    "/api/v1/admin/cache/stats",
    response_model=CacheStatsResponse,
    summary="Cache key counts for the tenant (retrieval + response)",
)
async def get_cache_stats(
    ctx: _Ctx,
    _auth: _RequireAdmin,
) -> Any:
    redis = get_redis_client()
    retrieval_pattern = f"{_RETRIEVAL_PREFIX}:{ctx.tenant_id}:*"
    response_pattern = f"{_RESPONSE_PREFIX}:{ctx.tenant_id}:*"

    retrieval_keys: list[bytes] = []
    response_keys: list[bytes] = []

    try:
        async for key in redis.scan_iter(retrieval_pattern):
            retrieval_keys.append(key)
        async for key in redis.scan_iter(response_pattern):
            response_keys.append(key)
    except Exception:
        pass  # degraded gracefully — return zeros on Redis error

    return CacheStatsResponse(
        tenant_id=str(ctx.tenant_id),
        retrieval_cache_keys=len(retrieval_keys),
        response_cache_keys=len(response_keys),
    )


@router.delete(
    "/api/v1/admin/cache/invalidate/{collection_id}",
    response_model=CacheInvalidateResponse,
    summary="Force-invalidate retrieval cache for a collection",
)
async def invalidate_collection_cache(
    collection_id: uuid.UUID,
    ctx: _Ctx,
    _auth: _RequireAdmin,
) -> Any:
    cache = get_rag_cache()
    deleted = await cache.invalidate_collection(ctx.tenant_id, collection_id)
    return CacheInvalidateResponse(
        collection_id=str(collection_id),
        keys_deleted=deleted,
    )

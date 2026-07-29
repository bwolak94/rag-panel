"""TosCheckMiddleware — enforce ToS acceptance before processing API requests.

Security note on JWT parsing without verification:
  The tenant_id is extracted from the JWT payload WITHOUT verifying the signature.
  This is intentional: the middleware only uses the tenant_id to look up a Redis
  cache key. Full JWT verification (signature, exp, aud) still happens inside
  get_current_ctx() before any business logic runs. An attacker who forges a
  tenant_id in a JWT would still be rejected by the real auth layer.
"""

from __future__ import annotations

import json
import uuid
from base64 import b64decode

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from src.db.repositories.tos_repo import TosRepository

# Paths that are always allowed without ToS check
_EXCLUDED_PATHS: frozenset[str] = frozenset(
    {
        "/api/v1/terms",
        "/health",
        "/metrics",
    }
)

_EXCLUDED_PREFIXES: tuple[str, ...] = (
    "/api/v1/auth/",
    "/docs",
    "/openapi.json",
    "/redoc",
)


class TosCheckMiddleware(BaseHTTPMiddleware):
    """Starlette middleware that gates API access on ToS acceptance.

    Checks are Redis-first (O(1)) with a 60-second TTL. Falls back to a DB
    query only on cache miss, using a dedicated short-lived session.
    """

    def __init__(
        self,
        app: ASGIApp,
        redis: Redis,  # type: ignore[type-arg]
        session_factory: async_sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        super().__init__(app)
        self._redis = redis
        self._session_factory = session_factory

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path

        # Excluded exact paths
        if path in _EXCLUDED_PATHS:
            return await call_next(request)

        # Excluded path prefixes
        if any(path.startswith(p) for p in _EXCLUDED_PREFIXES):
            return await call_next(request)

        # ToS accept/status endpoints must pass through (otherwise bootstrapping is impossible)
        if path.endswith("/terms/accept") or path.endswith("/terms/status"):
            return await call_next(request)

        # Only enforce on /api/v1/ and /v1/ routes (chat completions use /v1/ prefix)
        if not path.startswith("/api/v1/") and not path.startswith("/v1/"):
            return await call_next(request)

        # Extract tenant_id from JWT payload WITHOUT signature verification
        tenant_id = _extract_tenant_id_from_jwt(request)
        if tenant_id is None:
            # No JWT or unparseable — delegate to auth layer
            return await call_next(request)

        # Redis check (hot path)
        cache_key = f"tos:tenant:{tenant_id}:accepted"
        cached = await self._redis.get(cache_key)

        if cached == b"1":
            return await call_next(request)

        if cached == b"0":
            return _tos_required_response(str(tenant_id))

        # Cache miss — query DB and repopulate cache
        async with self._session_factory() as session:
            repo = TosRepository(session)
            accepted = await repo.has_accepted_current_tos(tenant_id)

        await self._redis.set(cache_key, "1" if accepted else "0", ex=60)

        if not accepted:
            return _tos_required_response(str(tenant_id))

        return await call_next(request)


def _extract_tenant_id_from_jwt(request: Request) -> uuid.UUID | None:
    """Decode the JWT payload (no signature check) to retrieve tenant_id.

    Args:
        request: Incoming Starlette request.

    Returns:
        Parsed tenant_id UUID, or None when extraction fails for any reason.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:]
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload_b64 = parts[1]
        # JWT uses base64url without padding — re-add required padding
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(
            b64decode(payload_b64.replace("-", "+").replace("_", "/"))
        )
        raw = payload.get("tenant_id")
        if raw is None:
            return None
        return uuid.UUID(str(raw))
    except Exception:  # noqa: BLE001
        return None


def _tos_required_response(tenant_id: str) -> JSONResponse:
    """Build the 403 response body for tenants that have not accepted the ToS."""
    return JSONResponse(
        status_code=403,
        content={
            "type": "about:blank",
            "title": "Wymagana akceptacja warunkow korzystania",
            "status": 403,
            "detail": (
                "Wlasciciel Twojej organizacji musi zaakceptowac warunki korzystania "
                "przed kontynuacja pracy w systemie."
            ),
            "tos_required": True,
            "accept_url": f"/api/v1/tenants/{tenant_id}/terms/accept",
        },
    )

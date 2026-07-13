"""Async JWKS client with TTL cache and stale-on-failure fallback.

Fetches public keys from Keycloak's JWKS endpoint for RS256 JWT verification.
"""

from typing import Any

import httpx
import structlog
from cachetools import TTLCache
from jose import jwk, jwt
from jose.exceptions import JWTClaimsError, JWTError

from src.core.config import settings
from src.core.exceptions import AuthenticationError

logger = structlog.get_logger(__name__)

# 5-minute TTL, single-entry cache (one realm's JWKS per process)
_jwks_cache: TTLCache[str, list[dict[str, Any]]] = TTLCache(maxsize=1, ttl=300)


class _JwksState:
    """Mutable container for stale JWKS fallback — avoids module-level global statement."""

    last_good: list[dict[str, Any]] | None = None


_state = _JwksState()

_CACHE_KEY = "jwks"


async def get_public_keys() -> list[dict[str, Any]]:
    """Fetch and cache JWKS from Keycloak.

    Returns stale keys on network failure (prefer availability; tokens already issued
    remain valid and cannot be revoked via JWKS anyway).
    Timeouts: connect=3s, read=5s per architecture §16.
    """
    # Fast path — no lock needed; TTLCache reads are safe in asyncio
    cached = _jwks_cache.get(_CACHE_KEY)
    if cached is not None:
        return cached

    jwks_url = (
        f"{settings.KEYCLOAK_BASE_URL}/realms/{settings.KEYCLOAK_REALM}"
        "/protocol/openid-connect/certs"
    )
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=5.0)
        ) as client:
            response = await client.get(jwks_url)
            response.raise_for_status()
            keys: list[dict[str, Any]] = response.json()["keys"]
            _jwks_cache[_CACHE_KEY] = keys
            _state.last_good = keys
            return keys
    except (httpx.HTTPError, httpx.TimeoutException, OSError):
        # Network/connectivity failure — serve stale keys if available
        if _state.last_good is not None:
            logger.warning("jwks_fetch_failed_serving_stale")
            return _state.last_good
        raise


def verify_token(token: str, public_keys: list[dict[str, Any]]) -> dict[str, Any]:
    """Verify RS256 JWT signature and standard claims against JWKS.

    Tries each key in the JWKS set to support key rotation.
    Raises:
        JWTError: signature invalid, expired, wrong audience, missing required claim.
        AuthenticationError: unexpected error during key processing.
    """
    last_error: JWTError | None = None
    for key_data in public_keys:
        try:
            public_key = jwk.construct(key_data, algorithm="RS256")
            claims: dict[str, Any] = jwt.decode(
                token,
                public_key.to_dict(),
                algorithms=["RS256"],
                audience=settings.KEYCLOAK_AUDIENCE,
                options={"verify_exp": True, "verify_aud": True},
            )
            # python-jose 3.x does not reject tokens with missing aud when audience is
            # specified — enforce it explicitly so missing-aud tokens are always rejected.
            if "aud" not in claims:
                raise JWTClaimsError("Token missing required aud claim")
            return claims
        except JWTError as exc:
            last_error = exc
            continue
        except Exception as exc:
            logger.error("unexpected_jwks_error", error_type=type(exc).__name__)
            raise AuthenticationError("Token verification failed") from exc

    if last_error is not None:
        raise last_error
    raise AuthenticationError("No JWKS keys available")

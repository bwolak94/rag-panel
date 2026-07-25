"""Unit tests for get_current_ctx dependency.

JWT crypto is mocked out — tests cover the dependency's control flow:
claim extraction, tenant validation, DB lookup, and in-process caching.
"""

from __future__ import annotations

import time
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from jose.exceptions import ExpiredSignatureError, JWTError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies.auth import get_current_ctx
from src.db.repositories.auth_repository import _ctx_cache, _negative_cache
from src.domain.auth import UserContext

TENANT_ID = uuid.uuid4()
USER_ID = uuid.uuid4()
KEYCLOAK_SUB = "kc-" + str(uuid.uuid4())


def make_user_context(**kwargs: Any) -> UserContext:
    defaults: dict[str, Any] = {
        "user_id": USER_ID,
        "keycloak_sub": KEYCLOAK_SUB,
        "email": "test@example.com",
        "display_name": "Test User",
        "tenant_id": TENANT_ID,
        "roles": frozenset({"contributor"}),
        "permissions": frozenset({"documents:upload", "documents:read"}),
        "allowed_collection_ids": frozenset(),
        "writable_collection_ids": frozenset(),
    }
    defaults.update(kwargs)
    return UserContext(**defaults)


def make_valid_claims(**kwargs: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "sub": KEYCLOAK_SUB,
        "aud": "rag-api",
        "exp": int(time.time()) + 3600,
        "iat": int(time.time()),
        "email": "test@example.com",
        "name": "Test User",
        "tenant_id": str(TENANT_ID),
    }
    defaults.update(kwargs)
    return defaults


def make_creds(token: str = "fake.token.here") -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def make_session() -> AsyncSession:
    return MagicMock(spec=AsyncSession)


@pytest.fixture(autouse=True)
def clear_caches() -> None:
    _ctx_cache.clear()
    _negative_cache.clear()

    from src.core.clients import jwks as jwks_mod

    jwks_mod._jwks_cache.clear()
    jwks_mod._state.last_good = None


class TestGetCurrentCtx:
    @pytest.mark.asyncio
    async def test_valid_token_returns_user_context(self) -> None:
        """Valid JWT + DB membership → UserContext with correct fields."""
        claims = make_valid_claims()
        ctx = make_user_context()

        with (
            patch("src.api.dependencies.auth.get_public_keys", new_callable=AsyncMock) as mock_pk,
            patch("src.api.dependencies.auth.verify_token", return_value=claims),
            patch(
                "src.api.dependencies.auth.AuthRepository.build_user_context",
                new_callable=AsyncMock,
                return_value=ctx,
            ),
        ):
            mock_pk.return_value = [{"kty": "RSA"}]
            result = await get_current_ctx(credentials=make_creds(), session=make_session())

        assert result.user_id == USER_ID
        assert result.tenant_id == TENANT_ID
        assert result.email == "test@example.com"

    @pytest.mark.asyncio
    async def test_expired_token_raises_401(self) -> None:
        """JWT with expired exp → HTTP 401."""
        with (
            patch("src.api.dependencies.auth.get_public_keys", new_callable=AsyncMock) as mock_pk,
            patch(
                "src.api.dependencies.auth.verify_token",
                side_effect=ExpiredSignatureError("expired"),
            ),
        ):
            mock_pk.return_value = [{"kty": "RSA"}]
            with pytest.raises(HTTPException) as exc_info:
                await get_current_ctx(credentials=make_creds(), session=make_session())

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_wrong_audience_raises_401(self) -> None:
        """JWT with wrong aud claim → HTTP 401."""
        with (
            patch("src.api.dependencies.auth.get_public_keys", new_callable=AsyncMock) as mock_pk,
            patch(
                "src.api.dependencies.auth.verify_token",
                side_effect=JWTError("Invalid audience"),
            ),
        ):
            mock_pk.return_value = [{"kty": "RSA"}]
            with pytest.raises(HTTPException) as exc_info:
                await get_current_ctx(credentials=make_creds(), session=make_session())

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_missing_tenant_id_claim_raises_401(self) -> None:
        """JWT without tenant_id claim → HTTP 401."""
        claims = make_valid_claims()
        del claims["tenant_id"]

        with (
            patch("src.api.dependencies.auth.get_public_keys", new_callable=AsyncMock) as mock_pk,
            patch("src.api.dependencies.auth.verify_token", return_value=claims),
        ):
            mock_pk.return_value = [{"kty": "RSA"}]
            with pytest.raises(HTTPException) as exc_info:
                await get_current_ctx(credentials=make_creds(), session=make_session())

        assert exc_info.value.status_code == 401
        assert "tenant_id" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_invalid_tenant_id_uuid_raises_401(self) -> None:
        """JWT with non-UUID tenant_id → HTTP 401."""
        claims = make_valid_claims(tenant_id="not-a-uuid")

        with (
            patch("src.api.dependencies.auth.get_public_keys", new_callable=AsyncMock) as mock_pk,
            patch("src.api.dependencies.auth.verify_token", return_value=claims),
        ):
            mock_pk.return_value = [{"kty": "RSA"}]
            with pytest.raises(HTTPException) as exc_info:
                await get_current_ctx(credentials=make_creds(), session=make_session())

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_user_not_in_tenant_raises_403(self) -> None:
        """Valid JWT but user not in user_tenants → HTTP 403 'Access denied'."""
        claims = make_valid_claims()

        with (
            patch("src.api.dependencies.auth.get_public_keys", new_callable=AsyncMock) as mock_pk,
            patch("src.api.dependencies.auth.verify_token", return_value=claims),
            patch(
                "src.api.dependencies.auth.AuthRepository.build_user_context",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            mock_pk.return_value = [{"kty": "RSA"}]
            with pytest.raises(HTTPException) as exc_info:
                await get_current_ctx(credentials=make_creds(), session=make_session())

        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == "Access denied"

    @pytest.mark.asyncio
    async def test_inactive_user_raises_403(self) -> None:
        """is_active=False user → build_user_context returns None → HTTP 403."""
        claims = make_valid_claims()

        with (
            patch("src.api.dependencies.auth.get_public_keys", new_callable=AsyncMock) as mock_pk,
            patch("src.api.dependencies.auth.verify_token", return_value=claims),
            patch(
                "src.api.dependencies.auth.AuthRepository.build_user_context",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            mock_pk.return_value = [{"kty": "RSA"}]
            with pytest.raises(HTTPException) as exc_info:
                await get_current_ctx(credentials=make_creds(), session=make_session())

        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_wrong_rsa_key_raises_401(self) -> None:
        """RS256 token signed with wrong RSA key → HTTP 401."""
        with (
            patch("src.api.dependencies.auth.get_public_keys", new_callable=AsyncMock) as mock_pk,
            patch(
                "src.api.dependencies.auth.verify_token",
                side_effect=JWTError("Signature verification failed"),
            ),
        ):
            mock_pk.return_value = [{"kty": "RSA", "n": "wrong"}]
            with pytest.raises(HTTPException) as exc_info:
                await get_current_ctx(credentials=make_creds(), session=make_session())

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_jwks_unavailable_raises_503(self) -> None:
        """Keycloak JWKS unreachable with no stale keys → HTTP 503."""
        from src.core.exceptions import AuthenticationError

        with (
            patch(
                "src.api.dependencies.auth.get_public_keys",
                new_callable=AsyncMock,
                side_effect=AuthenticationError("No JWKS keys available"),
            ),pytest.raises(HTTPException) as exc_info
        ):
            await get_current_ctx(credentials=make_creds(), session=make_session())

        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_second_call_uses_positive_cache(self) -> None:
        """Second build_user_context call within 60s returns cached result (no DB query)."""
        ctx = make_user_context()
        cache_key = (KEYCLOAK_SUB, TENANT_ID)
        _ctx_cache[cache_key] = ctx  # Pre-populate cache

        claims = make_valid_claims()

        with (
            patch("src.api.dependencies.auth.get_public_keys", new_callable=AsyncMock) as mock_pk,
            patch("src.api.dependencies.auth.verify_token", return_value=claims),
            patch(
                "src.db.repositories.auth_repository.AuthRepository._load_user_context",
                new_callable=AsyncMock,
            ) as mock_load,
        ):
            mock_pk.return_value = [{"kty": "RSA"}]
            result = await get_current_ctx(credentials=make_creds(), session=make_session())

        mock_load.assert_not_called()
        assert result == ctx

    @pytest.mark.asyncio
    async def test_non_member_result_is_negative_cached(self) -> None:
        """Second call for non-member user hits negative cache, not DB."""
        cache_key = (KEYCLOAK_SUB, TENANT_ID)
        _negative_cache[cache_key] = True  # Pre-populate negative cache

        claims = make_valid_claims()

        with (
            patch("src.api.dependencies.auth.get_public_keys", new_callable=AsyncMock) as mock_pk,
            patch("src.api.dependencies.auth.verify_token", return_value=claims),
            patch(
                "src.db.repositories.auth_repository.AuthRepository._load_user_context",
                new_callable=AsyncMock,
            ) as mock_load,
        ):
            mock_pk.return_value = [{"kty": "RSA"}]
            with pytest.raises(HTTPException) as exc_info:
                await get_current_ctx(credentials=make_creds(), session=make_session())

        # DB should not be hit — negative cache served the result
        mock_load.assert_not_called()
        assert exc_info.value.status_code == 403

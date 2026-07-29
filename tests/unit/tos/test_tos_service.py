"""Unit tests for TosService.

All external dependencies (AsyncSession, Redis, TosRepository) are mocked.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.exceptions import DomainValidationError

TENANT_ID = uuid.uuid4()
USER_ID = uuid.uuid4()
TOS_VERSION_ID = uuid.uuid4()


def _make_active_version(version_id: uuid.UUID = TOS_VERSION_ID) -> MagicMock:
    v = MagicMock()
    v.id = version_id
    v.version = "1.0"
    v.content = "Terms content"
    v.summary = "Summary"
    v.status = "active"
    v.effective_date = datetime(2024, 1, 1, tzinfo=UTC)
    return v


def _make_acceptance() -> MagicMock:
    a = MagicMock()
    a.id = uuid.uuid4()
    a.tenant_id = TENANT_ID
    a.user_id = USER_ID
    a.tos_version_id = TOS_VERSION_ID
    a.accepted_at = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)
    a.ip_address = "192.168.1.42"
    a.user_agent = "pytest"
    return a


def _make_redis(*, cached: bytes | None = None) -> MagicMock:
    r = MagicMock()
    r.get = AsyncMock(return_value=cached)
    r.set = AsyncMock()
    r.delete = AsyncMock()
    return r


def _make_session() -> MagicMock:
    s = MagicMock()
    s.add = MagicMock()
    s.flush = AsyncMock()
    return s


# ── 1. get_current_tos returns active version ─────────────────────────────


@pytest.mark.asyncio
async def test_get_current_tos_returns_active_version() -> None:
    active = _make_active_version()
    session = _make_session()
    redis = _make_redis()

    with patch("src.domain.tos_service.TosRepository") as mock_repo:
        repo_instance = mock_repo.return_value
        repo_instance.get_active_version = AsyncMock(return_value=active)

        from src.domain.tos_service import TosService

        svc = TosService(session, redis)
        result = await svc.get_current_tos()

    assert result is active


# ── 2. check_tenant_acceptance returns True when Redis has "1" ────────────


@pytest.mark.asyncio
async def test_check_tenant_acceptance_returns_true_from_cache() -> None:
    session = _make_session()
    redis = _make_redis(cached=b"1")

    with patch("src.domain.tos_service.TosRepository") as mock_repo:
        repo_instance = mock_repo.return_value
        repo_instance.has_accepted_current_tos = AsyncMock(return_value=False)

        from src.domain.tos_service import TosService

        svc = TosService(session, redis)
        result = await svc.check_tenant_acceptance(TENANT_ID)

    assert result is True
    # DB should NOT be queried when cache hits
    repo_instance.has_accepted_current_tos.assert_not_called()


# ── 3. check_tenant_acceptance queries DB on cache miss and caches result ─


@pytest.mark.asyncio
async def test_check_tenant_acceptance_queries_db_on_cache_miss() -> None:
    session = _make_session()
    redis = _make_redis(cached=None)

    with patch("src.domain.tos_service.TosRepository") as mock_repo:
        repo_instance = mock_repo.return_value
        repo_instance.has_accepted_current_tos = AsyncMock(return_value=True)

        from src.domain.tos_service import TosService

        svc = TosService(session, redis)
        result = await svc.check_tenant_acceptance(TENANT_ID)

    assert result is True
    repo_instance.has_accepted_current_tos.assert_called_once_with(TENANT_ID)
    redis.set.assert_called_once()
    # Value stored should be "1"
    call_args = redis.set.call_args
    assert call_args[0][1] == "1"


# ── 4. accept_tos creates acceptance record ───────────────────────────────


@pytest.mark.asyncio
async def test_accept_tos_creates_new_acceptance() -> None:
    active = _make_active_version()
    new_acceptance = _make_acceptance()
    session = _make_session()
    redis = _make_redis()

    with patch("src.domain.tos_service.TosRepository") as mock_repo:
        repo_instance = mock_repo.return_value
        repo_instance.get_active_version = AsyncMock(return_value=active)
        repo_instance.get_acceptance = AsyncMock(return_value=None)
        repo_instance.create_acceptance = AsyncMock(return_value=new_acceptance)

        from src.domain.tos_service import TosService

        svc = TosService(session, redis)
        result = await svc.accept_tos(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            tos_version_id=TOS_VERSION_ID,
            ip_address="10.0.0.1",
            user_agent="TestAgent",
            explicit_consent=True,
        )

    assert result is new_acceptance
    repo_instance.create_acceptance.assert_called_once()


# ── 5. accept_tos is idempotent ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_accept_tos_is_idempotent() -> None:
    active = _make_active_version()
    existing = _make_acceptance()
    session = _make_session()
    redis = _make_redis()

    with patch("src.domain.tos_service.TosRepository") as mock_repo:
        repo_instance = mock_repo.return_value
        repo_instance.get_active_version = AsyncMock(return_value=active)
        repo_instance.get_acceptance = AsyncMock(return_value=existing)
        repo_instance.create_acceptance = AsyncMock()

        from src.domain.tos_service import TosService

        svc = TosService(session, redis)
        result = await svc.accept_tos(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            tos_version_id=TOS_VERSION_ID,
            ip_address="10.0.0.1",
            user_agent=None,
            explicit_consent=True,
        )

    assert result is existing
    repo_instance.create_acceptance.assert_not_called()


# ── 6. accept_tos raises DomainValidationError when explicit_consent=False ─


@pytest.mark.asyncio
async def test_accept_tos_raises_when_no_explicit_consent() -> None:
    session = _make_session()
    redis = _make_redis()

    with patch("src.domain.tos_service.TosRepository"):
        from src.domain.tos_service import TosService

        svc = TosService(session, redis)
        with pytest.raises(DomainValidationError, match="explicit_consent"):
            await svc.accept_tos(
                tenant_id=TENANT_ID,
                user_id=USER_ID,
                tos_version_id=TOS_VERSION_ID,
                ip_address="10.0.0.1",
                user_agent=None,
                explicit_consent=False,
            )


# ── 7. accept_tos raises DomainValidationError when version ID mismatches ─


@pytest.mark.asyncio
async def test_accept_tos_raises_when_version_id_mismatch() -> None:
    active = _make_active_version()  # id = TOS_VERSION_ID
    wrong_id = uuid.uuid4()
    session = _make_session()
    redis = _make_redis()

    with patch("src.domain.tos_service.TosRepository") as mock_repo:
        repo_instance = mock_repo.return_value
        repo_instance.get_active_version = AsyncMock(return_value=active)

        from src.domain.tos_service import TosService

        svc = TosService(session, redis)
        with pytest.raises(DomainValidationError, match="active version"):
            await svc.accept_tos(
                tenant_id=TENANT_ID,
                user_id=USER_ID,
                tos_version_id=wrong_id,
                ip_address="10.0.0.1",
                user_agent=None,
                explicit_consent=True,
            )


# ── 8. accept_tos invalidates Redis cache ─────────────────────────────────


@pytest.mark.asyncio
async def test_accept_tos_invalidates_redis_cache() -> None:
    active = _make_active_version()
    new_acceptance = _make_acceptance()
    session = _make_session()
    redis = _make_redis()

    with patch("src.domain.tos_service.TosRepository") as mock_repo:
        repo_instance = mock_repo.return_value
        repo_instance.get_active_version = AsyncMock(return_value=active)
        repo_instance.get_acceptance = AsyncMock(return_value=None)
        repo_instance.create_acceptance = AsyncMock(return_value=new_acceptance)

        from src.domain.tos_service import TosService

        svc = TosService(session, redis)
        await svc.accept_tos(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            tos_version_id=TOS_VERSION_ID,
            ip_address="10.0.0.1",
            user_agent=None,
            explicit_consent=True,
        )

    redis.delete.assert_called_once_with(f"tos:tenant:{TENANT_ID}:accepted")


# ── 9. check_tenant_acceptance returns False for new tenant ──────────────


@pytest.mark.asyncio
async def test_check_tenant_acceptance_returns_false_for_new_tenant() -> None:
    session = _make_session()
    redis = _make_redis(cached=None)

    with patch("src.domain.tos_service.TosRepository") as mock_repo:
        repo_instance = mock_repo.return_value
        repo_instance.has_accepted_current_tos = AsyncMock(return_value=False)

        from src.domain.tos_service import TosService

        svc = TosService(session, redis)
        result = await svc.check_tenant_acceptance(TENANT_ID)

    assert result is False
    call_args = redis.set.call_args
    assert call_args[0][1] == "0"


# ── 10. get_tos_status returns is_accepted=True when accepted ─────────────


@pytest.mark.asyncio
async def test_get_tos_status_returns_accepted() -> None:
    active = _make_active_version()
    acceptance = _make_acceptance()
    session = _make_session()
    redis = _make_redis()

    with patch("src.domain.tos_service.TosRepository") as mock_repo:
        repo_instance = mock_repo.return_value
        repo_instance.get_active_version = AsyncMock(return_value=active)
        repo_instance.has_accepted_current_tos = AsyncMock(return_value=True)
        repo_instance.get_acceptance = AsyncMock(return_value=acceptance)

        from src.domain.tos_service import TosService

        svc = TosService(session, redis)
        result = await svc.get_tos_status(TENANT_ID)

    assert result["is_accepted"] is True
    assert result["current_tos_version"] == "1.0"
    assert result["requires_reacceptance"] is False
    assert result["accepted_at"] == acceptance.accepted_at

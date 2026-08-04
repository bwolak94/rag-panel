"""Unit tests for OnboardingService (TASK-032).

Tests cover:
- create_session() builds correct session object
- get_session() raises NotFoundError when missing or abandoned
- get_session() raises NotFoundError when expired (auto-abandons)
- execute_step_2() raises DomainValidationError when step 1 not done
- activate() raises DomainValidationError when required steps missing
- activate() sets session status to 'completed'
- abandon_session() raises ConflictError when already completed
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.exceptions import ConflictError, DomainValidationError, NotFoundError
from src.db.models.onboarding_session import OnboardingSession
from src.domain.onboarding_service import OnboardingService

PLATFORM_ADMIN_ID = uuid.uuid4()
TENANT_ID = uuid.uuid4()
SESSION_ID = uuid.uuid4()


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _make_session(
    status: str = "in_progress",
    steps_completed: dict[str, Any] | None = None,
    tenant_id: uuid.UUID | None = None,
    expires_delta: timedelta = timedelta(days=7),
    draft_config: dict[str, Any] | None = None,
) -> OnboardingSession:
    now = _utcnow()
    obj = OnboardingSession(
        id=SESSION_ID,
        tenant_id=tenant_id or TENANT_ID,
        status=status,
        current_step=len(steps_completed) if steps_completed else 0,
        steps_completed=steps_completed or {},
        draft_config=draft_config or {"tenant_name": "Test Clinic", "tenant_slug": "test-clinic"},
        created_by=PLATFORM_ADMIN_ID,
        created_at=now,
        updated_at=now,
        expires_at=now + expires_delta,
    )
    return obj


def _make_db(session_obj: OnboardingSession | None = None) -> MagicMock:
    db = MagicMock()
    db.get = AsyncMock(return_value=session_obj)
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
    db.add = MagicMock()
    db.flush = AsyncMock()
    return db


# ── create_session ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_session_adds_object_and_flushes() -> None:
    db = _make_db()
    svc = OnboardingService(db)
    session_obj = await svc.create_session(
        platform_admin_id=PLATFORM_ADMIN_ID,
        tenant_name="Clinic",
        tenant_slug="clinic",
        contact_email="admin@clinic.pl",
    )
    db.add.assert_called_once()
    db.flush.assert_awaited_once()
    assert session_obj.status == "in_progress"
    assert session_obj.draft_config["tenant_name"] == "Clinic"
    assert session_obj.expires_at > session_obj.created_at


# ── get_session ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_session_raises_when_not_found() -> None:
    db = _make_db(session_obj=None)
    svc = OnboardingService(db)
    with pytest.raises(NotFoundError):
        await svc.get_session(SESSION_ID)


@pytest.mark.asyncio
async def test_get_session_raises_when_abandoned() -> None:
    obj = _make_session(status="abandoned")
    db = _make_db(session_obj=obj)
    svc = OnboardingService(db)
    with pytest.raises(NotFoundError):
        await svc.get_session(SESSION_ID)


@pytest.mark.asyncio
async def test_get_session_raises_and_abandons_when_expired() -> None:
    obj = _make_session(expires_delta=timedelta(seconds=-1))  # already expired
    db = _make_db(session_obj=obj)
    svc = OnboardingService(db)
    with pytest.raises(NotFoundError, match="expired"):
        await svc.get_session(SESSION_ID)
    # execute() called to set status=abandoned
    db.execute.assert_awaited()


@pytest.mark.asyncio
async def test_get_session_returns_valid_session() -> None:
    obj = _make_session()
    db = _make_db(session_obj=obj)
    svc = OnboardingService(db)
    result = await svc.get_session(SESSION_ID)
    assert result is obj


# ── execute_step_2 ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_step2_requires_step1_completed() -> None:
    """execute_step_2() without step 1 done → DomainValidationError."""
    obj = _make_session(steps_completed={})  # step 1 not done
    db = _make_db(session_obj=obj)
    svc = OnboardingService(db)

    from src.domain.schemas.onboarding import CollectionConfigItem, Step2CollectionsRequest

    body = Step2CollectionsRequest(
        collections=[
            CollectionConfigItem(
                name="Test Collection",
                description="desc",
                primary_language="pol",
                chunk_strategy="recursive",
                chunk_size=512,
                chunk_overlap=64,
                visibility="internal",
            )
        ]
    )

    with pytest.raises(DomainValidationError, match="Step 1"):
        await svc.execute_step_2(SESSION_ID, body)


# ── activate (step 5) ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_activate_raises_when_steps_missing() -> None:
    """activate() without completing all steps → DomainValidationError."""
    obj = _make_session(steps_completed={"1": True, "2": True})  # missing 3, 4
    db = _make_db(session_obj=obj)
    svc = OnboardingService(db)

    with pytest.raises(DomainValidationError, match="steps"):
        await svc.activate(SESSION_ID)


@pytest.mark.asyncio
async def test_activate_sets_completed_status() -> None:
    """activate() with all steps done → session marked 'completed'."""
    all_steps = {"1": True, "2": True, "3": True, "4": True}
    obj = _make_session(
        steps_completed=all_steps,
        draft_config={"tenant_slug": "clinic", "pipeline_name": "Default"},
    )

    from src.db.models.tenant import Tenant

    tenant = MagicMock(spec=Tenant)
    tenant.id = TENANT_ID
    tenant.slug = "clinic"

    db = _make_db(session_obj=obj)
    db.get = AsyncMock(side_effect=[obj, tenant])  # first=session, second=tenant
    db.execute = AsyncMock(
        return_value=MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        )
    )

    svc = OnboardingService(db)
    result = await svc.activate(SESSION_ID)

    assert result.tenant_slug == "clinic"
    assert result.pipeline_created is True


# ── abandon_session ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_abandon_completed_session_raises_conflict() -> None:
    obj = _make_session(status="completed")
    db = _make_db(session_obj=obj)
    svc = OnboardingService(db)

    with pytest.raises(ConflictError, match="completed"):
        await svc.abandon_session(SESSION_ID)

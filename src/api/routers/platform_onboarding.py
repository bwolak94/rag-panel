"""Platform onboarding wizard router.

POST /api/v1/platform/onboarding/sessions                     → start new session
GET  /api/v1/platform/onboarding/sessions/{id}                → get session status
DELETE /api/v1/platform/onboarding/sessions/{id}              → abandon session
POST /api/v1/platform/onboarding/sessions/{id}/steps/1        → tenant config
POST /api/v1/platform/onboarding/sessions/{id}/steps/2        → collections
POST /api/v1/platform/onboarding/sessions/{id}/steps/3        → admin user
POST /api/v1/platform/onboarding/sessions/{id}/steps/4        → pipeline config
POST /api/v1/platform/onboarding/sessions/{id}/steps/5        → activate

All endpoints require the 'platform:admin' role (super-admin, not tenant admin).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies.auth import get_current_ctx, require_realm_role
from src.api.schemas.onboarding import (
    ActivationResultResponse,
    SessionStatusResponse,
    StartSessionRequest,
    Step1TenantConfigRequest,
    Step2CollectionsRequest,
    Step3AdminUserRequest,
    Step4PipelineRequest,
    StepResultResponse,
)
from src.core.database import get_db_session
from src.domain.audit_service import AuditService
from src.domain.auth import UserContext
from src.domain.onboarding_service import OnboardingService

router = APIRouter(prefix="/api/v1/platform/onboarding", tags=["platform-onboarding"])


class CleanupResponse(BaseModel):
    sessions_abandoned: int


_RequirePlatformAdmin = Annotated[None, Depends(require_realm_role("platform:admin"))]
_Ctx = Annotated[UserContext, Depends(get_current_ctx)]
_Session = Annotated[AsyncSession, Depends(get_db_session)]


def _svc(session: AsyncSession, caller_id: uuid.UUID | None = None) -> OnboardingService:
    return OnboardingService(session, caller_id=caller_id)


@router.post(
    "/sessions",
    response_model=SessionStatusResponse,
    status_code=201,
    summary="Start a new tenant onboarding session",
)
async def start_session(
    body: StartSessionRequest,
    ctx: _Ctx,
    _: _RequirePlatformAdmin,
    session: _Session,
) -> SessionStatusResponse:
    svc = _svc(session, caller_id=ctx.user_id)
    obj = await svc.create_session(
        platform_admin_id=ctx.user_id,
        tenant_name=body.tenant_name,
        tenant_slug=body.tenant_slug,
        contact_email=body.contact_email,
    )
    await session.commit()
    return svc._session_to_status(obj)


@router.get(
    "/sessions/{session_id}",
    response_model=SessionStatusResponse,
    summary="Get onboarding session status",
)
async def get_session(
    session_id: uuid.UUID,
    ctx: _Ctx,
    _: _RequirePlatformAdmin,
    session: _Session,
) -> SessionStatusResponse:
    svc = _svc(session, caller_id=ctx.user_id)
    obj = await svc.get_session(session_id)
    return svc._session_to_status(obj)


@router.delete(
    "/sessions/{session_id}",
    status_code=204,
    summary="Abandon an onboarding session",
)
async def abandon_session(
    session_id: uuid.UUID,
    ctx: _Ctx,
    _: _RequirePlatformAdmin,
    session: _Session,
) -> None:
    await _svc(session, caller_id=ctx.user_id).abandon_session(session_id)
    await session.commit()


@router.post(
    "/sessions/{session_id}/steps/1",
    response_model=StepResultResponse,
    summary="Step 1 — configure tenant settings and quotas",
)
async def step_1_tenant_config(
    session_id: uuid.UUID,
    body: Step1TenantConfigRequest,
    ctx: _Ctx,
    _: _RequirePlatformAdmin,
    session: _Session,
) -> StepResultResponse:
    result = await _svc(session, caller_id=ctx.user_id).execute_step_1(session_id, body)
    await session.commit()
    return result


@router.post(
    "/sessions/{session_id}/steps/2",
    response_model=StepResultResponse,
    summary="Step 2 — create knowledge collections",
)
async def step_2_collections(
    session_id: uuid.UUID,
    body: Step2CollectionsRequest,
    ctx: _Ctx,
    _: _RequirePlatformAdmin,
    session: _Session,
) -> StepResultResponse:
    result = await _svc(session, caller_id=ctx.user_id).execute_step_2(session_id, body)
    await session.commit()
    return result


@router.post(
    "/sessions/{session_id}/steps/3",
    response_model=StepResultResponse,
    summary="Step 3 — assign initial admin user",
)
async def step_3_admin_user(
    session_id: uuid.UUID,
    body: Step3AdminUserRequest,
    ctx: _Ctx,
    _: _RequirePlatformAdmin,
    session: _Session,
) -> StepResultResponse:
    result = await _svc(session, caller_id=ctx.user_id).execute_step_3(session_id, body)
    await session.commit()
    return result


@router.post(
    "/sessions/{session_id}/steps/4",
    response_model=StepResultResponse,
    summary="Step 4 — configure default RAG pipeline",
)
async def step_4_pipeline(
    session_id: uuid.UUID,
    body: Step4PipelineRequest,
    ctx: _Ctx,
    _: _RequirePlatformAdmin,
    session: _Session,
) -> StepResultResponse:
    result = await _svc(session, caller_id=ctx.user_id).execute_step_4(session_id, body)
    await session.commit()
    return result


@router.post(
    "/sessions/{session_id}/steps/5",
    response_model=ActivationResultResponse,
    summary="Step 5 — confirm and activate the tenant",
)
async def step_5_activate(
    session_id: uuid.UUID,
    ctx: _Ctx,
    _: _RequirePlatformAdmin,
    session: _Session,
) -> ActivationResultResponse:
    result = await _svc(session, caller_id=ctx.user_id).activate(session_id)
    await AuditService(session).log(
        ctx=ctx,
        action="tenant.activated",
        resource_type="tenant",
        resource_id=result.tenant_id,
        details={
            "session_id": str(session_id),
            "tenant_slug": result.tenant_slug,
            "collections_created": result.collections_created,
        },
    )
    await session.commit()
    return result


@router.post(
    "/sessions/cleanup",
    response_model=CleanupResponse,
    summary="Abandon all expired onboarding sessions",
    description=(
        "Marks every in-progress session whose `expires_at` is in the past as `abandoned`. "
        "Intended to be called by a Kubernetes CronJob on a regular schedule."
    ),
)
async def cleanup_expired_sessions(
    _: _RequirePlatformAdmin,
    session: _Session,
) -> CleanupResponse:
    count = await _svc(session).cleanup_expired_sessions()
    await session.commit()
    return CleanupResponse(sessions_abandoned=count)

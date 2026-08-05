"""Integration tests for OnboardingService — 5-step tenant onboarding wizard.

Uses testcontainers to spin up Postgres 16. Tests are skipped automatically if
Docker is unavailable or testcontainers is not installed.

Mark: @pytest.mark.integration

Design notes:
- Each test gets its own AsyncSession that is rolled back after the test so
  tests remain isolated despite sharing the module-scoped engine.
- audit_log is a RANGE-partitioned table; a default partition is created so
  that any INSERT with any created_at timestamp succeeds.
- The OnboardingService.execute_step_2() requires at least one row in
  models_registry; a system-wide embedding model is seeded once at module
  level (tenant_id=NULL means "available to all tenants").
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

try:
    from testcontainers.core.container import DockerContainer  # type: ignore[import-untyped]
    from testcontainers.core.waiting_utils import wait_for_logs  # type: ignore[import-untyped]
except ImportError:
    pytest.skip("testcontainers not installed", allow_module_level=True)

try:
    import sqlalchemy  # noqa: F401
except ImportError:
    pytest.skip("sqlalchemy not installed", allow_module_level=True)

# Guard: skip if the Docker daemon is not reachable at module import time.
try:
    import docker  # type: ignore[import-untyped]

    docker.from_env().ping()
except Exception:
    pytest.skip("Docker daemon not available", allow_module_level=True)

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from src.core.exceptions import NotFoundError
from src.db.models.base import Base
from src.db.models.collection import Collection
from src.db.models.onboarding_session import OnboardingSession
from src.domain.onboarding_service import OnboardingService
from src.domain.schemas.onboarding import (
    CollectionConfigItem,
    Step1TenantConfigRequest,
    Step2CollectionsRequest,
    Step3AdminUserRequest,
    Step4PipelineRequest,
)

# ── Module-level constants ─────────────────────────────────────────────────────

PLATFORM_ADMIN_ID = uuid.uuid4()
EMBEDDING_MODEL_ID = uuid.uuid4()


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def pg_container() -> Any:
    """Start a Postgres 16 container for the test module."""
    container = DockerContainer("postgres:16-alpine")
    container.with_exposed_ports(5432)
    container.with_env("POSTGRES_USER", "testuser")
    container.with_env("POSTGRES_PASSWORD", "testpass")
    container.with_env("POSTGRES_DB", "testdb")
    container.start()
    wait_for_logs(container, "database system is ready to accept connections", timeout=30)
    yield container
    container.stop()


@pytest.fixture(scope="module")
def pg_dsn(pg_container: Any) -> str:
    host = pg_container.get_container_host_ip()
    port = pg_container.get_exposed_port(5432)
    return f"postgresql+asyncpg://testuser:testpass@{host}:{port}/testdb"


@pytest.fixture(scope="module")
async def pg_engine(pg_dsn: str) -> Any:
    """Create all tables and seed required reference data."""
    engine = create_async_engine(pg_dsn, echo=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # audit_log is RANGE-partitioned — create a default partition
        await conn.execute(
            text("CREATE TABLE IF NOT EXISTS audit_log_default PARTITION OF audit_log DEFAULT")
        )

        # Seed a system-wide embedding model (tenant_id NULL = available to all tenants).
        # OnboardingService.execute_step_2() requires at least one models_registry row.
        await conn.execute(
            text(
                "INSERT INTO models_registry "
                "(id, tenant_id, name, type, provider, endpoint_url, model_id, params, "
                " allowed_roles, is_active) "
                "VALUES (:id, NULL, 'bge-m3', 'embedding', 'ollama', "
                "        'http://ollama:11434', 'BAAI/bge-m3', '{}', '{}', true) "
                "ON CONFLICT DO NOTHING"
            ),
            {"id": EMBEDDING_MODEL_ID},
        )

        # Seed the platform admin user so step_3 can look him up or use him.
        await conn.execute(
            text(
                "INSERT INTO users (id, keycloak_sub, email, display_name, is_active) "
                "VALUES (:id, :kc_sub, :email, 'Platform Admin', true) "
                "ON CONFLICT DO NOTHING"
            ),
            {
                "id": PLATFORM_ADMIN_ID,
                "kc_sub": f"kc-admin-{PLATFORM_ADMIN_ID}",
                "email": f"admin-{PLATFORM_ADMIN_ID}@platform.test",
            },
        )

    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(pg_engine: Any) -> Any:
    """Yield a fresh AsyncSession; roll back all writes after the test."""
    async_session = sessionmaker(  # type: ignore[call-overload]
        pg_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with async_session() as session, session.begin():
        yield session
        await session.rollback()


# ── Helper factories ───────────────────────────────────────────────────────────


def _svc(session: AsyncSession, caller_id: uuid.UUID = PLATFORM_ADMIN_ID) -> OnboardingService:
    return OnboardingService(session, caller_id=caller_id)


def _step1_body(display_name: str = "Integration Clinic") -> Step1TenantConfigRequest:
    return Step1TenantConfigRequest(display_name=display_name, settings={})


def _step2_body(collection_name: str = "General") -> Step2CollectionsRequest:
    return Step2CollectionsRequest(
        collections=[
            CollectionConfigItem(
                name=collection_name,
                description="Integration test collection",
                primary_language="pol",
                chunk_strategy="recursive",
                chunk_size=512,
                chunk_overlap=64,
            )
        ]
    )


def _step3_body(unique_suffix: str = "") -> Step3AdminUserRequest:
    suffix = unique_suffix or str(uuid.uuid4())[:8]
    return Step3AdminUserRequest(
        keycloak_user_id=f"kc-admin-{suffix}",
        display_name="Clinic Admin",
        email=f"clinic-admin-{suffix}@test.pl",
        role="admin",
    )


def _step4_body() -> Step4PipelineRequest:
    return Step4PipelineRequest(
        pipeline_name="Default Pipeline",
        llm_model="mistral:7b",
        embedding_model="BAAI/bge-m3",
        top_k=8,
        score_threshold=0.7,
        cache_responses=False,
        guardrails_enabled=True,
    )


async def _unique_slug() -> str:
    """Generate a slug that cannot collide across tests."""
    return f"clinic-{uuid.uuid4().hex[:8]}"


# ── Tests: Happy path ─────────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_full_happy_path_5_steps(db_session: AsyncSession) -> None:
    """Create a session and complete all 5 steps; final status must be 'completed'."""
    slug = await _unique_slug()
    svc = _svc(db_session)

    # Step 0 — create session
    session_obj = await svc.create_session(
        platform_admin_id=PLATFORM_ADMIN_ID,
        tenant_name="Happy Path Clinic",
        tenant_slug=slug,
        contact_email="happy@clinic.pl",
    )
    session_id = session_obj.id
    assert session_obj.status == "in_progress"
    assert session_obj.current_step == 0

    # Step 1 — tenant configuration
    result1 = await svc.execute_step_1(session_id, _step1_body("Happy Path Clinic"))
    assert result1.step_completed == 1
    assert result1.next_step == 2

    # Step 2 — collections
    result2 = await svc.execute_step_2(session_id, _step2_body("Medical Records"))
    assert result2.step_completed == 2
    assert result2.next_step == 3

    # Step 3 — admin user
    result3 = await svc.execute_step_3(session_id, _step3_body())
    assert result3.step_completed == 3
    assert result3.next_step == 4

    # Step 4 — pipeline
    result4 = await svc.execute_step_4(session_id, _step4_body())
    assert result4.step_completed == 4
    assert result4.next_step == 5

    # Step 5 — activate
    activation = await svc.activate(session_id)
    assert activation.collections_created >= 1
    assert activation.pipeline_created is True
    assert activation.admin_user_assigned is True

    # Session status must be 'completed'
    refreshed = await db_session.get(OnboardingSession, session_id)
    assert refreshed is not None
    assert refreshed.status == "completed"
    assert refreshed.current_step == 5


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_session_status_after_step_1(db_session: AsyncSession) -> None:
    """_session_to_status() reflects progress correctly after step 1."""
    slug = await _unique_slug()
    svc = _svc(db_session)

    session_obj = await svc.create_session(
        platform_admin_id=PLATFORM_ADMIN_ID,
        tenant_name="Status Check Clinic",
        tenant_slug=slug,
        contact_email="status@clinic.pl",
    )
    await svc.execute_step_1(session_obj.id, _step1_body())

    # Re-fetch fresh state
    updated = await svc.get_session(session_obj.id)
    status = svc._session_to_status(updated)

    assert status.current_step == 1
    assert status.steps_completed.get("1") is True
    assert status.steps_completed.get("2") is not True


# ── Tests: Session expiry ──────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_expired_session_raises_not_found_on_get(db_session: AsyncSession) -> None:
    """Accessing a session past its expires_at auto-abandons it and raises NotFoundError."""
    now = datetime.now(UTC)
    expired_session = OnboardingSession(
        status="in_progress",
        current_step=0,
        steps_completed={},
        draft_config={
            "tenant_name": "Old Clinic",
            "tenant_slug": "old-clinic",
            "contact_email": "old@clinic.pl",
        },
        created_by=PLATFORM_ADMIN_ID,
        created_at=now - timedelta(days=10),
        updated_at=now - timedelta(days=10),
        expires_at=now - timedelta(days=3),  # already expired
    )
    db_session.add(expired_session)
    await db_session.flush()

    svc = _svc(db_session, caller_id=PLATFORM_ADMIN_ID)
    with pytest.raises(NotFoundError, match="expired"):
        await svc.get_session(expired_session.id)

    # The session must have been auto-abandoned in the DB
    refreshed = await db_session.get(OnboardingSession, expired_session.id)
    assert refreshed is not None
    assert refreshed.status == "abandoned"


# ── Tests: Idempotency ─────────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_step_2_idempotency_no_duplicate_collections(
    db_session: AsyncSession,
) -> None:
    """Re-running step 2 with the same collection name must not create a duplicate."""
    slug = await _unique_slug()
    svc = _svc(db_session)

    session_obj = await svc.create_session(
        platform_admin_id=PLATFORM_ADMIN_ID,
        tenant_name="Idempotency Clinic",
        tenant_slug=slug,
        contact_email="idempotent@clinic.pl",
    )
    session_id = session_obj.id
    await svc.execute_step_1(session_id, _step1_body("Idempotency Clinic"))

    collection_body = _step2_body("Protocols")

    # First execution
    await svc.execute_step_2(session_id, collection_body)

    # Second execution with the same name — must not raise, must not duplicate
    await svc.execute_step_2(session_id, collection_body)

    updated = await svc.get_session(session_id)
    tenant_id = updated.tenant_id
    assert tenant_id is not None

    count_q = select(Collection).where(
        Collection.tenant_id == tenant_id,
        Collection.name == "Protocols",
    )
    rows = (await db_session.execute(count_q)).scalars().all()
    assert len(rows) == 1, f"Expected exactly 1 collection named 'Protocols', found {len(rows)}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_step_1_idempotency_updates_tenant_name(
    db_session: AsyncSession,
) -> None:
    """Re-running step 1 with a new display_name must update the tenant, not create a new one."""
    slug = await _unique_slug()
    svc = _svc(db_session)

    session_obj = await svc.create_session(
        platform_admin_id=PLATFORM_ADMIN_ID,
        tenant_name="Initial Name",
        tenant_slug=slug,
        contact_email="update@clinic.pl",
    )
    session_id = session_obj.id

    await svc.execute_step_1(session_id, _step1_body("Initial Name"))
    await svc.execute_step_1(session_id, _step1_body("Updated Name"))

    updated_session = await svc.get_session(session_id)
    assert updated_session.tenant_id is not None

    from src.db.models.tenant import Tenant

    tenant = await db_session.get(Tenant, updated_session.tenant_id)
    assert tenant is not None
    assert tenant.name == "Updated Name"


# ── Tests: Abandon session ─────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_abandon_session_sets_status_to_abandoned(
    db_session: AsyncSession,
) -> None:
    """abandon_session() must set status to 'abandoned'; get_session() then raises."""
    slug = await _unique_slug()
    svc = _svc(db_session)

    session_obj = await svc.create_session(
        platform_admin_id=PLATFORM_ADMIN_ID,
        tenant_name="Abandoned Clinic",
        tenant_slug=slug,
        contact_email="abandon@clinic.pl",
    )
    session_id = session_obj.id

    await svc.abandon_session(session_id)

    # Raw DB check: status should be 'abandoned'
    refreshed = await db_session.get(OnboardingSession, session_id)
    assert refreshed is not None
    assert refreshed.status == "abandoned"

    # Service access now raises NotFoundError
    with pytest.raises(NotFoundError):
        await svc.get_session(session_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cannot_abandon_completed_session(db_session: AsyncSession) -> None:
    """abandon_session() on a completed session must raise ConflictError."""
    from src.core.exceptions import ConflictError

    slug = await _unique_slug()
    svc = _svc(db_session)

    session_obj = await svc.create_session(
        platform_admin_id=PLATFORM_ADMIN_ID,
        tenant_name="Completed Clinic",
        tenant_slug=slug,
        contact_email="completed@clinic.pl",
    )
    session_id = session_obj.id

    await svc.execute_step_1(session_id, _step1_body("Completed Clinic"))
    await svc.execute_step_2(session_id, _step2_body("Policies"))
    await svc.execute_step_3(session_id, _step3_body())
    await svc.execute_step_4(session_id, _step4_body())
    await svc.activate(session_id)

    with pytest.raises(ConflictError, match="completed"):
        await svc.abandon_session(session_id)


# ── Tests: Ownership check ─────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_different_admin_cannot_access_session(db_session: AsyncSession) -> None:
    """A platform admin must not access a session they did not create."""
    slug = await _unique_slug()
    owner_id = PLATFORM_ADMIN_ID
    other_admin_id = uuid.uuid4()

    svc_owner = _svc(db_session, caller_id=owner_id)
    session_obj = await svc_owner.create_session(
        platform_admin_id=owner_id,
        tenant_name="Private Clinic",
        tenant_slug=slug,
        contact_email="private@clinic.pl",
    )

    svc_other = _svc(db_session, caller_id=other_admin_id)
    with pytest.raises(NotFoundError):
        await svc_other.get_session(session_obj.id)


# ── Tests: Step ordering guard ─────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_step_2_fails_when_step_1_not_complete(db_session: AsyncSession) -> None:
    """execute_step_2() must raise DomainValidationError if step 1 was not done."""
    from src.core.exceptions import DomainValidationError

    slug = await _unique_slug()
    svc = _svc(db_session)

    session_obj = await svc.create_session(
        platform_admin_id=PLATFORM_ADMIN_ID,
        tenant_name="Out of Order Clinic",
        tenant_slug=slug,
        contact_email="order@clinic.pl",
    )

    with pytest.raises(DomainValidationError, match="Step 1"):
        await svc.execute_step_2(session_obj.id, _step2_body())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_activate_fails_when_steps_incomplete(db_session: AsyncSession) -> None:
    """activate() must raise DomainValidationError when required steps are missing."""
    from src.core.exceptions import DomainValidationError

    slug = await _unique_slug()
    svc = _svc(db_session)

    session_obj = await svc.create_session(
        platform_admin_id=PLATFORM_ADMIN_ID,
        tenant_name="Incomplete Clinic",
        tenant_slug=slug,
        contact_email="incomplete@clinic.pl",
    )
    session_id = session_obj.id

    # Only step 1 complete — steps 2, 3, 4 missing
    await svc.execute_step_1(session_id, _step1_body("Incomplete Clinic"))

    with pytest.raises(DomainValidationError, match="steps"):
        await svc.activate(session_id)

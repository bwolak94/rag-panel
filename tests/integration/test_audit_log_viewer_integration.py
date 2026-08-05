"""Integration tests for AuditLogViewerService against a real PostgreSQL instance.

Uses testcontainers to spin up Postgres 16. Tests are skipped automatically if
Docker is unavailable or testcontainers is not installed.

Mark: @pytest.mark.integration

Design notes:
- AuditLog is a RANGE-partitioned table (partition key: created_at).
  We create a default partition so that all inserts land somewhere without
  requiring exact month-range partitions in the test schema.
- Schema is created via SQLAlchemy metadata.create_all() plus raw DDL for the
  partition; this mirrors production structure without running Alembic.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

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

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from src.db.models.audit_log import AuditLog
from src.db.models.base import Base
from src.domain.audit_log_viewer_service import AuditLogViewerService
from src.domain.auth import UserContext

# ── Module-level UUIDs ────────────────────────────────────────────────────────

TENANT_A_ID = uuid.uuid4()
TENANT_B_ID = uuid.uuid4()
USER_ID = uuid.uuid4()


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
    """Create async engine and all tables (including partition DDL)."""
    engine = create_async_engine(pg_dsn, echo=False)

    async with engine.begin() as conn:
        # Create all ORM-mapped tables.
        # audit_log has postgresql_partition_by in __table_args__ — SQLAlchemy will
        # emit "PARTITION BY RANGE (created_at)" in the CREATE TABLE statement.
        await conn.run_sync(Base.metadata.create_all)

        # Create a default partition so that any created_at value is accepted.
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS audit_log_default "
                "PARTITION OF audit_log DEFAULT"
            )
        )

        # Seed the two tenants needed across all tests.
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, slug, settings, status) VALUES "
                "(:id_a, 'Tenant A', 'tenant-a', '{}', 'active'), "
                "(:id_b, 'Tenant B', 'tenant-b', '{}', 'active') "
                "ON CONFLICT DO NOTHING"
            ),
            {"id_a": TENANT_A_ID, "id_b": TENANT_B_ID},
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


def _make_ctx(tenant_id: uuid.UUID = TENANT_A_ID) -> UserContext:
    return UserContext(
        user_id=USER_ID,
        keycloak_sub="kc-test-sub",
        email="admin@test.pl",
        display_name="Test Admin",
        tenant_id=tenant_id,
        roles=frozenset({"admin"}),
        permissions=frozenset({"admin:audit_log"}),
        allowed_collection_ids=frozenset(),
        writable_collection_ids=frozenset(),
    )


async def _insert_log_row(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    action: str,
    created_at: datetime | None = None,
    resource_type: str | None = None,
    resource_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    """Helper: insert a single AuditLog row and flush so it is queryable."""
    now = created_at or datetime.now(UTC)
    row = AuditLog(
        tenant_id=tenant_id,
        user_id=USER_ID,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        details=details or {},
        created_at=now,
    )
    session.add(row)
    await session.flush()
    return row


# ── Tests: Tenant isolation ────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_events_returns_only_own_tenant_events(db_session: AsyncSession) -> None:
    """Events for Tenant B must never appear in Tenant A's results."""
    await _insert_log_row(db_session, tenant_id=TENANT_A_ID, action="document.uploaded")
    await _insert_log_row(db_session, tenant_id=TENANT_B_ID, action="document.deleted")

    svc = AuditLogViewerService(db_session)
    response = await svc.list_events(_make_ctx(TENANT_A_ID))

    actions = {item.action for item in response.items}
    assert "document.uploaded" in actions
    assert "document.deleted" not in actions


# ── Tests: Cursor-based pagination ─────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cursor_pagination_returns_correct_pages(db_session: AsyncSession) -> None:
    """next_cursor from page 1 should yield the remaining items on page 2."""
    base_time = datetime.now(UTC)
    for i in range(5):
        await _insert_log_row(
            db_session,
            tenant_id=TENANT_A_ID,
            action=f"event.{i}",
            created_at=base_time - timedelta(seconds=i),
        )

    svc = AuditLogViewerService(db_session)

    # Page 1 — three items
    page1 = await svc.list_events(_make_ctx(), page_size=3)
    assert len(page1.items) == 3
    assert page1.has_next_page is True
    assert page1.next_cursor is not None

    # Page 2 — remaining two items
    page2 = await svc.list_events(_make_ctx(), cursor=page1.next_cursor, page_size=3)
    assert len(page2.items) == 2
    assert page2.has_next_page is False

    # No overlap between pages
    ids_page1 = {item.id for item in page1.items}
    ids_page2 = {item.id for item in page2.items}
    assert ids_page1.isdisjoint(ids_page2)


# ── Tests: Filtering ───────────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_filter_by_exact_action(db_session: AsyncSession) -> None:
    """Only events matching the exact action string are returned."""
    await _insert_log_row(db_session, tenant_id=TENANT_A_ID, action="document.uploaded")
    await _insert_log_row(db_session, tenant_id=TENANT_A_ID, action="collection.created")

    svc = AuditLogViewerService(db_session)
    response = await svc.list_events(_make_ctx(), action="document.uploaded")

    assert all(item.action == "document.uploaded" for item in response.items)
    assert any(item.action == "document.uploaded" for item in response.items)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_filter_by_action_prefix(db_session: AsyncSession) -> None:
    """action_prefix filter returns all events whose action starts with the prefix."""
    await _insert_log_row(db_session, tenant_id=TENANT_A_ID, action="document.uploaded")
    await _insert_log_row(db_session, tenant_id=TENANT_A_ID, action="document.deleted")
    await _insert_log_row(db_session, tenant_id=TENANT_A_ID, action="collection.created")

    svc = AuditLogViewerService(db_session)
    response = await svc.list_events(_make_ctx(), action_prefix="document.")

    actions = {item.action for item in response.items}
    assert "document.uploaded" in actions
    assert "document.deleted" in actions
    assert "collection.created" not in actions


@pytest.mark.integration
@pytest.mark.asyncio
async def test_filter_by_from_date(db_session: AsyncSession) -> None:
    """Events before from_date must be excluded."""
    now = datetime.now(UTC)
    old_time = now - timedelta(days=10)
    new_time = now - timedelta(seconds=1)

    await _insert_log_row(
        db_session, tenant_id=TENANT_A_ID, action="old.event", created_at=old_time
    )
    await _insert_log_row(
        db_session, tenant_id=TENANT_A_ID, action="new.event", created_at=new_time
    )

    cutoff = now - timedelta(days=1)
    svc = AuditLogViewerService(db_session)
    response = await svc.list_events(_make_ctx(), from_date=cutoff)

    actions = {item.action for item in response.items}
    assert "new.event" in actions
    assert "old.event" not in actions


@pytest.mark.integration
@pytest.mark.asyncio
async def test_filter_by_to_date(db_session: AsyncSession) -> None:
    """Events after to_date must be excluded."""
    now = datetime.now(UTC)
    recent_time = now - timedelta(seconds=1)
    future_time = now + timedelta(days=1)  # We insert it slightly in the future

    await _insert_log_row(
        db_session, tenant_id=TENANT_A_ID, action="recent.event", created_at=recent_time
    )
    await _insert_log_row(
        db_session, tenant_id=TENANT_A_ID, action="future.event", created_at=future_time
    )

    cutoff = now + timedelta(seconds=1)
    svc = AuditLogViewerService(db_session)
    response = await svc.list_events(_make_ctx(), to_date=cutoff)

    actions = {item.action for item in response.items}
    assert "recent.event" in actions
    assert "future.event" not in actions


# ── Tests: CSV export ──────────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_csv_export_contains_expected_headers_and_rows(
    db_session: AsyncSession,
) -> None:
    """Exported CSV has the canonical header row and at least one data row."""
    import csv
    import io

    await _insert_log_row(
        db_session,
        tenant_id=TENANT_A_ID,
        action="audit.csv_test",
        resource_type="document",
        details={"key": "value"},
    )

    svc = AuditLogViewerService(db_session)
    audit_stub = AsyncMock()
    audit_stub.log = AsyncMock()

    csv_text = await svc.export_csv(_make_ctx(), audit_service=audit_stub)

    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)

    expected_headers = {
        "id",
        "user_id",
        "user_display_name",
        "action",
        "resource_type",
        "resource_id",
        "details_summary",
        "ip_address",
        "created_at",
    }
    assert set(reader.fieldnames or []) == expected_headers

    audit_row = next((r for r in rows if r["action"] == "audit.csv_test"), None)
    assert audit_row is not None
    assert audit_row["resource_type"] == "document"

    # Self-audit call must be made
    audit_stub.log.assert_called_once()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_csv_export_tenant_isolation(db_session: AsyncSession) -> None:
    """CSV export for Tenant A must not contain rows belonging to Tenant B."""
    import csv
    import io

    await _insert_log_row(
        db_session, tenant_id=TENANT_A_ID, action="export.tenant_a"
    )
    await _insert_log_row(
        db_session, tenant_id=TENANT_B_ID, action="export.tenant_b"
    )

    svc = AuditLogViewerService(db_session)
    audit_stub = AsyncMock()
    audit_stub.log = AsyncMock()

    csv_text = await svc.export_csv(_make_ctx(TENANT_A_ID), audit_service=audit_stub)

    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)
    actions = {r["action"] for r in rows}

    assert "export.tenant_a" in actions
    assert "export.tenant_b" not in actions


# ── Tests: SQL injection via action_prefix ─────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_action_prefix_with_sql_like_wildcard_is_escaped(
    db_session: AsyncSession,
) -> None:
    """action_prefix containing '%' must be treated as a literal character.

    If escaping were absent, 'document%' would match all actions. With proper
    LIKE escape, the query must return zero rows (no action is literally
    'document%...').
    """
    await _insert_log_row(db_session, tenant_id=TENANT_A_ID, action="document.uploaded")
    await _insert_log_row(db_session, tenant_id=TENANT_A_ID, action="collection.created")

    svc = AuditLogViewerService(db_session)
    # "document%" as a literal prefix — no action starts with "document%"
    response = await svc.list_events(_make_ctx(), action_prefix="document%")

    assert len(response.items) == 0, (
        "action_prefix with literal '%' must match zero rows "
        "(the '%' must be escaped, not treated as a wildcard)"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_action_prefix_with_underscore_is_escaped(
    db_session: AsyncSession,
) -> None:
    """action_prefix containing '_' must not act as a single-character wildcard."""
    await _insert_log_row(db_session, tenant_id=TENANT_A_ID, action="document.uploaded")
    await _insert_log_row(db_session, tenant_id=TENANT_A_ID, action="docXment.uploaded")

    svc = AuditLogViewerService(db_session)
    # Literal underscore — only "doc_ment..." would match if underscore is literal
    response = await svc.list_events(_make_ctx(), action_prefix="doc_ment")

    actions = {item.action for item in response.items}
    # "docXment.uploaded" must NOT match (underscore was not used as wildcard)
    assert "docXment.uploaded" not in actions

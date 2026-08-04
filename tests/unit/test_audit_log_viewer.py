"""Unit tests for audit log viewer (TASK-031).

Tests cover:
- Cursor encode/decode round-trip
- CSV streaming produces valid CSV for a fixture dataset
- AuditLogViewerService.export_csv() calls audit_service.log()
- get_distinct_actions scoped to tenant
"""

from __future__ import annotations

import base64
import csv
import io
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.db.repositories.audit_log_repository import AuditLogRepository
from src.domain.audit_log_viewer_service import AuditLogViewerService
from src.domain.auth import UserContext

TENANT_ID = uuid.uuid4()
USER_ID = uuid.uuid4()


def _make_ctx() -> UserContext:
    return UserContext(
        user_id=USER_ID,
        keycloak_sub="kc-sub",
        email="admin@clinic.pl",
        display_name="Admin",
        tenant_id=TENANT_ID,
        roles=frozenset({"admin"}),
        permissions=frozenset({"admin:audit_log"}),
        allowed_collection_ids=frozenset(),
        writable_collection_ids=frozenset(),
    )


def _make_row(action: str = "document.approved") -> dict[str, Any]:
    return {
        "id": uuid.uuid4(),
        "user_id": USER_ID,
        "user_display_name": "Admin User",
        "action": action,
        "resource_type": "document",
        "resource_id": uuid.uuid4(),
        "details": {"note": "review passed"},
        "ip_address": "192.168.1.1",
        "created_at": datetime(2026, 8, 1, 10, 0, 0, tzinfo=UTC),
    }


# ── Cursor encode/decode ───────────────────────────────────────────────────────


def test_cursor_encode_decode_round_trip() -> None:
    """Cursor encodes datetime + UUID and decodes back correctly."""
    ts = datetime(2026, 8, 1, 10, 0, 0, tzinfo=UTC)
    row_id = uuid.uuid4()
    cursor = AuditLogRepository.encode_cursor(ts, row_id)
    # Decode manually (repository uses rsplit to handle ISO timestamp colons)
    raw = base64.b64decode(cursor.encode()).decode()
    ts_str, id_str = raw.rsplit(":", 1)
    decoded_ts = datetime.fromisoformat(ts_str)
    decoded_id = uuid.UUID(id_str)
    assert decoded_ts == ts
    assert decoded_id == row_id


def test_cursor_is_base64_string() -> None:
    ts = datetime(2026, 8, 1, 0, 0, 0, tzinfo=UTC)
    cursor = AuditLogRepository.encode_cursor(ts, uuid.uuid4())
    # Valid base64
    assert isinstance(cursor, str)
    base64.b64decode(cursor)  # must not raise


# ── CSV export ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_export_csv_produces_valid_csv() -> None:
    """export_csv() returns valid CSV with correct columns."""
    rows = [_make_row("document.approved"), _make_row("document.rejected")]

    session = MagicMock()
    audit_service = MagicMock()
    audit_service.log = AsyncMock()

    svc = AuditLogViewerService(session)

    with patch.object(
        svc._repo,
        "list_events",
        AsyncMock(return_value=(rows, False)),
    ):
        csv_str = await svc.export_csv(_make_ctx(), audit_service)

    reader = csv.DictReader(io.StringIO(csv_str))
    parsed = list(reader)
    assert len(parsed) == 2
    assert parsed[0]["action"] == "document.approved"
    assert parsed[1]["action"] == "document.rejected"
    assert "id" in parsed[0]
    assert "created_at" in parsed[0]


@pytest.mark.asyncio
async def test_export_csv_logs_audit_event() -> None:
    """export_csv() must log 'audit_log.exported' for GDPR accountability."""
    rows = [_make_row()]

    session = MagicMock()
    audit_service = MagicMock()
    audit_service.log = AsyncMock()

    svc = AuditLogViewerService(session)

    with patch.object(
        svc._repo,
        "list_events",
        AsyncMock(return_value=(rows, False)),
    ):
        await svc.export_csv(_make_ctx(), audit_service)

    audit_service.log.assert_awaited_once()
    call_kwargs = audit_service.log.call_args.kwargs
    assert call_kwargs["action"] == "audit_log.exported"


@pytest.mark.asyncio
async def test_export_csv_details_summary_truncated() -> None:
    """details_summary must be truncated to 200 chars."""
    row = _make_row()
    row["details"] = {"key": "x" * 300}

    session = MagicMock()
    audit_service = MagicMock()
    audit_service.log = AsyncMock()

    svc = AuditLogViewerService(session)

    with patch.object(
        svc._repo,
        "list_events",
        AsyncMock(return_value=([row], False)),
    ):
        csv_str = await svc.export_csv(_make_ctx(), audit_service)

    reader = csv.DictReader(io.StringIO(csv_str))
    parsed = list(reader)
    assert len(parsed[0]["details_summary"]) <= 200


# ── Distinct actions scoped ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_distinct_actions_returns_tenant_actions() -> None:
    """get_distinct_actions() returns the repo result for the tenant."""
    session = MagicMock()
    svc = AuditLogViewerService(session)

    expected = ["document.approved", "document.rejected", "user.login"]

    with patch.object(
        svc._repo,
        "get_distinct_actions",
        AsyncMock(return_value=expected),
    ):
        result = await svc.get_distinct_actions(_make_ctx())

    assert result.actions == expected

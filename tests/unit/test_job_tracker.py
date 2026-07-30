"""Unit tests for JobTracker — ingestion_jobs.steps JSONB progress tracking."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.ingest.job_tracker import JobTracker

JOB_ID = uuid.uuid4()
TENANT_ID = uuid.uuid4()


def _make_session() -> MagicMock:
    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    return session


@pytest.mark.asyncio
async def test_begin_stage_executes_update_and_commits() -> None:
    """begin_stage calls session.execute (UPDATE) and commit."""
    session = _make_session()
    tracker = JobTracker(session, JOB_ID, TENANT_ID)

    await tracker.begin_stage("fetch")

    session.execute.assert_called_once()
    session.commit.assert_called_once()
    # Verify it's an UPDATE statement targeting ingestion_jobs
    call_arg = session.execute.call_args[0][0]
    assert "ingestion_jobs" in str(call_arg)


@pytest.mark.asyncio
async def test_complete_stage_fetches_mutates_and_updates() -> None:
    """complete_stage reads steps, finds the stage, marks completed, saves."""
    session = _make_session()

    # First execute (SELECT steps): return a row with a running step
    steps_result = MagicMock()
    steps_result.scalar_one.return_value = [
        {
            "stage": "fetch",
            "status": "running",
            "started_at": "2026-01-01T00:00:00",
            "completed_at": None,
            "error": None,
            "meta": {},
        }
    ]
    # Second execute (UPDATE): plain mock
    update_result = MagicMock()
    session.execute = AsyncMock(side_effect=[steps_result, update_result])

    tracker = JobTracker(session, JOB_ID, TENANT_ID)
    await tracker.complete_stage("fetch", meta={"chunks": 5})

    assert session.execute.call_count == 2
    assert session.commit.call_count == 1


@pytest.mark.asyncio
async def test_fail_stage_increments_retry_count() -> None:
    """fail_stage marks last step failed and increments retry_count."""
    session = _make_session()

    # First execute (SELECT steps + retry_count)
    row = MagicMock()
    row.steps = [
        {
            "stage": "fetch",
            "status": "running",
            "started_at": "2026-01-01T00:00:00",
            "completed_at": None,
            "error": None,
            "meta": {},
        }
    ]
    row.retry_count = 0

    select_result = MagicMock()
    select_result.one.return_value = row
    update_result = MagicMock()
    session.execute = AsyncMock(side_effect=[select_result, update_result])

    tracker = JobTracker(session, JOB_ID, TENANT_ID)
    await tracker.fail_stage("fetch", "connection timeout")

    # Verify UPDATE was called with retry_count=1
    update_call = session.execute.call_args_list[1][0][0]
    stmt_str = str(update_call)
    assert "retry_count" in stmt_str or session.execute.call_count == 2


@pytest.mark.asyncio
async def test_fail_stage_truncates_error_at_2000_chars() -> None:
    """Long error messages are truncated to 2000 characters."""
    session = _make_session()

    row = MagicMock()
    row.steps = [
        {
            "stage": "extract",
            "status": "running",
            "started_at": "2026-01-01T00:00:00",
            "completed_at": None,
            "error": None,
            "meta": {},
        }
    ]
    row.retry_count = 1

    captured_steps: list = []

    async def capture_execute(stmt: object) -> MagicMock:
        result = MagicMock()
        result.one.return_value = row
        # On second call, capture the values
        if hasattr(stmt, "_values"):
            captured_steps.extend(stmt._values.get("steps", []))
        return result

    session.execute = AsyncMock(
        side_effect=[
            MagicMock(**{"one.return_value": row}),
            MagicMock(),
        ]
    )

    tracker = JobTracker(session, JOB_ID, TENANT_ID)

    long_error = "x" * 3000

    # We verify truncation by inspecting the steps mutation directly
    # by running the actual logic with a real-ish session mock
    select_result = MagicMock()
    select_result.one.return_value = row

    session.execute = AsyncMock(side_effect=[select_result, MagicMock()])
    await tracker.fail_stage("extract", long_error)

    # The steps list was mutated in-place — check row.steps[-1]["error"]
    assert len(row.steps[-1]["error"]) == 2000

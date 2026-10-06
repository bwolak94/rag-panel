"""CLI: python -m src.scripts.ab_report --experiment-id <id> [options]

Queries ab_test_results and prints a comparison report:
  - Sample count
  - Control vs shadow avg answer lengths
  - Control vs shadow avg latency (ms)
  - Shadow/control length ratio

Uses asyncio + SQLAlchemy async session, reads DATABASE_URL from env/settings.
Output is plain text suitable for terminal and CI logs.

Usage
-----
    python -m src.scripts.ab_report --experiment-id exp-generate-v2
    python -m src.scripts.ab_report --experiment-id exp-generate-v2 --tenant-id <uuid> --days 7

Exit codes
----------
    0 — report printed successfully
    1 — no data found for the given filters
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import UTC, datetime, timedelta
from typing import TypedDict

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.config import settings
from src.db.models.ab_test_result import ABTestResult


class ABTestMetrics(TypedDict):
    """Aggregated metrics for a single AB test experiment."""

    row_count: int
    control_version: str
    shadow_version: str
    avg_control_length: float
    avg_shadow_length: float
    avg_control_latency: float
    avg_shadow_latency: float


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build and return the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="ab_report",
        description="Print an AB test comparison report from ab_test_results.",
    )
    parser.add_argument(
        "--experiment-id",
        required=True,
        help="Experiment identifier to filter by (experiment_id column).",
    )
    parser.add_argument(
        "--tenant-id",
        default=None,
        help="Optional UUID to restrict results to a single tenant.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Look-back window in days (default: 30).",
    )
    return parser


async def _fetch_aggregates(
    session: AsyncSession,
    experiment_id: str,
    tenant_id: uuid.UUID | None,
    since: datetime,
) -> ABTestMetrics | None:
    """Query ab_test_results and return aggregated metrics.

    Args:
        session: Active async SQLAlchemy session.
        experiment_id: Logical experiment identifier to filter by.
        tenant_id: Optional tenant UUID to restrict the query scope.
        since: Lower bound for ``created_at`` (UTC).

    Returns:
        An ``ABTestMetrics`` dict with aggregated fields, or ``None`` when no rows match.
    """
    stmt = (
        select(
            func.count(ABTestResult.id).label("row_count"),
            func.min(ABTestResult.control_version).label("control_version"),
            func.min(ABTestResult.shadow_version).label("shadow_version"),
            func.avg(ABTestResult.control_length).label("avg_control_length"),
            func.avg(ABTestResult.shadow_length).label("avg_shadow_length"),
            func.avg(ABTestResult.latency_control_ms).label("avg_control_latency"),
            func.avg(ABTestResult.latency_shadow_ms).label("avg_shadow_latency"),
        )
        .where(ABTestResult.experiment_id == experiment_id)
        .where(ABTestResult.created_at >= since)
    )

    if tenant_id is not None:
        stmt = stmt.where(ABTestResult.tenant_id == tenant_id)

    result = await session.execute(stmt)
    row = result.one_or_none()

    if row is None or row.row_count == 0:
        return None

    return ABTestMetrics(
        row_count=int(row.row_count),
        control_version=str(row.control_version or ""),
        shadow_version=str(row.shadow_version or ""),
        avg_control_length=float(row.avg_control_length or 0.0),
        avg_shadow_length=float(row.avg_shadow_length or 0.0),
        avg_control_latency=float(row.avg_control_latency or 0.0),
        avg_shadow_latency=float(row.avg_shadow_latency or 0.0),
    )


def _print_report(experiment_id: str, metrics: ABTestMetrics) -> None:
    """Print a formatted AB test report to stdout.

    Args:
        experiment_id: The experiment identifier shown in the report header.
        metrics: Aggregated metrics dict as returned by ``_fetch_aggregates``.
    """
    count = metrics["row_count"]
    ctrl_ver = metrics["control_version"]
    shadow_ver = metrics["shadow_version"]
    avg_ctrl_len = metrics["avg_control_length"]
    avg_shad_len = metrics["avg_shadow_length"]
    avg_ctrl_lat = metrics["avg_control_latency"]
    avg_shad_lat = metrics["avg_shadow_latency"]

    length_ratio = avg_shad_len / avg_ctrl_len if avg_ctrl_len > 0 else 0.0

    print(f"AB Test Report — experiment: {experiment_id}")
    print(f"Rows analysed: {count}")
    print(f"Control version : {ctrl_ver}  | Shadow version: {shadow_ver}")
    print(
        f"Avg control length  : {avg_ctrl_len:.0f} chars"
        f" | Avg shadow length : {avg_shad_len:.0f} chars"
    )
    print(
        f"Avg control latency : {avg_ctrl_lat:,.0f} ms"
        f"  | Avg shadow latency: {avg_shad_lat:,.0f} ms"
    )
    print(f"Length ratio (shadow/control): {length_ratio:.2f}")


async def _run(
    experiment_id: str,
    tenant_id: uuid.UUID | None,
    days: int,
) -> int:
    """Main async entrypoint: fetch data and print report.

    Args:
        experiment_id: Experiment identifier to report on.
        tenant_id: Optional tenant UUID filter.
        days: Look-back window in days.

    Returns:
        Exit code — 0 on success, 1 when no data is found.
    """
    since = datetime.now(tz=UTC) - timedelta(days=days)

    engine = create_async_engine(
        str(settings.DATABASE_URL),
        echo=False,
        connect_args={"command_timeout": 10},
        pool_timeout=10,
    )
    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
    )

    try:
        async with session_factory() as session:
            try:
                metrics = await _fetch_aggregates(session, experiment_id, tenant_id, since)
            except Exception as exc:
                print(f"Error querying database: {exc}", file=sys.stderr)
                sys.exit(1)
    finally:
        await engine.dispose()

    if metrics is None:
        print(
            f"No data found for experiment '{experiment_id}' in the last {days} day(s).",
            file=sys.stderr,
        )
        return 1

    _print_report(experiment_id, metrics)
    return 0


def main() -> None:
    """CLI entrypoint — parse arguments and run the async report."""
    parser = _build_arg_parser()
    args = parser.parse_args()

    tenant_id: uuid.UUID | None = None
    if args.tenant_id is not None:
        try:
            tenant_id = uuid.UUID(args.tenant_id)
        except ValueError:
            parser.error(f"--tenant-id is not a valid UUID: {args.tenant_id!r}")

    exit_code = asyncio.run(_run(args.experiment_id, tenant_id, args.days))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

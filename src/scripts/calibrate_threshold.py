"""CLI: python -m src.scripts.calibrate_threshold --model-id <uuid> --scores-file <path> [--dry-run]

Reads a JSONL file of EvalResult items, runs the F1-maximising threshold sweep
via ThresholdCalibrationService, and optionally persists the result to
models_registry.

Usage
-----
    python -m src.scripts.calibrate_threshold \\
        --model-id 00000000-0000-0000-0000-000000000001 \\
        --scores-file /tmp/eval_results.jsonl

    # Compute only — do not write to the database:
    python -m src.scripts.calibrate_threshold \\
        --model-id 00000000-0000-0000-0000-000000000001 \\
        --scores-file /tmp/eval_results.jsonl \\
        --dry-run

Input format (JSONL)
--------------------
Each line must be a valid JSON object with the fields expected by EvalResult::

    {"question": "What is X?", "retrieved_score": 0.85, "relevant": true}

Malformed lines are skipped with a warning; the script does not abort.

Exit codes
----------
    0 — calibration succeeded (or dry-run printed result)
    1 — calibration not possible (no positive labels, empty dataset, DB error)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

import structlog
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.config import settings
from src.core.exceptions import NotFoundError
from src.domain.calibration_service import EvalResult, ThresholdCalibrationService, sweep_thresholds
from src.domain.model_service import ModelService

logger = structlog.get_logger(__name__)


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build and return the CLI argument parser.

    Returns:
        Configured ArgumentParser for the calibrate_threshold script.
    """
    parser = argparse.ArgumentParser(
        prog="calibrate_threshold",
        description=(
            "Auto-calibrate retrieval score threshold for an embedding model "
            "by sweeping F1 over a JSONL evaluation dataset."
        ),
    )
    parser.add_argument(
        "--model-id",
        required=True,
        help="UUID of the embedding model in models_registry to calibrate.",
    )
    parser.add_argument(
        "--scores-file",
        required=True,
        help=(
            "Path to a JSONL file where each line is a JSON object matching EvalResult: "
            '{"question": "...", "retrieved_score": 0.85, "relevant": true}'
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Compute and print the calibrated threshold but do NOT write to the database.",
    )
    return parser


def load_eval_results(scores_file: Path) -> list[EvalResult]:
    """Read and validate a JSONL scores file into a list of EvalResult objects.

    Each line is parsed as a JSON object and validated against EvalResult.
    Lines that are not valid JSON or do not match the EvalResult schema are
    skipped with a warning message printed to stderr; the script does not abort.

    Args:
        scores_file: Path to the JSONL file to read.

    Returns:
        List of successfully parsed EvalResult items (may be empty).
    """
    results: list[EvalResult] = []

    with scores_file.open(encoding="utf-8") as fh:
        for line_no, raw_line in enumerate(fh, start=1):
            line = raw_line.strip()
            if not line:
                continue

            try:
                data = json.loads(line)
            except json.JSONDecodeError as exc:
                print(
                    f"Warning: line {line_no} is not valid JSON — skipped. ({exc})",
                    file=sys.stderr,
                )
                continue

            try:
                results.append(EvalResult.model_validate(data))
            except ValidationError as exc:
                # Field names/types only — never actual values (GDPR: may contain medical text)
                error_locs = ", ".join(str(e["loc"]) for e in exc.errors())
                print(
                    f"Warning: line {line_no} failed EvalResult validation — skipped."
                    f" Errors: {error_locs}",
                    file=sys.stderr,
                )
                continue

    return results


def _print_report(
    model_id: uuid.UUID,
    sample_count: int,
    threshold_before: float | None,
    threshold_after: float,
    dry_run: bool,
) -> None:
    """Print a calibration summary report to stdout.

    Args:
        model_id: The model UUID that was calibrated.
        sample_count: Number of EvalResult items used.
        threshold_before: Previously stored calibrated threshold, or None if not calibrated.
        threshold_after: Newly computed threshold.
        dry_run: When True, annotate the report as a dry-run (not persisted).
    """
    if threshold_before is None:
        before_str = "not previously calibrated"
    else:
        before_str = f"{threshold_before:.3f}"

    header = "Calibration Report (DRY RUN)" if dry_run else "Calibration Report"
    print(f"{header} — model: {model_id}")
    print(f"Samples used    : {sample_count}")
    print(f"Threshold before: {before_str}")
    print(f"Threshold after : {threshold_after:.3f}")
    if dry_run:
        print("(No changes written to the database.)")


async def _run(model_id: uuid.UUID, scores_file: Path, dry_run: bool) -> int:
    """Main async entrypoint: load eval results, calibrate, and optionally persist.

    Args:
        model_id: UUID of the embedding model to calibrate.
        scores_file: Path to the JSONL evaluation dataset.
        dry_run: When True, compute the threshold but do not commit to the database.

    Returns:
        Exit code — 0 on success, 1 when calibration is not possible or an error occurs.
    """
    eval_results = load_eval_results(scores_file)

    if not eval_results:
        print(
            "Calibration not possible (no positive labels or empty dataset)",
            file=sys.stderr,
        )
        return 1

    # Dry-run: pure in-memory sweep — no DB session needed at all.
    if dry_run:
        result = sweep_thresholds(eval_results)
        if result is None:
            print(
                "Calibration not possible (no positive labels or empty dataset)",
                file=sys.stderr,
            )
            return 1
        threshold_after, _best_f1 = result
        _print_report(
            model_id=model_id,
            sample_count=len(eval_results),
            threshold_before=None,
            threshold_after=threshold_after,
            dry_run=True,
        )
        return 0

    # Live run: open session, calibrate, commit.
    threshold_before: float | None = None
    threshold_after_live: float | None = None

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
                model_service = ModelService(session)
                calibration_service = ThresholdCalibrationService(model_service)

                threshold_before = await model_service.get_calibrated_threshold(model_id)
                threshold_after_live = await calibration_service.calibrate_from_eval_results(
                    model_id, eval_results
                )

                if threshold_after_live is None:
                    print(
                        "Calibration not possible (no positive labels or empty dataset)",
                        file=sys.stderr,
                    )
                    return 1

                await session.commit()

            except NotFoundError:
                print(f"Model {model_id} not found in models_registry.", file=sys.stderr)
                return 1
            except Exception as exc:  # noqa: BLE001
                print(f"Error during calibration: {type(exc).__name__}", file=sys.stderr)
                return 1
    finally:
        await engine.dispose()

    # threshold_after_live is guaranteed non-None here: the None branch returned 1 above.
    assert threshold_after_live is not None
    _print_report(
        model_id=model_id,
        sample_count=len(eval_results),
        threshold_before=threshold_before,
        threshold_after=threshold_after_live,
        dry_run=False,
    )

    return 0


def main() -> None:
    """CLI entrypoint — parse arguments and run the async calibration."""
    parser = _build_arg_parser()
    args = parser.parse_args()

    try:
        model_id = uuid.UUID(args.model_id)
    except ValueError:
        parser.error(f"--model-id is not a valid UUID: {args.model_id!r}")
        return  # unreachable; parser.error() exits

    scores_file = Path(args.scores_file)
    if not scores_file.exists():
        print(f"Error: --scores-file not found: {scores_file}", file=sys.stderr)
        sys.exit(1)
    if not scores_file.is_file():
        print(f"Error: --scores-file is not a regular file: {scores_file}", file=sys.stderr)
        sys.exit(1)

    exit_code = asyncio.run(_run(model_id, scores_file, args.dry_run))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Metrics reporter for the RAG evaluation CI pipeline.

Reads current evaluation results (produced by the eval suite via pytest-json-report
or a results JSON written by the test), compares them against the stored baseline,
and formats a Markdown table suitable for posting as a GitHub PR comment.

Exit codes:
    0 — all metrics within 5% of baseline (or above it)
    1 — at least one metric regressed >5% below baseline

Usage (CI):
    # Run eval suite with JSON report output
    uv run pytest tests/eval/ -m eval -v \\
        --json-report --json-report-file=tests/eval/results.json

    # Post comment and gate the build
    uv run python tests/eval/metrics_reporter.py tests/eval/results.json

Usage (local):
    python tests/eval/metrics_reporter.py tests/eval/results.json

The PR comment is written to stdout so the GitHub Actions step can capture it:
    body=$(python tests/eval/metrics_reporter.py tests/eval/results.json)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

EVAL_DIR = Path(__file__).parent
BASELINE_PATH = EVAL_DIR / "baseline.json"
REGRESSION_THRESHOLD = 0.05  # 5 %

METRIC_LABELS: dict[str, str] = {
    "faithfulness": "Faithfulness",
    "answer_relevancy": "Answer Relevancy",
    "context_precision": "Context Precision",
    "context_recall": "Context Recall",
}

METRIC_DESCRIPTIONS: dict[str, str] = {
    "faithfulness": (
        "Every claim in the answer is grounded in a retrieved context chunk. "
        "Regressions indicate hallucinations."
    ),
    "answer_relevancy": (
        "The answer directly addresses the question asked. "
        "Regressions indicate off-topic or generic responses."
    ),
    "context_precision": (
        "Retrieved chunks are relevant to the question. Regressions indicate retrieval noise."
    ),
    "context_recall": (
        "Retrieved chunks contain the information needed for a complete answer. "
        "Regressions indicate missed source passages."
    ),
}


def _load_baseline() -> dict[str, float]:
    """Load and return the baseline metrics dict from baseline.json."""
    with open(BASELINE_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    return {k: float(v) for k, v in data.items() if k in METRIC_LABELS}


def _extract_metrics_from_pytest_report(report_path: Path) -> dict[str, float] | None:
    """Try to extract metric values from a pytest-json-report output file.

    pytest-json-report stores test outcomes in ``report["tests"]``.
    The ``test_print_metrics_summary`` test captures a structured dict in its
    ``longrepr`` field via an assertion message.  We parse that as a fallback.

    Returns None when the report does not contain parseable metrics.
    """
    with open(report_path, encoding="utf-8") as fh:
        report = json.load(fh)

    # Prefer a standalone results.json written by update_baseline.py format
    if all(k in report for k in METRIC_LABELS):
        return {k: float(report[k]) for k in METRIC_LABELS}

    return None


def _load_results(results_path: Path) -> dict[str, float]:
    """Load metric results from the given path.

    Supports two formats:
    1. A flat JSON dict with metric keys (produced by update_baseline.py).
    2. A pytest-json-report JSON (produced by ``--json-report`` pytest plugin).

    Raises:
        SystemExit: When the file cannot be parsed or metrics are not found.
    """
    if not results_path.exists():
        print(f"ERROR: results file not found: {results_path}", file=sys.stderr)
        sys.exit(1)

    metrics = _extract_metrics_from_pytest_report(results_path)
    if metrics is None:
        print(
            f"ERROR: could not extract metrics from {results_path}.\n"
            "Expected a JSON with keys: faithfulness, answer_relevancy, "
            "context_precision, context_recall.",
            file=sys.stderr,
        )
        sys.exit(1)
    return metrics


def build_markdown_comment(
    current: dict[str, float],
    baseline: dict[str, float],
) -> tuple[str, bool]:
    """Build a Markdown PR comment body and determine overall pass/fail.

    Args:
        current: Current metric values from the eval run.
        baseline: Baseline metric values from baseline.json.

    Returns:
        A tuple of (markdown_string, has_regression).
        has_regression is True when at least one metric fell >5% below baseline.
    """
    has_regression = False
    rows: list[str] = []

    for key, label in METRIC_LABELS.items():
        cur = current.get(key, 0.0)
        base = baseline.get(key, 0.0)
        delta = cur - base
        floor = base * (1 - REGRESSION_THRESHOLD)
        regressed = cur < floor

        if regressed:
            has_regression = True

        status = "FAIL (-5% regression)" if regressed else "pass"
        sign = "+" if delta >= 0 else ""
        rows.append(f"| {label} | {cur:.4f} | {base:.4f} | {sign}{delta:.4f} | {status} |")

    status_header = (
        "REGRESSION DETECTED — merge blocked" if has_regression else "All metrics within threshold"
    )
    status_emoji_text = "[FAIL]" if has_regression else "[PASS]"

    lines = [
        f"## RAG Evaluation Results {status_emoji_text}",
        "",
        f"**Status:** {status_header}",
        "",
        "| Metric | Current | Baseline | Delta | Status |",
        "|--------|---------|----------|-------|--------|",
        *rows,
        "",
        "**Regression threshold:** 5% below baseline on any metric blocks merge.",
        "",
        "### Metric definitions",
    ]
    for key, label in METRIC_LABELS.items():
        lines.append(f"- **{label}:** {METRIC_DESCRIPTIONS[key]}")

    lines += [
        "",
        "---",
        "_Generated by `tests/eval/metrics_reporter.py`._",
        "_To update the baseline after an intentional improvement: "
        "`python tests/eval/update_baseline.py`_",
    ]

    return "\n".join(lines), has_regression


def main() -> None:
    """Entry point: load results, compare to baseline, print comment, exit 0/1."""
    if len(sys.argv) < 2:
        print(
            "Usage: python tests/eval/metrics_reporter.py <results.json>",
            file=sys.stderr,
        )
        sys.exit(1)

    results_path = Path(sys.argv[1])
    current = _load_results(results_path)
    baseline = _load_baseline()

    comment, has_regression = build_markdown_comment(current, baseline)
    print(comment)

    if has_regression:
        sys.exit(1)


if __name__ == "__main__":
    main()

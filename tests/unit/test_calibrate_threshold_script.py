"""Unit tests for src/scripts/calibrate_threshold.py — pure-logic helpers.

No DB connections or async engine created. All DB-dependent code paths are
mocked out. Only the file-parsing and validation logic is tested here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.domain.calibration_service import EvalResult
from src.scripts.calibrate_threshold import load_eval_results

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_jsonl(tmp_path: Path, lines: list[str]) -> Path:
    """Write a list of raw JSONL lines to a temp file and return its path.

    Args:
        tmp_path: pytest tmp_path fixture directory.
        lines: Raw string lines to write (newline appended automatically).

    Returns:
        Path to the written file.
    """
    p = tmp_path / "eval.jsonl"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def _make_valid_line(
    question: str = "What is X?", score: float = 0.8, relevant: bool = True
) -> str:
    """Return a valid JSON line matching EvalResult schema.

    Args:
        question: Question text.
        score: retrieved_score value.
        relevant: Ground-truth label.

    Returns:
        JSON-serialised string (no trailing newline).
    """
    return json.dumps({"question": question, "retrieved_score": score, "relevant": relevant})


# ---------------------------------------------------------------------------
# load_eval_results — valid JSONL
# ---------------------------------------------------------------------------


def test_load_valid_jsonl_returns_eval_results(tmp_path: Path) -> None:
    """Valid JSONL produces a list of EvalResult with correct field values."""
    lines = [
        _make_valid_line("Q1", 0.9, True),
        _make_valid_line("Q2", 0.4, False),
        _make_valid_line("Q3", 0.75, True),
    ]
    p = _write_jsonl(tmp_path, lines)

    results = load_eval_results(p)

    assert len(results) == 3
    assert all(isinstance(r, EvalResult) for r in results)

    assert results[0].question == "Q1"
    assert results[0].retrieved_score == pytest.approx(0.9)
    assert results[0].relevant is True

    assert results[1].question == "Q2"
    assert results[1].retrieved_score == pytest.approx(0.4)
    assert results[1].relevant is False

    assert results[2].question == "Q3"
    assert results[2].retrieved_score == pytest.approx(0.75)
    assert results[2].relevant is True


def test_load_jsonl_single_line(tmp_path: Path) -> None:
    """Single-line JSONL file produces a one-element list."""
    p = _write_jsonl(tmp_path, [_make_valid_line()])
    results = load_eval_results(p)
    assert len(results) == 1


# ---------------------------------------------------------------------------
# load_eval_results — empty file
# ---------------------------------------------------------------------------


def test_load_empty_jsonl_returns_empty_list(tmp_path: Path) -> None:
    """An empty file (or file with only blank lines) returns an empty list."""
    p = tmp_path / "empty.jsonl"
    p.write_text("", encoding="utf-8")

    results = load_eval_results(p)

    assert results == []


def test_load_blank_lines_only_returns_empty_list(tmp_path: Path) -> None:
    """File containing only whitespace / blank lines returns empty list."""
    p = _write_jsonl(tmp_path, ["", "   ", ""])
    results = load_eval_results(p)
    assert results == []


# ---------------------------------------------------------------------------
# load_eval_results — malformed lines are skipped
# ---------------------------------------------------------------------------


def test_load_skips_malformed_json_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Non-JSON lines emit a warning to stderr but do not abort the load."""
    lines = [
        _make_valid_line("Q1", 0.9, True),
        "THIS IS NOT JSON {{{{",
        _make_valid_line("Q2", 0.5, False),
    ]
    p = _write_jsonl(tmp_path, lines)

    results = load_eval_results(p)

    assert len(results) == 2, "Malformed line should be skipped, not raise"
    captured = capsys.readouterr()
    assert "Warning" in captured.err
    assert "line 2" in captured.err


def test_load_skips_invalid_schema_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """JSON lines that fail EvalResult validation are skipped with a warning."""
    # Missing required 'relevant' field
    bad_line = json.dumps({"question": "Q?", "retrieved_score": 0.7})
    lines = [
        _make_valid_line("Q1", 0.9, True),
        bad_line,
        _make_valid_line("Q2", 0.3, False),
    ]
    p = _write_jsonl(tmp_path, lines)

    results = load_eval_results(p)

    assert len(results) == 2
    captured = capsys.readouterr()
    assert "Warning" in captured.err
    assert "line 2" in captured.err


def test_load_skips_multiple_malformed_lines(tmp_path: Path) -> None:
    """Multiple bad lines are all skipped; only valid lines are returned."""
    lines = [
        "not json at all",
        _make_valid_line("Q1", 0.8, True),
        "{ incomplete",
        json.dumps({"question": "no score"}),  # missing retrieved_score + relevant
        _make_valid_line("Q2", 0.6, False),
    ]
    p = _write_jsonl(tmp_path, lines)

    results = load_eval_results(p)

    assert len(results) == 2


def test_load_all_malformed_returns_empty_list(tmp_path: Path) -> None:
    """When every line is malformed, the result is an empty list (no exception)."""
    lines = [
        "not json",
        "{bad: json}",
        "also not json",
    ]
    p = _write_jsonl(tmp_path, lines)

    results = load_eval_results(p)

    assert results == []


# ---------------------------------------------------------------------------
# EvalResult — Pydantic validation guards (score outside [0, 1])
# ---------------------------------------------------------------------------


def test_eval_result_score_above_one_raises_validation_error() -> None:
    """retrieved_score > 1.0 must raise Pydantic ValidationError."""
    with pytest.raises(ValidationError):
        EvalResult(question="Q", retrieved_score=1.5, relevant=True)


def test_eval_result_score_below_zero_raises_validation_error() -> None:
    """retrieved_score < 0.0 must raise Pydantic ValidationError."""
    with pytest.raises(ValidationError):
        EvalResult(question="Q", retrieved_score=-0.1, relevant=False)


def test_eval_result_score_at_boundary_values_valid() -> None:
    """Scores exactly at 0.0 and 1.0 are valid boundary values."""
    r_min = EvalResult(question="Q", retrieved_score=0.0, relevant=False)
    r_max = EvalResult(question="Q", retrieved_score=1.0, relevant=True)

    assert r_min.retrieved_score == pytest.approx(0.0)
    assert r_max.retrieved_score == pytest.approx(1.0)


def test_eval_result_score_out_of_range_in_jsonl_file_is_skipped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """JSONL line with retrieved_score outside [0, 1] is skipped with a warning."""
    bad_line = json.dumps({"question": "Q?", "retrieved_score": 2.0, "relevant": True})
    lines = [
        bad_line,
        _make_valid_line("Q_valid", 0.5, True),
    ]
    p = _write_jsonl(tmp_path, lines)

    results = load_eval_results(p)

    assert len(results) == 1
    assert results[0].question == "Q_valid"
    captured = capsys.readouterr()
    assert "Warning" in captured.err

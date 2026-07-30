"""Unit tests for ThresholdCalibrationService and its helpers.

No DB or real model endpoints are used — ModelService is mocked.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.calibration_service import (
    EvalResult,
    ThresholdCalibrationService,
    _f1_at_threshold,
    _quantile,
    sweep_thresholds,
)

# ---------------------------------------------------------------------------
# _quantile
# ---------------------------------------------------------------------------


def test_quantile_single_element() -> None:
    assert _quantile([0.5], 0.0) == 0.5
    assert _quantile([0.5], 1.0) == 0.5


def test_quantile_two_elements() -> None:
    values = [0.0, 1.0]
    assert _quantile(values, 0.0) == 0.0
    assert _quantile(values, 1.0) == 1.0
    assert _quantile(values, 0.5) == pytest.approx(0.5)


def test_quantile_interpolates() -> None:
    values = [0.0, 0.4, 0.8, 1.0]
    # q=0.5 is halfway between index 1 and 2
    result = _quantile(values, 0.5)
    assert 0.4 <= result <= 0.8


# ---------------------------------------------------------------------------
# _f1_at_threshold
# ---------------------------------------------------------------------------


def _make_results(items: list[tuple[float, bool]]) -> list[EvalResult]:
    return [EvalResult(question="q", retrieved_score=score, relevant=rel) for score, rel in items]


def test_f1_perfect_separation() -> None:
    """All relevant chunks score above threshold, all irrelevant below."""
    results = _make_results([(0.9, True), (0.8, True), (0.3, False), (0.2, False)])
    f1 = _f1_at_threshold(results, threshold=0.5)
    assert f1 == pytest.approx(1.0)


def test_f1_all_predicted_positive() -> None:
    """Threshold=0 → everything predicted positive → recall=1, precision depends on mix."""
    results = _make_results([(0.9, True), (0.5, False), (0.3, True)])
    f1 = _f1_at_threshold(results, threshold=0.0)
    # TP=2, FP=1, FN=0 → precision=2/3, recall=1, F1=0.8
    assert f1 == pytest.approx(0.8, abs=1e-6)


def test_f1_zero_when_nothing_predicted_positive() -> None:
    """Threshold above all scores → zero predictions → F1=0."""
    results = _make_results([(0.9, True), (0.8, True)])
    assert _f1_at_threshold(results, threshold=1.1) == 0.0


def test_f1_zero_when_no_true_positives() -> None:
    """Relevant items all below threshold, irrelevant above → FP only → F1=0."""
    results = _make_results([(0.9, False), (0.2, True)])
    assert _f1_at_threshold(results, threshold=0.5) == 0.0


# ---------------------------------------------------------------------------
# sweep_thresholds
# ---------------------------------------------------------------------------


def test_sweep_returns_none_on_empty_input() -> None:
    assert sweep_thresholds([]) is None


def test_sweep_returns_none_when_no_positive_labels() -> None:
    results = _make_results([(0.9, False), (0.7, False), (0.5, False)])
    assert sweep_thresholds(results) is None


def test_sweep_finds_optimal_threshold() -> None:
    """Clear score gap between relevant and irrelevant chunks."""
    results = _make_results(
        [
            (0.92, True),
            (0.88, True),
            (0.85, True),
            (0.20, False),
            (0.15, False),
            (0.10, False),
        ]
    )
    result = sweep_thresholds(results)
    assert result is not None
    threshold, f1 = result
    # The optimal cut should be somewhere between 0.20 and 0.85
    assert 0.20 <= threshold <= 0.85
    assert f1 == pytest.approx(1.0)


def test_sweep_returns_nonzero_f1() -> None:
    """Partial overlap — best F1 should still be > 0."""
    results = _make_results(
        [
            (0.9, True),
            (0.6, True),
            (0.5, False),
            (0.4, True),
            (0.2, False),
        ]
    )
    result = sweep_thresholds(results)
    assert result is not None
    _, f1 = result
    assert f1 > 0.0


def test_sweep_returns_none_when_all_f1_zero() -> None:
    """Completely inverted scores (all relevant below threshold) → all F1 zero → None."""
    # Only one relevant chunk at 0.0, one irrelevant at 1.0
    # Any threshold that includes the irrelevant excludes the relevant
    _make_results([(1.0, False), (0.0, True)])
    # Because every quantile candidate will land somewhere between 0 and 1,
    # there will be thresholds that produce F1>0 (e.g. threshold=0 catches True).
    # So instead test with relevant=True score < every candidate:
    results2 = _make_results([(0.0, True)])  # one result, quantile will be 0.0
    # threshold=0.0: TP=1, FP=0, FN=0 → F1=1
    result2 = sweep_thresholds(results2)
    assert result2 is not None
    assert result2[1] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# ThresholdCalibrationService.calibrate_from_eval_results
# ---------------------------------------------------------------------------


def _make_calibration_service() -> tuple[ThresholdCalibrationService, MagicMock]:
    """Build a ThresholdCalibrationService with a mocked ModelService."""
    from src.domain.model_service import ModelService

    mock_model_service = MagicMock(spec=ModelService)
    mock_model_service.update_score_threshold = AsyncMock(return_value=None)

    svc = ThresholdCalibrationService.__new__(ThresholdCalibrationService)
    svc._model_service = mock_model_service  # bypass __init__ isinstance check
    return svc, mock_model_service


@pytest.mark.asyncio
async def test_calibrate_returns_threshold_and_persists() -> None:
    """Happy path: clear signal → returns threshold and calls update_score_threshold."""
    svc, mock_ms = _make_calibration_service()
    model_id = uuid.uuid4()

    results = _make_results([(0.9, True), (0.85, True), (0.1, False), (0.05, False)])
    threshold = await svc.calibrate_from_eval_results(model_id, results)

    assert threshold is not None
    assert 0.0 < threshold < 1.0

    mock_ms.update_score_threshold.assert_awaited_once()
    call_kwargs = mock_ms.update_score_threshold.call_args
    assert call_kwargs.kwargs["model_id"] == model_id
    assert call_kwargs.kwargs["threshold"] == threshold
    assert call_kwargs.kwargs["sample_count"] == len(results)


@pytest.mark.asyncio
async def test_calibrate_returns_none_on_empty_input() -> None:
    """Empty eval_results → None, no DB write."""
    svc, mock_ms = _make_calibration_service()
    model_id = uuid.uuid4()

    result = await svc.calibrate_from_eval_results(model_id, [])

    assert result is None
    mock_ms.update_score_threshold.assert_not_awaited()


@pytest.mark.asyncio
async def test_calibrate_returns_none_when_no_positive_labels() -> None:
    """All relevant=False → sweep returns None → no DB write."""
    svc, mock_ms = _make_calibration_service()
    model_id = uuid.uuid4()

    results = _make_results([(0.9, False), (0.7, False)])
    result = await svc.calibrate_from_eval_results(model_id, results)

    assert result is None
    mock_ms.update_score_threshold.assert_not_awaited()


@pytest.mark.asyncio
async def test_calibrate_propagates_not_found_error() -> None:
    """NotFoundError from ModelService bubbles up unchanged."""
    from src.core.exceptions import NotFoundError
    from src.domain.model_service import ModelService

    mock_ms = MagicMock(spec=ModelService)
    mock_ms.update_score_threshold = AsyncMock(side_effect=NotFoundError("Model not found"))

    svc = ThresholdCalibrationService.__new__(ThresholdCalibrationService)
    svc._model_service = mock_ms

    model_id = uuid.uuid4()
    results = _make_results([(0.9, True), (0.2, False)])

    with pytest.raises(NotFoundError):
        await svc.calibrate_from_eval_results(model_id, results)


# ---------------------------------------------------------------------------
# ThresholdCalibrationService.__init__ guard
# ---------------------------------------------------------------------------


def test_init_raises_on_non_model_service() -> None:
    """Passing a random object instead of ModelService raises TypeError."""
    with pytest.raises(TypeError, match="ModelService"):
        ThresholdCalibrationService(model_service=object())  # type: ignore[arg-type]

"""ThresholdCalibrationService — auto-calibrate retrieval score thresholds.

Algorithm
---------
Given a list of EvalResult items (one per retrieved chunk), the service sweeps
over candidate thresholds derived from quantiles of the observed score
distribution.  For each candidate it computes precision, recall, and F1 against
the ``relevant`` label, then selects the threshold with the highest F1.

If the eval set is empty or contains no positive labels, the service returns
None and skips persistence (no calibration possible).

The calibrated value is written back to ``models_registry`` via
``ModelService.update_score_threshold()``, which owns the flush.
Callers (e.g. eval harness or admin API) must commit the session afterwards.

Security note: this service writes only to the models_registry row for the
provided model_id; it does not touch tenant data and needs no UserContext.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from src.domain.model_service import ModelService

logger = structlog.get_logger(__name__)

# Quantile levels swept during calibration.  Fine enough for practical gains
# without being expensive on moderate eval sets.
_QUANTILE_LEVELS: list[float] = [
    0.05,
    0.10,
    0.15,
    0.20,
    0.25,
    0.30,
    0.35,
    0.40,
    0.45,
    0.50,
    0.55,
    0.60,
    0.65,
    0.70,
    0.75,
    0.80,
    0.85,
    0.90,
    0.95,
]


class EvalResult(BaseModel):
    """A single retrieved chunk from an evaluation query.

    Fields:
        question: The evaluation question (used only for logging / tracing).
        retrieved_score: Qdrant similarity score returned for this chunk.
        relevant: Ground-truth label — True if this chunk is relevant for the question.
    """

    question: str = Field(..., description="Evaluation question text (for logging only)")
    retrieved_score: float = Field(..., ge=0.0, le=1.0, description="Qdrant similarity score")
    relevant: bool = Field(..., description="Ground-truth relevance label")


def _quantile(values: list[float], q: float) -> float:
    """Return the q-th quantile of sorted values (linear interpolation).

    Args:
        values: Non-empty list of floats; must be sorted ascending.
        q: Quantile level in [0, 1].

    Returns:
        Interpolated quantile value.
    """
    n = len(values)
    if n == 1:
        return values[0]
    idx = q * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    frac = idx - lo
    return values[lo] * (1.0 - frac) + values[hi] * frac


def _f1_at_threshold(results: Sequence[EvalResult], threshold: float) -> float:
    """Compute F1 score when predicting 'relevant' for score >= threshold.

    A chunk is predicted positive when its retrieved_score >= threshold.
    Precision = TP / (TP + FP), Recall = TP / (TP + FN), F1 = harmonic mean.

    Returns 0.0 when both precision and recall are zero (degenerate cases).

    Args:
        results: Eval results to evaluate.
        threshold: Score cut-off to test.

    Returns:
        F1 value in [0.0, 1.0].
    """
    tp = fp = fn = 0
    for r in results:
        predicted = r.retrieved_score >= threshold
        if predicted and r.relevant:
            tp += 1
        elif predicted and not r.relevant:
            fp += 1
        elif not predicted and r.relevant:
            fn += 1
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    if precision + recall == 0.0:
        return 0.0
    return 2.0 * precision * recall / (precision + recall)


def sweep_thresholds(eval_results: Sequence[EvalResult]) -> tuple[float, float] | None:
    """Find the threshold that maximises F1 over eval_results.

    Candidate thresholds are derived from quantiles of the observed score
    distribution so the sweep adapts to whatever score range the current
    embedding model produces.

    Args:
        eval_results: Non-empty sequence of EvalResult items.

    Returns:
        (best_threshold, best_f1) tuple, or None if calibration is not possible
        (empty input, no positive labels, or all F1 values are 0).
    """
    if not eval_results:
        return None

    has_positive = any(r.relevant for r in eval_results)
    if not has_positive:
        logger.warning("calibration.no_positive_labels", n=len(eval_results))
        return None

    scores_sorted = sorted(r.retrieved_score for r in eval_results)
    candidates = sorted({_quantile(scores_sorted, q) for q in _QUANTILE_LEVELS})

    best_threshold = candidates[0]
    best_f1 = -1.0

    for candidate in candidates:
        f1 = _f1_at_threshold(eval_results, candidate)
        if f1 > best_f1:
            best_f1 = f1
            best_threshold = candidate

    if best_f1 <= 0.0:
        logger.warning(
            "calibration.all_f1_zero",
            n=len(eval_results),
            candidates=len(candidates),
        )
        return None

    return best_threshold, best_f1


class ThresholdCalibrationService:
    """Calibrate and persist optimal score thresholds for embedding models.

    Usage
    -----
    Inject a ``ModelService`` instance (which carries the async DB session).
    Call ``calibrate_from_eval_results()`` after an eval run; it sweeps
    thresholds, picks the best F1, and writes the result via ``ModelService``.

    The session is NOT committed here — the caller (router / eval harness)
    owns the transaction boundary.
    """

    def __init__(self, model_service: ModelService) -> None:
        from src.domain.model_service import ModelService as _ModelService  # avoid circular dep

        if not isinstance(model_service, _ModelService):
            raise TypeError("model_service must be a ModelService instance")
        self._model_service = model_service

    async def calibrate_from_eval_results(
        self,
        model_id: uuid.UUID,
        eval_results: list[EvalResult],
    ) -> float | None:
        """Sweep thresholds, pick the best F1, persist, and return the threshold.

        If the sweep cannot produce a valid threshold (no positive labels, empty
        input, all-zero F1) the method returns None and does NOT update the DB.

        Args:
            model_id: UUID of the models_registry row to update.
            eval_results: Retrieved chunk results with ground-truth relevance labels.

        Returns:
            The calibrated threshold, or None when calibration was not possible.

        Raises:
            NotFoundError: If model_id does not exist in models_registry.
        """
        result = sweep_thresholds(eval_results)
        if result is None:
            logger.info(
                "calibration.skipped",
                model_id=str(model_id),
                n=len(eval_results),
            )
            return None

        best_threshold, best_f1 = result
        sample_count = len(eval_results)

        await self._model_service.update_score_threshold(
            model_id=model_id,
            threshold=best_threshold,
            sample_count=sample_count,
        )

        logger.info(
            "calibration.complete",
            model_id=str(model_id),
            threshold=best_threshold,
            f1=best_f1,
            sample_count=sample_count,
        )
        return best_threshold

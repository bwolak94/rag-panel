"""Retrieval result fusion utilities — ADR-013.

Pure functions for combining results from multiple ranked lists.
Provides Reciprocal Rank Fusion (RRF) used by RetrievalService.search_hybrid().
No external dependencies — deterministic and testable in isolation.
"""

from __future__ import annotations

from uuid import UUID

from src.retrieval.schemas import RetrievalResult

# RRF constant — standard value from the original Cormack et al. (2009) paper.
# Higher k smooths rank differences; 60 is the conventional default.
_DEFAULT_RRF_K = 60


def rrf_fuse(
    dense_results: list[RetrievalResult],
    sparse_results: list[RetrievalResult],
    k: int = _DEFAULT_RRF_K,
) -> list[RetrievalResult]:
    """Merge dense and sparse ranked lists using Reciprocal Rank Fusion.

    RRF score for a point: sum over each ranked list of 1 / (k + rank),
    where rank is 1-based. Points that appear in only one list still get
    a contribution from that list (rank from the other list is omitted —
    the standard RRF formulation for partial coverage).

    Args:
        dense_results: Results ordered by dense similarity score (index 0 = best).
        sparse_results: Results ordered by sparse/BM25 score (index 0 = best).
        k: Smoothing constant. Default 60 (Cormack et al. 2009).

    Returns:
        Deduplicated list of RetrievalResult ordered by RRF score descending.
        The .score field of each result is replaced with the computed RRF score
        so callers can apply a downstream threshold if needed.
    """
    rrf_scores: dict[UUID, float] = {}
    # Keep the best RetrievalResult object for each point_id (dense wins on tie).
    best_result: dict[UUID, RetrievalResult] = {}

    for rank, result in enumerate(dense_results, start=1):
        pid = result.point_id
        rrf_scores[pid] = rrf_scores.get(pid, 0.0) + 1.0 / (k + rank)
        if pid not in best_result:
            best_result[pid] = result

    for rank, result in enumerate(sparse_results, start=1):
        pid = result.point_id
        rrf_scores[pid] = rrf_scores.get(pid, 0.0) + 1.0 / (k + rank)
        if pid not in best_result:
            best_result[pid] = result

    # NOTE: the .score on returned results is an RRF score in (0, 1/(k+1)], NOT a
    # cosine similarity. Callers must not apply cosine-range thresholds to these scores.
    merged: list[RetrievalResult] = []
    for pid, rrf_score in sorted(rrf_scores.items(), key=lambda kv: kv[1], reverse=True):
        result_copy = best_result[pid].model_copy(update={"score": rrf_score})
        merged.append(result_copy)

    return merged

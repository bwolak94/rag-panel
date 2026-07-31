"""Reranker utilities — Reciprocal Rank Fusion and (future) cross-encoder reranking.

Phase 2: RRF is active and used by search_hybrid().
Phase 3: cross-encoder reranking will replace or extend the rerank() no-op below.
"""

from __future__ import annotations

from uuid import UUID

from src.retrieval.schemas import RetrievalResult

# RRF constant — standard value from the original Cormack et al. (2009) paper.
# Higher k smooths rank differences; 60 is the conventional default.
_RRF_K = 60


def reciprocal_rank_fusion(
    dense_results: list[RetrievalResult],
    bm25_results: list[RetrievalResult],
    k: int = _RRF_K,
) -> list[RetrievalResult]:
    """Merge dense and BM25 ranked lists using Reciprocal Rank Fusion.

    RRF score for a point: sum over each ranked list of 1 / (k + rank),
    where rank is 1-based. Points that appear in only one list still get
    a contribution from that list (rank from the other list is omitted,
    not treated as infinity — this is equivalent to omitting missing
    documents, which is the standard RRF formulation).

    Args:
        dense_results: Results ordered by dense similarity score (index 0 = best).
        bm25_results:  Results ordered by BM25 score (index 0 = best).
        k:             Smoothing constant. Default 60 (Cormack et al. 2009).

    Returns:
        Deduplicated list of RetrievalResult ordered by RRF score descending.
        The .score field is replaced by the RRF score so callers can apply
        a downstream threshold if needed.
    """
    rrf_scores: dict[UUID, float] = {}
    # Keep the best RetrievalResult object for each point_id (dense wins on tie).
    best_result: dict[UUID, RetrievalResult] = {}

    for rank, result in enumerate(dense_results, start=1):
        pid = result.point_id
        rrf_scores[pid] = rrf_scores.get(pid, 0.0) + 1.0 / (k + rank)
        if pid not in best_result:
            best_result[pid] = result

    for rank, result in enumerate(bm25_results, start=1):
        pid = result.point_id
        rrf_scores[pid] = rrf_scores.get(pid, 0.0) + 1.0 / (k + rank)
        if pid not in best_result:
            best_result[pid] = result

    merged: list[RetrievalResult] = []
    for pid, rrf_score in sorted(rrf_scores.items(), key=lambda kv: kv[1], reverse=True):
        result_copy = best_result[pid].model_copy(update={"score": rrf_score})
        merged.append(result_copy)

    return merged


async def rerank(results: list[RetrievalResult], query: str) -> list[RetrievalResult]:
    """No-op placeholder. Returns results unchanged until cross-encoder is integrated."""
    return results

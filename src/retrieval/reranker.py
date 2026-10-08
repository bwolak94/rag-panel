"""Reranker utilities — Reciprocal Rank Fusion and (future) cross-encoder reranking.

Phase 2: RRF is active and used by search_hybrid(). Implementation lives in
src/retrieval/fusion.py (ADR-013); this module re-exports it for backward compat.
Phase 3: cross-encoder reranking will replace or extend the rerank() no-op below.
"""

from __future__ import annotations

from src.retrieval.fusion import rrf_fuse as reciprocal_rank_fusion
from src.retrieval.schemas import RetrievalResult

__all__ = ["reciprocal_rank_fusion", "rerank"]


async def rerank(results: list[RetrievalResult], query: str) -> list[RetrievalResult]:
    """No-op placeholder. Returns results unchanged until cross-encoder is integrated."""
    return results

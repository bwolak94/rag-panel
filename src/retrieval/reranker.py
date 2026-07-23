"""Reranker placeholder — Phase 3 cross-encoder reranking.

Not yet active. RetrievalService will call this after search() in Phase 3.
Interface kept stable so the service signature does not change.
"""

from __future__ import annotations

from src.retrieval.schemas import RetrievalResult


async def rerank(results: list[RetrievalResult], query: str) -> list[RetrievalResult]:
    """No-op placeholder. Returns results unchanged until cross-encoder is integrated."""
    return results

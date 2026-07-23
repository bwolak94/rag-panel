"""FastAPI dependency for RetrievalService.

AsyncQdrantClient is initialized once in the FastAPI lifespan and stored
in app.state.qdrant_client. Retrieved here per-request.
"""

from __future__ import annotations

from fastapi import Request
from qdrant_client import AsyncQdrantClient

from src.retrieval.service import RetrievalService


def get_qdrant_client(request: Request) -> AsyncQdrantClient:
    """Return the shared AsyncQdrantClient from app state."""
    return request.app.state.qdrant_client  # type: ignore[no-any-return]


def get_retrieval_service(request: Request) -> RetrievalService:
    """Return a RetrievalService backed by the shared Qdrant client."""
    return RetrievalService(client=get_qdrant_client(request))

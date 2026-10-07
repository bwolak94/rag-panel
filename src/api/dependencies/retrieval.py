"""FastAPI dependency for RetrievalService.

AsyncQdrantClient is initialized once in the FastAPI lifespan and stored
in app.state.qdrant_client. Retrieved here per-request.

ADR-022: qdrant_client must not be imported outside src/retrieval/.
get_qdrant_client returns Any — the client is retrieved from untyped app.state
and handed directly to RetrievalService, which owns all typing for it.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request

from src.retrieval.service import RetrievalService


def get_qdrant_client(request: Request) -> Any:
    """Return the shared AsyncQdrantClient from app state."""
    return request.app.state.qdrant_client


def get_retrieval_service(request: Request) -> RetrievalService:
    """Return a RetrievalService backed by the shared Qdrant client."""
    return RetrievalService(client=get_qdrant_client(request))

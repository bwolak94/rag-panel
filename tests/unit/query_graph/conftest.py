"""Test fixtures for query_graph unit tests."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture(autouse=True)
def patch_rag_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace RAGCache with a no-op mock so tests are cache-free."""
    null_cache = MagicMock()
    null_cache.get_retrieval = AsyncMock(return_value=None)
    null_cache.set_retrieval = AsyncMock()
    null_cache.get_response = AsyncMock(return_value=None)
    null_cache.set_response = AsyncMock()

    # __init__.py shadows the module name with the function; use sys.modules directly.
    import src.graphs.query_graph.nodes.node_retrieve  # noqa: F401 — ensure loaded

    mod = sys.modules["src.graphs.query_graph.nodes.node_retrieve"]
    monkeypatch.setattr(mod, "get_rag_cache", lambda: null_cache)

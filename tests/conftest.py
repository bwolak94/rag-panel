"""Shared pytest fixtures.

Session-scoped: override_settings, async_engine (in-memory SQLite for unit tests)
Function-scoped: async_client (fresh per test)
"""

from __future__ import annotations

import os

# Set required env vars before any src.* imports so Settings() can initialize
# during pytest collection. Tests that override specific vars use monkeypatch.
_TEST_ENV_DEFAULTS = {
    "SECRET_KEY": "test-secret-key-minimum-32-chars!!",
    "DATABASE_URL": "postgresql+asyncpg://raguser:ragpass@localhost:5432/ragdb",
    "REDIS_URL": "redis://localhost:6379/0",
    "MINIO_ENDPOINT": "localhost:9000",
    "MINIO_ACCESS_KEY": "minioadmin",
    "MINIO_SECRET_KEY": "minioadmin",
    "QDRANT_URL": "http://localhost:6333",
    "KEYCLOAK_BASE_URL": "http://localhost:8080",
    "KEYCLOAK_REALM": "rag-platform",
    "KEYCLOAK_CLIENT_ID": "rag-api",
    "KEYCLOAK_AUDIENCE": "rag-api",
    "LLM_BASE_URL": "http://localhost:11434/v1",
    "EMBEDDING_BASE_URL": "http://localhost:11434/v1",
}
for _key, _val in _TEST_ENV_DEFAULTS.items():
    os.environ.setdefault(_key, _val)

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def override_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Pre-set all required env vars so Settings() can instantiate in tests."""
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-32-chars-minimum!")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/testdb")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("MINIO_ENDPOINT", "localhost:9000")
    monkeypatch.setenv("MINIO_ACCESS_KEY", "minioadmin")
    monkeypatch.setenv("MINIO_SECRET_KEY", "minioadmin")
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("KEYCLOAK_BASE_URL", "http://localhost:8080")
    monkeypatch.setenv("KEYCLOAK_REALM", "rag-platform")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "rag-api")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "rag-api")
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://localhost:11434/v1")
    return monkeypatch


@pytest.fixture
async def test_app(override_env: pytest.MonkeyPatch) -> FastAPI:
    """Create a test FastAPI app instance with overridden settings."""
    from src.main import create_app

    return create_app()


@pytest.fixture
async def async_client(test_app: FastAPI) -> AsyncClient:
    """HTTP client for testing FastAPI endpoints."""
    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://test",
    ) as client:
        yield client

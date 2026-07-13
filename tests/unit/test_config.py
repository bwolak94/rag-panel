"""Tests for Settings validation.

Module-level import ensures src.core.config is cached before any test
removes env vars — the module-level `settings = Settings()` has already run.
"""

import pytest
from pydantic import ValidationError

# Importing here guarantees module is cached before individual tests manipulate env
from src.core.config import Settings  # noqa: E402


def test_settings_fail_without_secret_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings() must fail when SECRET_KEY is absent."""
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/db")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("MINIO_ENDPOINT", "localhost:9000")
    monkeypatch.setenv("MINIO_ACCESS_KEY", "key")
    monkeypatch.setenv("MINIO_SECRET_KEY", "secret")
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("KEYCLOAK_BASE_URL", "http://localhost:8080")
    monkeypatch.setenv("KEYCLOAK_REALM", "test")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "test")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "test")
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://localhost:11434/v1")

    with pytest.raises(ValidationError, match="SECRET_KEY"):
        Settings()


def test_settings_fail_without_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings() must fail when DATABASE_URL is absent."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("MINIO_ENDPOINT", "localhost:9000")
    monkeypatch.setenv("MINIO_ACCESS_KEY", "key")
    monkeypatch.setenv("MINIO_SECRET_KEY", "secret")
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("KEYCLOAK_BASE_URL", "http://localhost:8080")
    monkeypatch.setenv("KEYCLOAK_REALM", "test")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "test")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "test")
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://localhost:11434/v1")

    with pytest.raises(ValidationError, match="DATABASE_URL"):
        Settings()


def test_presigned_ttl_above_300_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import ValidationError as PydanticValidationError

    monkeypatch.setenv("SECRET_KEY", "test-key-32-chars-minimum-pad!!!")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/db")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("MINIO_ENDPOINT", "localhost:9000")
    monkeypatch.setenv("MINIO_ACCESS_KEY", "key")
    monkeypatch.setenv("MINIO_SECRET_KEY", "secret")
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("KEYCLOAK_BASE_URL", "http://localhost:8080")
    monkeypatch.setenv("KEYCLOAK_REALM", "test")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "test")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "test")
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("INGEST_PRESIGNED_URL_TTL_SECONDS", "301")

    from src.core.config import Settings

    with pytest.raises(PydanticValidationError, match="300 seconds"):
        Settings()


def test_presigned_ttl_at_300_is_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", "test-key-32-chars-minimum-pad!!!")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/db")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("MINIO_ENDPOINT", "localhost:9000")
    monkeypatch.setenv("MINIO_ACCESS_KEY", "key")
    monkeypatch.setenv("MINIO_SECRET_KEY", "secret")
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("KEYCLOAK_BASE_URL", "http://localhost:8080")
    monkeypatch.setenv("KEYCLOAK_REALM", "test")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "test")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "test")
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("INGEST_PRESIGNED_URL_TTL_SECONDS", "300")

    from src.core.config import Settings

    s = Settings()
    assert s.INGEST_PRESIGNED_URL_TTL_SECONDS == 300


def test_default_values() -> None:
    """Verify defaults don't change accidentally."""
    from src.core.config import settings

    assert settings.DB_POOL_SIZE == 10
    assert settings.DB_MAX_OVERFLOW == 20
    assert settings.DB_ECHO_SQL is False
    assert settings.RETRIEVAL_TOP_K == 8
    assert settings.RETRIEVAL_RELEVANCE_THRESHOLD == 0.5
    assert settings.INGEST_NODE_MAX_RETRIES == 3

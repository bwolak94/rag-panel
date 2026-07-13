"""Application settings loaded from environment variables."""

from pydantic import AnyUrl, PostgresDsn, RedisDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Application
    APP_VERSION: str = "0.1.0"
    ENVIRONMENT: str = "development"  # development | staging | production
    DEBUG: bool = False
    SECRET_KEY: str  # No default — app fails to start without it

    # Database
    DATABASE_URL: PostgresDsn
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_ECHO_SQL: bool = False

    # Redis
    REDIS_URL: RedisDsn

    # MinIO
    MINIO_ENDPOINT: str  # host:port, no scheme
    MINIO_ACCESS_KEY: str
    MINIO_SECRET_KEY: str  # No default — app fails to start without it
    MINIO_USE_TLS: bool = False

    # Qdrant
    QDRANT_URL: AnyUrl
    QDRANT_API_KEY: str | None = None  # None for unauthenticated local instance

    # Keycloak
    KEYCLOAK_BASE_URL: str  # e.g., http://keycloak:8080
    KEYCLOAK_REALM: str  # e.g., rag-platform
    KEYCLOAK_CLIENT_ID: str  # e.g., rag-api
    KEYCLOAK_AUDIENCE: str  # JWT aud claim to validate

    # LLM / Embedding (GPU host — OpenAI-compatible API)
    LLM_BASE_URL: str  # e.g., http://gpu-host:11434/v1
    EMBEDDING_BASE_URL: str
    LLM_API_KEY: str = "ollama"

    # Ingest
    INGEST_NODE_MAX_RETRIES: int = 3
    INGEST_PRESIGNED_URL_TTL_SECONDS: int = 300  # 5 minutes maximum

    # RAG
    RETRIEVAL_TOP_K: int = 8
    RETRIEVAL_RELEVANCE_THRESHOLD: float = 0.5
    NOT_FOUND_MESSAGE: str = "I could not find an answer in the available documents."

    # Langfuse (optional — disabled when keys are absent)
    LANGFUSE_PUBLIC_KEY: str | None = None
    LANGFUSE_SECRET_KEY: str | None = None
    LANGFUSE_HOST: str = "http://langfuse:3030"

    @field_validator("INGEST_PRESIGNED_URL_TTL_SECONDS")
    @classmethod
    def validate_presigned_ttl(cls, v: int) -> int:
        """Enforce maximum presigned URL TTL per security policy."""
        if v > 300:
            raise ValueError(
                "INGEST_PRESIGNED_URL_TTL_SECONDS must not exceed 300 seconds (5 minutes) "
                "per security policy."
            )
        return v


settings = Settings()  # required fields provided via environment variables

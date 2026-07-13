# TASK-001: Project Setup and Bootstrap

**Status:** TODO
**Priority:** P0 — required before any other task
**Owner:** backend-dev
**Reviewer:** python-reviewer
**Related docs:** `docs/architecture.md` §11, §18 | `docs/data-model.md`
**Estimated effort:** 2–3 days

---

## Overview

Bootstrap the entire repository structure for the multi-tenant RAG platform. This task establishes the Python project configuration, FastAPI application factory, settings management via `pydantic-settings`, structured logging, and the complete Docker Compose environment for local development.

The output of this task is a running local stack where every service is reachable, the FastAPI application starts cleanly, the health endpoint responds, and `ruff check . && mypy src/ && pytest -x -q` all exit zero. No business logic is implemented here — only the skeleton that all subsequent tasks build on.

## Usage

This task is a prerequisite for all other tasks. The `docker compose up -d` command starts all infrastructure services. The `rag-api` container starts after its dependencies are healthy (Postgres, Redis, MinIO, Qdrant, Keycloak). Developers run `uv run uvicorn src.main:app --reload` locally, with services provided by Docker Compose.

**Inputs:** `.env` file (copied from `.env.example`), Docker Engine running.

**Outputs:** Running FastAPI app at `http://localhost:8000`, health check at `GET /health`, metrics at `GET /metrics`.

## Tech Stack

- **Python 3.12** — minimum version specified in `pyproject.toml` (`requires-python = ">=3.12"`)
- **uv** — package and virtualenv manager; lockfile committed (`uv.lock`)
- **FastAPI 0.115+** — async web framework; chosen for native async support, OpenAPI generation, dependency injection
- **uvicorn[standard]** — ASGI server; `standard` extras provide `uvloop` + `httptools` for performance
- **pydantic-settings 2.x** — environment-based configuration with `.env` file support and type validation
- **structlog** — structured JSON logging; chosen over stdlib `logging` for machine-readable output compatible with log aggregators (Loki, ELK)
- **prometheus-fastapi-instrumentator** — auto-instruments FastAPI for Prometheus metrics with zero boilerplate
- **ruff** — linter and formatter (replaces flake8 + black + isort); configured to line-length 100
- **mypy 1.x** — static type checker; strict mode for `src/`
- **Docker Compose v2** — local environment; all services defined per `docs/architecture.md §18`

## Database Patterns

No database migrations in this task. SQLAlchemy `AsyncEngine` is created here but not used for DDL. The engine is configured and exposed as a dependency-injectable session factory.

```python
# src/core/database.py
from collections.abc import AsyncGenerator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.config import settings

engine = create_async_engine(
    str(settings.DATABASE_URL),
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_pre_ping=True,          # Verify connections before use
    echo=settings.DB_ECHO_SQL,   # Only True in local DEBUG mode
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)

async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an async database session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
```

The `DATABASE_URL` must use the `postgresql+asyncpg://` scheme. The `sqlite+aiosqlite://` scheme is supported for local unit tests only (never for integration tests that touch partitioned tables).

## API Contracts

### `GET /health`

Returns overall service health. Called by Docker Compose health check and load balancers.

```python
class HealthResponse(BaseModel):
    status: Literal["ok", "degraded", "down"]
    version: str
    checks: dict[str, Literal["ok", "error"]]
```

**Response 200:**
```json
{
  "status": "ok",
  "version": "0.1.0",
  "checks": {
    "postgres": "ok",
    "redis": "ok",
    "minio": "ok",
    "qdrant": "ok"
  }
}
```

If any check fails, `status` becomes `"degraded"` (not `"down"` — the API is still running). Always returns HTTP 200 so Docker health checks do not restart the container due to a downstream blip.

### `GET /health/live`

Liveness probe — returns 200 if the process is alive (no dependency checks). Used by Kubernetes `livenessProbe`.

### `GET /health/ready`

Readiness probe — checks all required dependencies (Postgres, Redis). Used by Kubernetes `readinessProbe`. Returns 503 if not ready.

### `GET /metrics`

Prometheus metrics in text exposition format 0.0.4. Exposed on internal network only (not through reverse proxy in production). Provided automatically by `prometheus-fastapi-instrumentator`.

## Architecture — SOLID & DRY

### Application Factory Pattern

```python
# src/main.py
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

from src.api.routers import health
from src.core.config import settings
from src.core.logging import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup and shutdown lifecycle management."""
    configure_logging()
    # Startup: initialize connection pools, verify connectivity
    from src.core.database import engine
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: None)  # warm up pool
    yield
    # Shutdown: close pools gracefully
    await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="RAG Platform API",
        version=settings.APP_VERSION,
        docs_url="/docs" if settings.ENVIRONMENT != "production" else None,
        redoc_url=None,
    )
    Instrumentator().instrument(app).expose(app, endpoint="/metrics")
    app.include_router(health.router)
    return app


app = create_app()
```

### Settings Model

```python
# src/core/config.py
from pydantic import AnyUrl, PostgresDsn, RedisDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Application
    APP_VERSION: str = "0.1.0"
    ENVIRONMENT: str = "development"  # development | staging | production
    DEBUG: bool = False
    SECRET_KEY: str  # No default; must be set in env

    # Database
    DATABASE_URL: PostgresDsn
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_ECHO_SQL: bool = False

    # Redis
    REDIS_URL: RedisDsn

    # MinIO
    MINIO_ENDPOINT: str   # host:port, no scheme
    MINIO_ACCESS_KEY: str
    MINIO_SECRET_KEY: str
    MINIO_USE_TLS: bool = False

    # Qdrant
    QDRANT_URL: AnyUrl
    QDRANT_API_KEY: str | None = None  # None for unauthenticated local instance

    # Keycloak
    KEYCLOAK_BASE_URL: str   # e.g., http://keycloak:8080
    KEYCLOAK_REALM: str      # e.g., rag-platform
    KEYCLOAK_CLIENT_ID: str  # e.g., rag-api
    KEYCLOAK_AUDIENCE: str   # JWT aud claim to validate

    # LLM / Embedding
    LLM_BASE_URL: str        # e.g., http://gpu-host:11434/v1
    EMBEDDING_BASE_URL: str
    LLM_API_KEY: str = "ollama"

    # Ingest
    INGEST_NODE_MAX_RETRIES: int = 3
    INGEST_PRESIGNED_URL_TTL_SECONDS: int = 300  # 5 minutes max

    # Langfuse
    LANGFUSE_PUBLIC_KEY: str | None = None
    LANGFUSE_SECRET_KEY: str | None = None
    LANGFUSE_HOST: str = "http://langfuse:3030"

    @field_validator("INGEST_PRESIGNED_URL_TTL_SECONDS")
    @classmethod
    def validate_ttl(cls, v: int) -> int:
        if v > 300:
            raise ValueError("Presigned URL TTL must not exceed 300 seconds (5 minutes)")
        return v


settings = Settings()  # type: ignore[call-arg]  # env provides required fields
```

**Hard rule:** `settings` is a module-level singleton. Never instantiate `Settings()` in test code directly — override via `monkeypatch.setenv()` or the `override_settings` fixture. Never pass connection strings as function arguments; always read from `settings`.

### Logging Setup

```python
# src/core/logging.py
import logging
import sys

import structlog


def configure_logging(*, level: str = "INFO") -> None:
    """Configure structlog for JSON output in production, console in development."""
    shared_processors: list[structlog.types.Processor] = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=True,
    )
```

**Security constraint:** Application logs MUST NOT contain: document text, prompt content, LLM responses, or any value from `pii_flags`. Log identifiers (IDs, status codes, counts) only. This is enforced by code review — structlog processors that redact structured fields are added per-service.

## Implementation Steps

1. **Initialize repository structure**
   - Create `src/`, `tests/`, `docs/`, `tasks/`, `.claude/` directories
   - Add `.gitignore` for Python (virtualenvs, `__pycache__`, `.env`, `*.pyc`, `.mypy_cache`, `.ruff_cache`)
   - Create `src/__init__.py` and `src/api/__init__.py`, `src/core/__init__.py`, etc.

2. **Configure `pyproject.toml` with `uv`**
   ```toml
   [project]
   name = "rag-platform"
   version = "0.1.0"
   requires-python = ">=3.12"
   dependencies = [
       "fastapi>=0.115",
       "uvicorn[standard]>=0.32",
       "pydantic>=2.9",
       "pydantic-settings>=2.6",
       "sqlalchemy>=2.0",
       "asyncpg>=0.30",
       "alembic>=1.14",
       "structlog>=24.4",
       "prometheus-fastapi-instrumentator>=7.0",
       "httpx>=0.27",
       "python-jose[cryptography]>=3.3",
       "minio>=7.2",
       "redis>=5.2",
       "qdrant-client>=1.12",
       "langfuse>=2.0",
   ]

   [tool.uv]
   dev-dependencies = [
       "pytest>=8.3",
       "pytest-asyncio>=0.24",
       "pytest-cov>=6.0",
       "pytest-mock>=3.14",
       "mypy>=1.12",
       "ruff>=0.7",
       "testcontainers>=4.8",
       "factory-boy>=3.3",
       "python-jose[cryptography]>=3.3",
   ]

   [tool.ruff]
   line-length = 100
   target-version = "py312"
   select = ["E", "F", "I", "N", "UP", "B", "SIM"]

   [tool.mypy]
   python_version = "3.12"
   strict = true
   plugins = ["pydantic.mypy"]

   [tool.pytest.ini_options]
   asyncio_mode = "auto"
   testpaths = ["tests"]
   markers = [
       "integration: requires testcontainers (Docker)",
       "tenant_isolation: blocker — must pass before merge",
       "auth: JWT/RBAC security tests",
   ]
   ```

3. **Create `src/core/config.py`** — `Settings` class as shown above

4. **Create `src/core/database.py`** — engine, session factory, `get_db_session` dependency

5. **Create `src/core/logging.py`** — `configure_logging()` function

6. **Create `src/core/exceptions.py`** — domain exception hierarchy

   ```python
   # src/core/exceptions.py
   class RAGPlatformError(Exception):
       """Base exception for all domain errors."""

   class NotFoundError(RAGPlatformError):
       """Resource not found (maps to 404)."""

   class PermissionDeniedError(RAGPlatformError):
       """Authorization failure (maps to 403)."""

   class ConflictError(RAGPlatformError):
       """Resource conflict, e.g., duplicate sha256 (maps to 409)."""

   class ValidationError(RAGPlatformError):
       """Domain-level validation failure (maps to 422)."""

   class TenantIsolationError(RAGPlatformError):
       """Attempted cross-tenant access — always 403, never 404."""
   ```

7. **Create `src/main.py`** — application factory as shown above

8. **Create `src/api/routers/health.py`** — health check endpoints

9. **Create `src/api/exception_handlers.py`** — map domain exceptions to HTTP responses

   ```python
   # src/api/exception_handlers.py
   from fastapi import Request
   from fastapi.responses import JSONResponse
   from src.core.exceptions import (
       ConflictError, NotFoundError, PermissionDeniedError,
       TenantIsolationError, ValidationError,
   )

   def register_exception_handlers(app: "FastAPI") -> None:
       @app.exception_handler(NotFoundError)
       async def not_found_handler(req: Request, exc: NotFoundError) -> JSONResponse:
           return JSONResponse(status_code=404, content={"detail": str(exc)})

       @app.exception_handler(PermissionDeniedError)
       async def permission_denied_handler(req: Request, exc: PermissionDeniedError) -> JSONResponse:
           return JSONResponse(status_code=403, content={"detail": str(exc)})

       @app.exception_handler(TenantIsolationError)
       async def tenant_isolation_handler(req: Request, exc: TenantIsolationError) -> JSONResponse:
           # Always 403, never 404 — leaking existence is also a violation
           return JSONResponse(status_code=403, content={"detail": "Access denied"})

       @app.exception_handler(ConflictError)
       async def conflict_handler(req: Request, exc: ConflictError) -> JSONResponse:
           return JSONResponse(status_code=409, content={"detail": str(exc)})

       @app.exception_handler(ValidationError)
       async def validation_handler(req: Request, exc: ValidationError) -> JSONResponse:
           return JSONResponse(status_code=422, content={"detail": str(exc)})
   ```

10. **Create `.env.example`** — all required variables with empty/example values, clearly marked

    ```
    # Application
    APP_VERSION=0.1.0
    ENVIRONMENT=development
    DEBUG=false
    SECRET_KEY=CHANGE_ME_generate_with_openssl_rand_hex_32

    # Database
    DATABASE_URL=postgresql+asyncpg://raguser:ragpass@localhost:5432/ragdb
    DB_POOL_SIZE=10
    DB_MAX_OVERFLOW=20
    DB_ECHO_SQL=false

    # Redis
    REDIS_URL=redis://localhost:6379/0

    # MinIO
    MINIO_ENDPOINT=localhost:9000
    MINIO_ACCESS_KEY=minioadmin
    MINIO_SECRET_KEY=CHANGE_ME_minio_secret
    MINIO_USE_TLS=false

    # Qdrant
    QDRANT_URL=http://localhost:6333
    QDRANT_API_KEY=

    # Keycloak
    KEYCLOAK_BASE_URL=http://localhost:8080
    KEYCLOAK_REALM=rag-platform
    KEYCLOAK_CLIENT_ID=rag-api
    KEYCLOAK_AUDIENCE=rag-api

    # LLM / Embedding (GPU host)
    LLM_BASE_URL=http://localhost:11434/v1
    EMBEDDING_BASE_URL=http://localhost:11434/v1
    LLM_API_KEY=ollama

    # Langfuse (optional — leave empty to disable tracing)
    LANGFUSE_PUBLIC_KEY=
    LANGFUSE_SECRET_KEY=
    LANGFUSE_HOST=http://localhost:3030
    ```

11. **Create `docker-compose.yml`** per `docs/architecture.md §18`

    Key constraints to implement:
    - Three networks: `proxy_net`, `internal_net`, `monitoring_net`
    - Only Traefik binds host ports (`80:80`, `443:443`)
    - All health checks as specified in the architecture doc
    - All `depends_on` with `condition: service_healthy`
    - Named volumes for all stateful services
    - Resource limits per §18 table
    - Services on `monitoring_net` only expose metrics ports internally

    Minimum services: `postgres`, `redis`, `minio`, `qdrant`, `keycloak`, `langfuse`, `rag-api`, `ingest-worker`, `openwebui`, `traefik`, `prometheus`, `grafana`.

12. **Create `alembic.ini` and `src/db/migrations/env.py`** — Alembic setup pointing at `src/db/models/` (actual models added in TASK-002)

13. **Configure ruff and mypy** in `pyproject.toml` as shown in step 2

14. **Create `tests/conftest.py`** with session-scoped fixtures:
    - `override_settings` fixture using `monkeypatch`
    - Test database URL using `sqlite+aiosqlite:///:memory:` (unit tests only)
    - `async_client` fixture using `httpx.AsyncClient` with `ASGITransport`

15. **Run validation:** `ruff check --fix . && ruff format . && mypy src/ && pytest -x -q`

## Security Checklist

- `.env` is in `.gitignore` and MUST NOT be committed; only `.env.example` is committed
- `SECRET_KEY` has no default value — app fails to start without it
- `docs_url` is disabled in production (`ENVIRONMENT=production`)
- `/metrics` endpoint is registered at the FastAPI level but NOT routed through Traefik (blocked at reverse proxy layer for external access)
- No sensitive values logged: `configure_logging()` must not include settings dump
- `DB_ECHO_SQL` defaults to `False` — SQL is never logged in production (would expose query parameters)
- Presigned URL TTL validated to never exceed 300 seconds (enforced in `Settings` validator)
- Docker Compose: no service runs as root (use `user: "1000:1000"` for application containers)
- MinIO secret key has no default value — startup fails if not set

## Terms of Use (relevant constraints)

- All infrastructure is self-hosted on-premises; no data leaves the network
- TLS termination at Traefik is mandatory for any deployment exposed to users (even internal)
- GPU host connection is over private VLAN only; no internet-routable path
- Keycloak is the sole authentication authority; no bypass credentials in the application

## Tests

References TASK-016 test categories. Specific test cases for this task:

**`tests/unit/test_config.py`**
- Settings fail to instantiate when `SECRET_KEY` is missing
- Settings fail to instantiate when `DATABASE_URL` is missing
- `INGEST_PRESIGNED_URL_TTL_SECONDS > 300` raises `ValidationError` at settings init time
- All optional settings have correct defaults

**`tests/unit/test_health.py`**
- `GET /health` returns 200 with `{"status": "ok"}` when all checks pass
- `GET /health` returns 200 with `{"status": "degraded"}` when Postgres is unreachable (mocked)
- `GET /health/live` always returns 200
- `GET /health/ready` returns 503 when Postgres is unreachable

**`tests/unit/test_exception_handlers.py`**
- `NotFoundError` maps to 404
- `PermissionDeniedError` maps to 403
- `TenantIsolationError` maps to 403 with generic message (not the exception detail)
- `ConflictError` maps to 409
- `ValidationError` maps to 422

## Definition of Done

- [ ] `pyproject.toml` with `uv` configuration present and `uv lock` runs cleanly
- [ ] `src/core/config.py` with all settings validated; startup fails on missing required vars
- [ ] `src/main.py` application factory created
- [ ] `GET /health` returns `{"status": "ok"}` with sub-service checks
- [ ] `docker-compose.yml` starts all services cleanly (`docker compose up -d && docker compose ps` shows all healthy)
- [ ] All three networks configured (`proxy_net`, `internal_net`, `monitoring_net`)
- [ ] `.env.example` committed with all required variables, no real secrets
- [ ] `alembic.ini` and `env.py` configured (no migrations yet)
- [ ] `ruff check . && ruff format . && mypy src/` all exit zero
- [ ] `pytest -x -q tests/unit/test_config.py tests/unit/test_health.py` passes
- [ ] No secrets in any committed file

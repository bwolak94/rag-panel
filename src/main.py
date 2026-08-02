"""FastAPI application factory."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator
from qdrant_client import AsyncQdrantClient

from src.api.exception_handlers import register_exception_handlers
from src.api.middleware.tos_check import TosCheckMiddleware
from src.api.routers import (
    admin_review,
    chat,
    collections,
    conversations,
    documents,
    health,
    messages,
    models,
    pipelines,
    tenants,
    tos,
)
from src.api.routers.admin_audit_log import router as admin_audit_log_router
from src.api.routers.platform_onboarding import router as platform_onboarding_router
from src.api.routers.webhooks import webhook_router
from src.core.config import settings
from src.core.langfuse_client import initialize_langfuse, shutdown_langfuse
from src.core.logging import configure_logging

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup and shutdown lifecycle management."""
    logger.info("startup", version=settings.APP_VERSION, environment=settings.ENVIRONMENT)

    # Initialize Langfuse tracing (no-op when keys are absent)
    initialize_langfuse()

    # Warm up Postgres connection pool
    from src.core.database import AsyncSessionLocal, engine

    async with engine.begin() as conn:
        await conn.run_sync(lambda _: None)

    # Seed default model registry entries
    from src.db.seed import seed_default_models

    async with AsyncSessionLocal() as seed_session:
        await seed_default_models(seed_session)

    # Initialize Qdrant client
    app.state.qdrant_client = AsyncQdrantClient(
        url=str(settings.QDRANT_URL),
        api_key=settings.QDRANT_API_KEY,
        timeout=10,
    )

    yield

    # Graceful shutdown
    logger.info("shutdown")
    # shutdown_langfuse() calls blocking flush()/shutdown() — offload to thread pool
    # so the async event loop is not stalled during Langfuse HTTP flush.
    await asyncio.to_thread(shutdown_langfuse)
    await engine.dispose()
    await app.state.qdrant_client.close()


def create_app() -> FastAPI:
    """Application factory — creates and configures the FastAPI app."""
    # Configure logging early so module-level loggers get the right factory before caching
    configure_logging(level="DEBUG" if settings.DEBUG else "INFO")

    app = FastAPI(
        title="RAG Platform API",
        version=settings.APP_VERSION,
        description="Self-hosted multi-tenant RAG platform",
        docs_url="/docs" if settings.ENVIRONMENT != "production" else None,
        redoc_url=None,
        lifespan=lifespan,
    )

    # Prometheus metrics at /metrics (blocked at reverse proxy in production)
    Instrumentator().instrument(app).expose(app, endpoint="/metrics")

    # Exception handlers
    register_exception_handlers(app)

    # Routers
    app.include_router(health.router)
    app.include_router(collections.router, prefix="/api/v1")
    app.include_router(tenants.router, prefix="/api/v1")
    app.include_router(documents.router)  # /api/v1/documents
    app.include_router(chat.router)  # /v1/models, /v1/chat/completions
    app.include_router(conversations.router, prefix="/api/v1")  # /api/v1/conversations
    app.include_router(messages.router, prefix="/api/v1")  # /api/v1/messages/{id}/feedback
    app.include_router(models.router, prefix="/api/v1")  # /api/v1/models
    app.include_router(pipelines.router, prefix="/api/v1")  # /api/v1/pipelines
    app.include_router(webhook_router)  # /internal/minio-webhook
    app.include_router(tos.router, prefix="/api/v1")  # /api/v1/terms, /api/v1/tenants/.../terms/*
    app.include_router(admin_review.router)  # /api/v1/admin/...
    app.include_router(admin_audit_log_router)  # /api/v1/admin/audit-log
    app.include_router(platform_onboarding_router)  # /api/v1/platform/onboarding

    # ToS check middleware — uses the shared Redis singleton (same pool as routers)
    from src.core.clients.redis_client import get_redis_client
    from src.core.database import AsyncSessionLocal

    app.add_middleware(
        TosCheckMiddleware,
        redis=get_redis_client(),
        session_factory=AsyncSessionLocal,
    )

    return app


app = create_app()

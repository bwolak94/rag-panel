"""FastAPI application factory."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

from src.api.exception_handlers import register_exception_handlers
from src.api.routers import collections, health, tenants
from src.core.config import settings
from src.core.logging import configure_logging

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup and shutdown lifecycle management."""
    configure_logging(level="DEBUG" if settings.DEBUG else "INFO")
    logger.info("startup", version=settings.APP_VERSION, environment=settings.ENVIRONMENT)

    # Warm up Postgres connection pool
    from src.core.database import engine

    async with engine.begin() as conn:
        await conn.run_sync(lambda _: None)

    yield

    # Graceful shutdown
    logger.info("shutdown")
    await engine.dispose()


def create_app() -> FastAPI:
    """Application factory — creates and configures the FastAPI app."""
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

    return app


app = create_app()

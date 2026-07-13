"""Map domain exceptions to HTTP responses.

All domain errors are caught here — never let RAGPlatformError
propagate to FastAPI's default handler.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request
from fastapi.responses import JSONResponse

from src.core.exceptions import (
    AuthenticationError,
    ConflictError,
    DomainValidationError,
    NotFoundError,
    PermissionDeniedError,
    ServiceUnavailableError,
    TenantIsolationError,
)

if TYPE_CHECKING:
    from fastapi import FastAPI


def register_exception_handlers(app: FastAPI) -> None:
    """Register all domain exception → HTTP response mappings."""

    @app.exception_handler(NotFoundError)
    async def not_found_handler(req: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(AuthenticationError)
    async def auth_handler(req: Request, exc: AuthenticationError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content={"detail": "Authentication required"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.exception_handler(PermissionDeniedError)
    async def permission_denied_handler(req: Request, exc: PermissionDeniedError) -> JSONResponse:
        # Generic message — never forward exc detail as it may contain resource identifiers
        return JSONResponse(status_code=403, content={"detail": "Access denied"})

    @app.exception_handler(TenantIsolationError)
    async def tenant_isolation_handler(req: Request, exc: TenantIsolationError) -> JSONResponse:
        # Always 403, never expose cross-tenant resource existence
        return JSONResponse(status_code=403, content={"detail": "Access denied"})

    @app.exception_handler(ConflictError)
    async def conflict_handler(req: Request, exc: ConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(DomainValidationError)
    async def validation_handler(req: Request, exc: DomainValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(ServiceUnavailableError)
    async def service_unavailable_handler(
        req: Request, exc: ServiceUnavailableError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": str(exc)},
            headers={"Retry-After": "60"},
        )

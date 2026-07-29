"""Tests for domain exception → HTTP response mapping."""

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRouter
from httpx import AsyncClient

from src.core.exceptions import (
    AuthenticationError,
    ConflictError,
    DomainValidationError,
    NotFoundError,
    PermissionDeniedError,
    ServiceUnavailableError,
    TenantIsolationError,
)


def _app_with_exception_routes() -> FastAPI:
    """Create a minimal app that raises each domain exception on demand."""
    from src.api.exception_handlers import register_exception_handlers
    from src.core.logging import configure_logging

    configure_logging()
    app = FastAPI()
    register_exception_handlers(app)
    router = APIRouter()

    @router.get("/raise/not-found")
    async def raise_not_found() -> None:
        raise NotFoundError("thing not found")

    @router.get("/raise/auth")
    async def raise_auth() -> None:
        raise AuthenticationError("bad token")

    @router.get("/raise/permission")
    async def raise_permission() -> None:
        raise PermissionDeniedError("not allowed")

    @router.get("/raise/tenant-isolation")
    async def raise_tenant() -> None:
        raise TenantIsolationError("cross-tenant access")

    @router.get("/raise/conflict")
    async def raise_conflict() -> None:
        raise ConflictError("duplicate")

    @router.get("/raise/validation")
    async def raise_validation() -> None:
        raise DomainValidationError("bad value")

    @router.get("/raise/service-unavailable")
    async def raise_service_unavailable() -> None:
        raise ServiceUnavailableError("LLM down")

    app.include_router(router)
    return app


@pytest.fixture
async def exc_client() -> AsyncClient:
    from httpx import ASGITransport

    async with AsyncClient(
        transport=ASGITransport(app=_app_with_exception_routes()),
        base_url="http://test",
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_not_found_returns_404(exc_client: AsyncClient) -> None:
    resp = await exc_client.get("/raise/not-found")
    assert resp.status_code == 404
    # Generic message — must NOT expose exception detail (UUID/resource existence oracle)
    assert resp.json()["detail"] == "Not found"


@pytest.mark.asyncio
async def test_auth_error_returns_401(exc_client: AsyncClient) -> None:
    resp = await exc_client.get("/raise/auth")
    assert resp.status_code == 401
    assert "WWW-Authenticate" in resp.headers


@pytest.mark.asyncio
async def test_permission_denied_returns_403(exc_client: AsyncClient) -> None:
    resp = await exc_client.get("/raise/permission")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_tenant_isolation_returns_403_generic(exc_client: AsyncClient) -> None:
    resp = await exc_client.get("/raise/tenant-isolation")
    assert resp.status_code == 403
    # Must NOT expose the original exception message (cross-tenant existence leak)
    assert "cross-tenant" not in resp.json()["detail"]
    assert resp.json()["detail"] == "Access denied"


@pytest.mark.asyncio
async def test_conflict_returns_409(exc_client: AsyncClient) -> None:
    resp = await exc_client.get("/raise/conflict")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_domain_validation_returns_422(exc_client: AsyncClient) -> None:
    resp = await exc_client.get("/raise/validation")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_service_unavailable_returns_503_with_retry_after(
    exc_client: AsyncClient,
) -> None:
    resp = await exc_client.get("/raise/service-unavailable")
    assert resp.status_code == 503
    assert resp.headers.get("Retry-After") == "60"


@pytest.mark.asyncio
async def test_conversation_not_found_returns_404_with_code(exc_client: AsyncClient) -> None:
    """ConversationNotFoundError → HTTP 404 with structured code detail."""
    from fastapi import FastAPI
    from fastapi.routing import APIRouter
    from httpx import ASGITransport
    from httpx import AsyncClient as _AsyncClient

    from src.api.exception_handlers import register_exception_handlers
    from src.core.exceptions import ConversationNotFoundError
    from src.core.logging import configure_logging

    configure_logging()
    app2 = FastAPI()
    register_exception_handlers(app2)
    r2 = APIRouter()

    @r2.get("/raise/conversation-not-found")
    async def raise_conv_not_found() -> None:
        raise ConversationNotFoundError("CONVERSATION_NOT_FOUND")

    app2.include_router(r2)

    async with _AsyncClient(
        transport=ASGITransport(app=app2), base_url="http://test"
    ) as client:
        resp = await client.get("/raise/conversation-not-found")

    assert resp.status_code == 404
    body = resp.json()
    assert body["detail"] == {"code": "CONVERSATION_NOT_FOUND"}


@pytest.mark.asyncio
async def test_conversation_not_found_is_subtype_of_not_found() -> None:
    """ConversationNotFoundError must be a subtype of NotFoundError (inherits 404 mapping)."""
    from src.core.exceptions import ConversationNotFoundError, NotFoundError

    exc = ConversationNotFoundError("test")
    assert isinstance(exc, NotFoundError)

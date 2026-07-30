"""Unit tests for ModelService client-builder and validate_model_reachable methods.

No real DB or HTTP connections are used — repositories and httpx are fully mocked.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.exceptions import NotFoundError, PermissionDeniedError
from src.domain.auth import UserContext

TENANT_ID = uuid.uuid4()
USER_ID = uuid.uuid4()
MODEL_ID = uuid.uuid4()
_NOW = datetime.now(tz=UTC)


def _make_ctx(permissions: frozenset[str] | None = None) -> UserContext:
    return UserContext(
        user_id=USER_ID,
        keycloak_sub="sub-123",
        email="admin@clinic.example",
        display_name="Admin",
        tenant_id=TENANT_ID,
        roles=frozenset({"admin"}),
        permissions=permissions or frozenset({"admin:models"}),
        allowed_collection_ids=frozenset(),
        writable_collection_ids=frozenset(),
    )


def _make_record(model_type: str, endpoint_url: str = "http://gpu:11434/v1") -> MagicMock:
    """Build a mock ModelsRegistry ORM record."""
    record = MagicMock()
    record.id = MODEL_ID
    record.tenant_id = None  # system-wide
    record.name = "test-model"
    record.type = model_type
    record.provider = "ollama"
    record.endpoint_url = endpoint_url
    record.model_id = "test/model"
    record.params = {}
    record.allowed_roles = []
    record.is_active = True
    record.created_at = _NOW
    record.updated_at = _NOW
    return record


def _make_service(visible_record: Any) -> Any:
    """Return a ModelService with its repository mocked to return visible_record."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from src.domain.model_service import ModelService

    session = AsyncMock(spec=AsyncSession)
    service = ModelService(session)
    service._repo = MagicMock()
    service._repo.get_visible_by_id = AsyncMock(return_value=visible_record)
    return service


# ---------------------------------------------------------------------------
# get_llm_client
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_llm_client_returns_async_openai() -> None:
    """Happy path: llm-type model → AsyncOpenAI client with correct base_url."""
    from openai import AsyncOpenAI

    record = _make_record("llm", endpoint_url="http://gpu:11434/v1")
    service = _make_service(record)
    ctx = _make_ctx()

    client = await service.get_llm_client(MODEL_ID, ctx)

    assert isinstance(client, AsyncOpenAI)
    # AsyncOpenAI stores base_url as a httpx.URL; str() gives us the URL string.
    assert "gpu" in str(client.base_url)


@pytest.mark.asyncio
async def test_get_llm_client_raises_not_found_when_record_missing() -> None:
    """Missing model raises NotFoundError."""
    service = _make_service(None)
    ctx = _make_ctx()

    with pytest.raises(NotFoundError):
        await service.get_llm_client(MODEL_ID, ctx)


@pytest.mark.asyncio
async def test_get_llm_client_raises_not_found_when_inactive() -> None:
    """Inactive model raises NotFoundError."""
    record = _make_record("llm")
    record.is_active = False
    service = _make_service(record)
    ctx = _make_ctx()

    with pytest.raises(NotFoundError):
        await service.get_llm_client(MODEL_ID, ctx)


@pytest.mark.asyncio
async def test_get_llm_client_raises_permission_denied_for_embedding_type() -> None:
    """Requesting an LLM client for an embedding model raises PermissionDeniedError."""
    record = _make_record("embedding")
    service = _make_service(record)
    ctx = _make_ctx()

    with pytest.raises(PermissionDeniedError):
        await service.get_llm_client(MODEL_ID, ctx)


# ---------------------------------------------------------------------------
# get_embedding_client
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_embedding_client_returns_async_openai() -> None:
    """Happy path: embedding-type model → AsyncOpenAI client with correct base_url."""
    from openai import AsyncOpenAI

    record = _make_record("embedding", endpoint_url="http://gpu:11434/v1")
    service = _make_service(record)
    ctx = _make_ctx()

    client = await service.get_embedding_client(MODEL_ID, ctx)

    assert isinstance(client, AsyncOpenAI)
    assert "gpu" in str(client.base_url)


@pytest.mark.asyncio
async def test_get_embedding_client_raises_not_found_when_record_missing() -> None:
    """Missing model raises NotFoundError."""
    service = _make_service(None)
    ctx = _make_ctx()

    with pytest.raises(NotFoundError):
        await service.get_embedding_client(MODEL_ID, ctx)


@pytest.mark.asyncio
async def test_get_embedding_client_raises_permission_denied_for_llm_type() -> None:
    """Requesting an embedding client for an LLM model raises PermissionDeniedError."""
    record = _make_record("llm")
    service = _make_service(record)
    ctx = _make_ctx()

    with pytest.raises(PermissionDeniedError):
        await service.get_embedding_client(MODEL_ID, ctx)


# ---------------------------------------------------------------------------
# validate_model_reachable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_model_reachable_returns_true_on_200() -> None:
    """Endpoint returns 200 → validate_model_reachable returns True."""
    record = _make_record("llm", endpoint_url="http://gpu:11434/v1")
    service = _make_service(record)
    ctx = _make_ctx()

    mock_response = MagicMock()
    mock_response.is_success = True

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_async_client_cm = MagicMock()
    mock_async_client_cm.__aenter__ = AsyncMock(return_value=mock_client)
    mock_async_client_cm.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_async_client_cm):
        result = await service.validate_model_reachable(MODEL_ID, ctx)

    assert result is True
    mock_client.get.assert_called_once_with("http://gpu:11434/v1/models")


@pytest.mark.asyncio
async def test_validate_model_reachable_returns_false_on_timeout() -> None:
    """httpx timeout → validate_model_reachable returns False without raising."""
    import httpx

    record = _make_record("llm", endpoint_url="http://gpu:11434/v1")
    service = _make_service(record)
    ctx = _make_ctx()

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
    mock_async_client_cm = MagicMock()
    mock_async_client_cm.__aenter__ = AsyncMock(return_value=mock_client)
    mock_async_client_cm.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_async_client_cm):
        result = await service.validate_model_reachable(MODEL_ID, ctx)

    assert result is False


@pytest.mark.asyncio
async def test_validate_model_reachable_returns_false_on_500() -> None:
    """Endpoint returns 500 → validate_model_reachable returns False."""
    record = _make_record("llm", endpoint_url="http://gpu:11434/v1")
    service = _make_service(record)
    ctx = _make_ctx()

    mock_response = MagicMock()
    mock_response.is_success = False  # 500 is not success

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_async_client_cm = MagicMock()
    mock_async_client_cm.__aenter__ = AsyncMock(return_value=mock_client)
    mock_async_client_cm.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_async_client_cm):
        result = await service.validate_model_reachable(MODEL_ID, ctx)

    assert result is False


@pytest.mark.asyncio
async def test_validate_model_reachable_raises_not_found_when_record_missing() -> None:
    """Missing model raises NotFoundError before any HTTP call is made."""
    service = _make_service(None)
    ctx = _make_ctx()

    with pytest.raises(NotFoundError):
        await service.validate_model_reachable(MODEL_ID, ctx)


@pytest.mark.asyncio
async def test_validate_model_reachable_strips_trailing_slash_from_url() -> None:
    """Trailing slash in endpoint_url is stripped before appending /models."""
    record = _make_record("llm", endpoint_url="http://gpu:11434/v1/")
    service = _make_service(record)
    ctx = _make_ctx()

    mock_response = MagicMock()
    mock_response.is_success = True

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_async_client_cm = MagicMock()
    mock_async_client_cm.__aenter__ = AsyncMock(return_value=mock_client)
    mock_async_client_cm.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=mock_async_client_cm):
        await service.validate_model_reachable(MODEL_ID, ctx)

    # Must not produce double slash: /v1//models
    call_url: str = mock_client.get.call_args[0][0]
    assert "//" not in call_url.split("://", 1)[-1]
    assert call_url.endswith("/models")

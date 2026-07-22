"""Domain exception hierarchy.

All domain exceptions are mapped to HTTP responses centrally in
`src/api/exception_handlers.py`. Never catch bare `Exception` — use
these typed exceptions instead.
"""


class RAGPlatformError(Exception):
    """Base exception for all domain errors."""


class NotFoundError(RAGPlatformError):
    """Resource not found. Maps to HTTP 404."""


class PermissionDeniedError(RAGPlatformError):
    """Authorization failure. Maps to HTTP 403."""


class ConflictError(RAGPlatformError):
    """Resource conflict, e.g., duplicate sha256. Maps to HTTP 409."""


class DomainValidationError(RAGPlatformError):
    """Domain-level validation failure. Maps to HTTP 422."""


class TenantIsolationError(RAGPlatformError):
    """Attempted cross-tenant access.

    Always maps to HTTP 403, never 404 — leaking resource existence
    across tenant boundaries is itself a security violation.
    """


class AuthenticationError(RAGPlatformError):
    """JWT verification or authentication failure. Maps to HTTP 401."""


class ServiceUnavailableError(RAGPlatformError):
    """Upstream service (LLM, Qdrant) unavailable. Maps to HTTP 503."""


class LLMUnavailableError(ServiceUnavailableError):
    """LLM unreachable after retries. Maps to HTTP 503 with Retry-After: 60."""


class IngestNodeError(RAGPlatformError):
    """Raised by ingest graph nodes on recoverable or unrecoverable pipeline errors.

    Caught by EventProcessor: if retriable, schedules backoff; if not, routes to DLQ.
    Must never contain document content, PII, or raw bytes in the message.
    """

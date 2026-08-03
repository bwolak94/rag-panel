"""Domain exception hierarchy.

All domain exceptions are mapped to HTTP responses centrally in
`src/api/exception_handlers.py`. Never catch bare `Exception` — use
these typed exceptions instead.
"""


class RAGPlatformError(Exception):
    """Base exception for all domain errors."""


class NotFoundError(RAGPlatformError):
    """Resource not found. Maps to HTTP 404."""


class ConversationNotFoundError(NotFoundError):
    """Conversation not found or not owned by the requesting user. Maps to HTTP 404.

    Raised by ChatService.get_or_create_conversation() when the conversation_id
    does not exist, belongs to another tenant, belongs to another user, or is deleted.
    Kept as a subtype of NotFoundError so the global handler maps it to 404 without
    any extra registration.
    """


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


class InvalidDocumentStateError(RAGPlatformError):
    """Document is not in the expected state for this operation.

    e.g. approve/reject on a document that is not in needs_review status.
    Maps to HTTP 409.
    """

    def __init__(self, message: str, current_status: str | None = None) -> None:
        super().__init__(message)
        self.current_status = current_status


class IngestNodeError(RAGPlatformError):
    """Raised by ingest graph nodes on recoverable or unrecoverable pipeline errors.

    Caught by EventProcessor: if retriable, schedules backoff; if not, routes to DLQ.
    Must never contain document content, PII, or raw bytes in the message.
    """


class QueryNodeError(RAGPlatformError):
    """Raised by query graph nodes on pipeline errors.

    Caught by ChatService._invoke_graph().
    Must never contain question content, chunk text, or PII in the message.
    """


class QuotaExceededError(RAGPlatformError):
    """Tenant quota exceeded. Maps to HTTP 429 with problem detail.

    Attributes:
        quota_type: Which quota was exceeded (e.g. 'monthly_queries').
        limit: The configured limit value.
        current: The current usage value.
        reset_at: When the quota counter resets (None for non-rolling quotas).
    """

    def __init__(
        self,
        message: str,
        quota_type: str,
        limit: int,
        current: int,
        reset_at: str | None = None,
    ) -> None:
        super().__init__(message)
        self.quota_type = quota_type
        self.limit = limit
        self.current = current
        self.reset_at = reset_at

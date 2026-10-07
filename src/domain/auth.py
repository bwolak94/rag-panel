"""UserContext — immutable identity object for the current request.

Domain-level auth checks (require_collection_read/write, assert_tenant_owns_resource)
live here so RetrievalService and graph nodes can import them without crossing into src/api/.
"""

from dataclasses import dataclass, field
from uuid import UUID

import structlog

from src.core.exceptions import PermissionDeniedError, TenantIsolationError

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class UserContext:
    """
    Immutable user context extracted from validated JWT and DB lookup.
    This is the sole source of identity for all business logic.
    Never extracted from request body or query parameters.
    """

    user_id: UUID
    keycloak_sub: str
    email: str
    display_name: str
    tenant_id: UUID
    roles: frozenset[str]  # Role names within this tenant
    permissions: frozenset[str]  # Permission codes (e.g., "documents:upload")
    allowed_collection_ids: frozenset[UUID]  # Collections readable by user
    writable_collection_ids: frozenset[UUID]  # Collections writable by user
    # Platform-wide public collections readable by this tenant (ADR-020).
    # Populated by AuthRepository from collections.is_public=True rows.
    # Default frozenset() for backward-compat (tests, seeding).
    public_collection_ids: frozenset[UUID] = field(default_factory=frozenset)
    realm_roles: frozenset[str] = field(default_factory=frozenset)  # JWT realm_access.roles

    def __post_init__(self) -> None:
        # Structural invariant: write access implies read access
        if not self.writable_collection_ids <= self.allowed_collection_ids:
            raise ValueError("writable_collection_ids must be a subset of allowed_collection_ids")

    def __repr__(self) -> str:
        # email and keycloak_sub intentionally excluded — GDPR log hygiene
        return (
            f"UserContext(user_id={self.user_id!r}, "
            f"tenant_id={self.tenant_id!r}, "
            f"roles={self.roles!r})"
        )

    def has_permission(self, permission: str) -> bool:
        return permission in self.permissions

    def can_read_collection(self, collection_id: UUID) -> bool:
        return (
            collection_id in self.allowed_collection_ids
            or collection_id in self.public_collection_ids
        )

    def can_write_collection(self, collection_id: UUID) -> bool:
        return collection_id in self.writable_collection_ids


def require_collection_read(collection_id: UUID, ctx: UserContext) -> None:
    """Resource-level check: user must have read access to the given collection.

    Call inside a route handler or graph node after resolving the collection_id.
    Raises PermissionDeniedError (→ HTTP 403) with a generic message — no UUID leak.
    """
    if not ctx.can_read_collection(collection_id):
        raise PermissionDeniedError("Access denied")


def require_collection_write(collection_id: UUID, ctx: UserContext) -> None:
    """Resource-level check: user must have write access to the given collection.

    Raises PermissionDeniedError (→ HTTP 403) with a generic message — no UUID leak.
    """
    if not ctx.can_write_collection(collection_id):
        raise PermissionDeniedError("Access denied")


def assert_tenant_owns_resource(resource_tenant_id: UUID, ctx: UserContext) -> None:
    """Hard tenant isolation check.

    MUST be called before returning or modifying any resource loaded by ID.
    Raises TenantIsolationError (→ HTTP 403) on mismatch — never 404.
    Logs at ERROR level with tenant_isolation_violation event key for incident tracking.
    Note: resource_tenant_id is logged server-side for audit purposes only;
          it is never included in the HTTP response body.
    """
    if resource_tenant_id != ctx.tenant_id:
        logger.error(
            "tenant_isolation_violation",
            ctx_tenant=str(ctx.tenant_id),
            resource_tenant=str(resource_tenant_id),
            user_id=str(ctx.user_id),
        )
        raise TenantIsolationError("Access denied")

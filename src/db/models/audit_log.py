"""AuditLog model — append-only, RANGE-partitioned by month."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.models.base import Base


class AuditLog(Base):
    """
    Append-only audit log. Partitioned RANGE(created_at) — one partition per month.
    Partition names: audit_log_YYYY_MM (e.g., audit_log_2026_07).

    NEVER issue UPDATE or DELETE against this table.
    Retention: minimum 24 months for medical data (GDPR Art. 17 exemption).
    Only the retention_worker may drop old partitions.

    `tenant_id` is nullable: SET NULL on tenant deletion so audit records survive
    for the mandatory 24-month retention period (GDPR Art. 17 exemption for medical data).
    `user_id` is SET NULL on user deletion: anonymises the actor while preserving the audit trail.
    `details` must contain only IDs and metadata — no PII, no document content.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("idx_audit_log_tenant_created", "tenant_id", "created_at"),
        Index("idx_audit_log_user_id", "user_id"),
        Index("idx_audit_log_action", "action"),
        Index("idx_audit_log_resource_type", "resource_type"),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )

    # Composite PK required for Postgres RANGE-partitioned tables:
    # the partition key (created_at) must be part of the primary key.
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # Nullable: SET NULL when a tenant is deleted so logs survive the 24-month retention window.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="SET NULL")
    )
    # Nullable: SET NULL when a user is deleted (GDPR Art. 17 — anonymise actor, keep event).
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(50))
    resource_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    ip: Mapped[str | None] = mapped_column(INET)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )
    # Also part of composite PK — required for Postgres partitioned table.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, primary_key=True
    )

"""TosAcceptance model — audit record of a user accepting a ToS version."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.models.base import Base


class TosAcceptance(Base):
    """Records that a specific user within a tenant accepted a specific ToS version.

    Serves as an immutable audit trail for GDPR compliance.  No `updated_at`
    column is intentional — acceptances are append-only and must never be
    modified after creation.

    `ip_address` is stored as VARCHAR(45) (covers both IPv4 and IPv6) rather
    than the Postgres INET type for maximum portability across drivers.
    """

    __tablename__ = "tos_acceptances"

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "tos_version_id",
            name="uq_tos_acceptances_tenant_version",
        ),
        Index("tos_acceptances_tenant_version", "tenant_id", "tos_version_id"),
        Index("tos_acceptances_tenant_id", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
    )
    tos_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tos_versions.id"),
        nullable=False,
    )
    accepted_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    ip_address: Mapped[str] = mapped_column(String(45), nullable=False)
    user_agent: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    # Relationships (lazy="raise" prevents accidental N+1)
    tos_version: Mapped[TosVersion] = relationship(  # type: ignore[name-defined]  # noqa: F821
        back_populates="acceptances", lazy="raise"
    )

"""TosVersion model — versioned Terms of Service documents."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.models.base import Base, TimestampMixin


class TosVersion(Base, TimestampMixin):
    """A published version of the platform Terms of Service.

    Only one version may have status='active' at a time — enforced by the
    partial unique index `tos_versions_single_active` defined in the migration.

    `created_by` is a plain UUID referencing the system admin who created
    the version. It is NOT a foreign key to the `users` table because the
    platform admin performing initial seeding may not have a user record yet.
    """

    __tablename__ = "tos_versions"

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'active', 'superseded')",
            name="ck_tos_versions_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    version: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'draft'"))
    effective_date: Mapped[datetime] = mapped_column(nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    # Relationships (lazy="raise" prevents accidental N+1)
    acceptances: Mapped[list[TosAcceptance]] = relationship(  # type: ignore[name-defined]  # noqa: F821
        back_populates="tos_version", lazy="raise"
    )

"""OnboardingSession model — stateful multi-step tenant onboarding wizard."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.models.base import Base


class OnboardingSession(Base):
    """Stateful onboarding session managed by a platform-admin.

    Steps 1-5 are tracked via steps_completed JSON.
    Session expires 7 days after creation (enforced by service, not DB).
    """

    __tablename__ = "onboarding_sessions"
    __table_args__ = (
        Index("ix_onboarding_sessions_tenant_id", "tenant_id"),
        Index("ix_onboarding_sessions_status", "status"),
        Index("ix_onboarding_sessions_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'in_progress'")
    )
    current_step: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    steps_completed: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )
    draft_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

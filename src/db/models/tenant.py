"""Tenant model — top-level multi-tenant isolation boundary."""

import uuid
from typing import Any

from sqlalchemy import String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.models.base import Base, TimestampMixin


class Tenant(Base, TimestampMixin):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'active'"), index=True
    )

    # Relationships (lazy="raise" prevents accidental N+1 — always use explicit joins)
    # foreign_keys is required because collections now has two FKs to tenants.id:
    #   tenant_id (ownership) and managed_by_tenant_id (public collection manager).
    # This relationship follows tenant_id — the primary ownership FK.
    collections: Mapped[list["Collection"]] = relationship(  # type: ignore[name-defined]  # noqa: F821
        foreign_keys="Collection.tenant_id",
        lazy="raise",
    )
    user_tenants: Mapped[list["UserTenant"]] = relationship(  # type: ignore[name-defined]  # noqa: F821
        back_populates="tenant", lazy="raise"
    )

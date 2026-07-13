"""User model — global Keycloak identity (no tenant_id by design).

Users are global entities identified by their Keycloak subject claim.
Membership in tenants is tracked via the `user_tenants` join table.
"""

import uuid

from sqlalchemy import Boolean, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.models.base import Base, TimestampMixin


class User(Base, TimestampMixin):
    __tablename__ = "users"
    # NO tenant_id — intentional. Users are global; link via user_tenants.

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    keycloak_sub: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True, index=True
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )

"""Base classes for all SQLAlchemy 2.x ORM models."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """All models inherit from this base.

    `type_annotation_map` ensures `uuid.UUID` and `datetime` are mapped
    to Postgres-native types throughout the project without repeating the
    type argument on every `mapped_column()` call.
    """

    type_annotation_map: dict[type, Any] = {
        uuid.UUID: UUID(as_uuid=True),
        datetime: DateTime(timezone=True),
    }


class TimestampMixin:
    """Adds `created_at` and `updated_at` columns.

    `updated_at` is managed both by SQLAlchemy's `onupdate` (for ORM
    updates) and by a Postgres trigger (for raw SQL updates). See the
    initial migration for the trigger DDL.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

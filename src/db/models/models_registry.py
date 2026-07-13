"""ModelsRegistry — LLM and embedding model configuration.

NULL tenant_id means the model is system-wide (available to all tenants).
Non-NULL tenant_id means the model is private to that tenant.
"""

import uuid
from typing import Any

from sqlalchemy import ARRAY, Boolean, CheckConstraint, ForeignKey, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.models.base import Base, TimestampMixin


class ModelsRegistry(Base, TimestampMixin):
    __tablename__ = "models_registry"
    __table_args__ = (
        CheckConstraint("type IN ('llm', 'embedding')", name="ck_models_registry_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )  # NULL = system-wide model available to all tenants
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(
        String(20), nullable=False, index=True
    )  # "llm" | "embedding" — enforced by ck_models_registry_type
    provider: Mapped[str] = mapped_column(String(50), nullable=False)  # "ollama" | "vllm"
    endpoint_url: Mapped[str] = mapped_column(String(500), nullable=False)
    model_id: Mapped[str] = mapped_column(String(255), nullable=False)  # e.g. "BAAI/bge-m3"
    params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    allowed_roles: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default=text("'{}'::uuid[]")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), index=True
    )

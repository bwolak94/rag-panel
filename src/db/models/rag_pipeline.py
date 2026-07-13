"""RagPipeline model — named pipeline combining collections, LLM, and prompt config."""

import uuid
from typing import Any

from sqlalchemy import ARRAY, Boolean, ForeignKey, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.models.base import Base, TimestampMixin


class RagPipeline(Base, TimestampMixin):
    """
    A RAG pipeline presented to Open WebUI as a selectable "model."
    `prompt_config` and `guardrails` are JSONB — validated at the Pydantic layer on write.
    """

    __tablename__ = "rag_pipelines"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_rag_pipelines_tenant_name"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    collection_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default=text("'{}'")
    )
    llm_model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("models_registry.id"), nullable=False, index=True
    )
    prompt_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )
    guardrails: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), index=True
    )

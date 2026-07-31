"""RagPipeline model — named pipeline combining collections, LLM, and prompt config.

prompt_config JSONB schema
--------------------------
All fields are optional.  Unrecognised keys are ignored.

    {
        "generate_prompt_version": "v1",   # str — which generate_<version>.md to use
        "ab_test": {                        # optional AB-test configuration block
            "enabled": true,               # bool — whether AB testing is active
            "shadow_prompt_version": "v2", # str  — the shadow prompt version to run
            "traffic_split": 0.1,          # float 0..1 — fraction of requests that run shadow
            "experiment_id": "exp-001"     # str  — logical experiment identifier for grouping
        }
    }

When ``ab_test.enabled`` is true and a random float < ``ab_test.traffic_split``,
``PromptABTestingService`` runs the shadow prompt in a background task after the
main generation completes.  The user response is never delayed.
Results (lengths, latencies — never content) are stored in ``ab_test_results``.
"""

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
    See module docstring for the full `prompt_config` schema, including AB-test support.
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
        JSONB,
        nullable=False,
        server_default=text("'{}'"),
        comment=(
            "Pipeline prompt configuration. Supports generate_prompt_version (str) and "
            "ab_test block {enabled, shadow_prompt_version, traffic_split, experiment_id}. "
            "See src/db/models/rag_pipeline.py module docstring for full schema."
        ),
    )
    guardrails: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), index=True
    )

"""ABTestResult — stores prompt AB-test comparison metrics.

Only non-content metrics are recorded (answer lengths, latency).
Answer text, question text, and chunk content are NEVER stored here (GDPR).

Schema notes
------------
- ``tenant_id`` is NOT a FK to ``tenants`` on purpose: results must survive
  pipeline or conversation deletion for retrospective analysis.  The pipeline_id
  and conversation_id columns carry NULLable FKs so cascade-deletes can clean
  them up without blocking the FK lookup in normal usage.
- ``control_version`` / ``shadow_version`` are plain strings matching the
  prompt file suffix, e.g. "v1", "v2".
"""

import uuid

from sqlalchemy import Float, ForeignKey, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.models.base import Base, TimestampMixin


class ABTestResult(Base, TimestampMixin):
    """Stores per-request AB-test comparison metrics (no answer content, GDPR-safe)."""

    __tablename__ = "ab_test_results"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        index=True,
        comment=(
            "Tenant that owns this result. Denormalised — not a FK so results survive tenant ops."
        ),
    )
    pipeline_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("rag_pipelines.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Pipeline the request was routed through. NULL when pipeline has been deleted.",
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Conversation the request belongs to. NULL when conversation has been deleted.",
    )
    experiment_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
        comment="Logical experiment identifier from prompt_config.ab_test.experiment_id.",
    )
    control_version: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Prompt version used for the control (main) generation, e.g. 'v1'.",
    )
    shadow_version: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Prompt version used for the shadow generation, e.g. 'v2'.",
    )
    control_length: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Character length of the control answer. Never stores the answer text itself.",
    )
    shadow_length: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Character length of the shadow answer. Never stores the answer text itself.",
    )
    latency_control_ms: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        comment="Wall-clock time in milliseconds for the control LLM call.",
    )
    latency_shadow_ms: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        comment="Wall-clock time in milliseconds for the shadow LLM call.",
    )

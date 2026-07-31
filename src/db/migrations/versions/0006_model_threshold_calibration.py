"""Add score-threshold calibration columns to models_registry.

Three new nullable columns record the result of an automated eval-driven
threshold sweep (ThresholdCalibrationService):

  score_threshold_calibrated  — Float   — optimal F1 threshold; NULL = use app default
  threshold_calibrated_at     — TimestampTZ — when the calibration last ran
  threshold_calibration_samples — Integer — eval samples used in that run

NULL in all three columns means the model has never been calibrated; callers
should fall back to the application or collection default score_threshold.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "models_registry",
        sa.Column(
            "score_threshold_calibrated",
            sa.Float(),
            nullable=True,
            comment=(
                "Optimal retrieval score threshold determined by F1 sweep on eval set. "
                "NULL means no calibration has been run; fall back to application default."
            ),
        ),
    )
    op.add_column(
        "models_registry",
        sa.Column(
            "threshold_calibrated_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="UTC timestamp of the most recent threshold calibration run.",
        ),
    )
    op.add_column(
        "models_registry",
        sa.Column(
            "threshold_calibration_samples",
            sa.Integer(),
            nullable=True,
            comment="Number of EvalResult samples used in the most recent calibration run.",
        ),
    )


def downgrade() -> None:
    op.drop_column("models_registry", "threshold_calibration_samples")
    op.drop_column("models_registry", "threshold_calibrated_at")
    op.drop_column("models_registry", "score_threshold_calibrated")

"""Seed default model registry entries from seed_models.json."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import structlog
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.models_registry import ModelsRegistry

logger = structlog.get_logger(__name__)


async def seed_default_models(session: AsyncSession) -> None:
    """Insert default models from seed_models.json if they do not already exist.

    Resolves ${ENV_VAR} placeholders from environment variables.
    Idempotent: skips rows where (name, tenant_id IS NULL) already exists.

    Args:
        session: An active async database session. The caller is responsible
                 for providing a session; this function commits internally after
                 all inserts so that seeding is atomic per startup.
    """
    seed_path = Path(__file__).parent.parent.parent / "seed_models.json"
    if not seed_path.exists():
        logger.warning("seed.models_json_not_found", path=str(seed_path))
        return

    raw = seed_path.read_text()
    resolved = re.sub(
        r"\$\{(\w+)\}",
        lambda m: os.environ.get(m.group(1), m.group(0)),
        raw,
    )
    entries: list[dict[str, object]] = json.loads(resolved)

    for entry in entries:
        stmt = select(ModelsRegistry).where(
            and_(
                ModelsRegistry.name == entry["name"],
                ModelsRegistry.tenant_id.is_(None),
            )
        )
        existing = (await session.execute(stmt)).scalar_one_or_none()
        if existing is None:
            session.add(ModelsRegistry(**entry))
            logger.info("seed.model_inserted", name=entry["name"])
        else:
            logger.debug("seed.model_exists", name=entry["name"])

    await session.commit()

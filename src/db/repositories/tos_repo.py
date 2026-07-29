"""Repository for TosVersion and TosAcceptance CRUD."""

from __future__ import annotations

import uuid

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.tos_acceptance import TosAcceptance
from src.db.models.tos_version import TosVersion


class TosRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_active_version(self) -> TosVersion | None:
        """Return the currently active ToS version, or None if none exists."""
        q = select(TosVersion).where(TosVersion.status == "active").limit(1)
        return (await self._session.execute(q)).scalar_one_or_none()

    async def get_acceptance(
        self, tenant_id: uuid.UUID, tos_version_id: uuid.UUID
    ) -> TosAcceptance | None:
        """Return the acceptance record for a specific tenant+version pair, or None."""
        q = select(TosAcceptance).where(
            TosAcceptance.tenant_id == tenant_id,
            TosAcceptance.tos_version_id == tos_version_id,
        )
        return (await self._session.execute(q)).scalar_one_or_none()

    async def create_acceptance(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        tos_version_id: uuid.UUID,
        ip_address: str,
        user_agent: str | None,
    ) -> TosAcceptance:
        """Insert a new ToS acceptance record and flush to obtain its id."""
        acceptance = TosAcceptance(
            tenant_id=tenant_id,
            user_id=user_id,
            tos_version_id=tos_version_id,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        self._session.add(acceptance)
        await self._session.flush()
        return acceptance

    async def has_accepted_current_tos(self, tenant_id: uuid.UUID) -> bool:
        """Return True if the tenant has accepted the currently active ToS version."""
        q = select(
            exists(
                select(TosAcceptance.id)
                .join(TosVersion, TosVersion.id == TosAcceptance.tos_version_id)
                .where(
                    TosAcceptance.tenant_id == tenant_id,
                    TosVersion.status == "active",
                )
            )
        )
        result: bool = (await self._session.execute(q)).scalar_one()
        return result

    async def get_latest_acceptance(self, tenant_id: uuid.UUID) -> TosAcceptance | None:
        """Return the most recent acceptance record for a tenant across all ToS versions."""
        q = (
            select(TosAcceptance)
            .where(TosAcceptance.tenant_id == tenant_id)
            .order_by(TosAcceptance.accepted_at.desc())
            .limit(1)
        )
        return (await self._session.execute(q)).scalar_one_or_none()

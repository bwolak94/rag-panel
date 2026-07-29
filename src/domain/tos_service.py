"""TosService — Terms of Service business logic with Redis caching."""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import DomainValidationError, NotFoundError
from src.db.models.tos_acceptance import TosAcceptance
from src.db.models.tos_version import TosVersion
from src.db.repositories.tos_repo import TosRepository

logger = structlog.get_logger(__name__)

_CACHE_TTL_SECONDS = 60
_CACHE_KEY_TEMPLATE = "tos:tenant:{tenant_id}:accepted"


class TosService:
    """Business logic for Terms of Service acceptance and status checks."""

    def __init__(self, session: AsyncSession, redis: Redis) -> None:  # type: ignore[type-arg]
        self._repo = TosRepository(session)
        self._redis = redis

    # ── Public API ────────────────────────────────────────────────────────────

    async def get_current_tos(self) -> TosVersion:
        """Return the currently active ToS version.

        Raises:
            NotFoundError: When no version with status='active' exists.
        """
        version = await self._repo.get_active_version()
        if version is None:
            raise NotFoundError("No active Terms of Service version found")
        return version

    async def accept_tos(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        tos_version_id: uuid.UUID,
        ip_address: str,
        user_agent: str | None,
        explicit_consent: bool,
    ) -> TosAcceptance:
        """Record a tenant's acceptance of the active ToS version.

        Idempotent: if the tenant already accepted this version, the existing
        record is returned without creating a duplicate.

        Args:
            tenant_id: UUID of the accepting tenant.
            user_id: UUID of the user performing the acceptance.
            tos_version_id: Must match the currently active version.
            ip_address: Raw client IP (stored unmasked in DB for legal audit).
            user_agent: Optional HTTP User-Agent header value.
            explicit_consent: Must be True; raises DomainValidationError if False.

        Returns:
            TosAcceptance ORM object (new or existing).

        Raises:
            DomainValidationError: If explicit_consent is False or tos_version_id
                does not match the currently active version.
            NotFoundError: If no active ToS version exists.
        """
        if not explicit_consent:
            raise DomainValidationError(
                "explicit_consent must be True to accept the Terms of Service"
            )

        active_version = await self._repo.get_active_version()
        if active_version is None:
            raise NotFoundError("No active Terms of Service version found")

        if active_version.id != tos_version_id:
            raise DomainValidationError(
                f"tos_version_id does not match the active version ({active_version.version})"
            )

        existing = await self._repo.get_acceptance(tenant_id, tos_version_id)
        if existing is not None:
            logger.info(
                "tos.already_accepted",
                tenant_id=str(tenant_id),
                tos_version_id=str(tos_version_id),
            )
            return existing

        acceptance = await self._repo.create_acceptance(
            tenant_id=tenant_id,
            user_id=user_id,
            tos_version_id=tos_version_id,
            ip_address=ip_address,
            user_agent=user_agent,
        )

        await self.invalidate_acceptance_cache(tenant_id)

        logger.info(
            "tos.accepted",
            tenant_id=str(tenant_id),
            user_id=str(user_id),
            tos_version_id=str(tos_version_id),
        )

        return acceptance

    async def get_tos_status(self, tenant_id: uuid.UUID) -> dict[str, Any]:
        """Return a dict with acceptance status info for a tenant.

        The router is responsible for building the final TosStatusResponse schema.

        Returns a dict with keys:
            current_tos_version (str): active version string
            is_accepted (bool): whether the tenant accepted the current version
            accepted_at (datetime | None): timestamp of acceptance
            accepted_by (str | None): display-name placeholder (stored in acceptance record)
            requires_reacceptance (bool): True when tenant has old acceptance but not current
            ui_message (str | None): human-readable message for the UI
        """
        active_version = await self._repo.get_active_version()
        if active_version is None:
            raise NotFoundError("No active Terms of Service version found")

        is_accepted = await self._repo.has_accepted_current_tos(tenant_id)

        accepted_at = None
        requires_reacceptance = False
        ui_message: str | None = None

        if is_accepted:
            latest = await self._repo.get_acceptance(tenant_id, active_version.id)
            if latest is not None:
                accepted_at = latest.accepted_at
            ui_message = None
        else:
            any_acceptance = await self._repo.get_latest_acceptance(tenant_id)
            if any_acceptance is not None:
                requires_reacceptance = True
                ui_message = (
                    "Warunki korzystania zostaly zaktualizowane. "
                    "Wlasciciel organizacji musi je ponownie zaakceptowac."
                )
            else:
                ui_message = (
                    "Wlasciciel Twojej organizacji musi zaakceptowac warunki "
                    "korzystania przed kontynuacja pracy w systemie."
                )

        return {
            "current_tos_version": active_version.version,
            "is_accepted": is_accepted,
            "accepted_at": accepted_at,
            "accepted_by": None,
            "requires_reacceptance": requires_reacceptance,
            "ui_message": ui_message,
        }

    async def check_tenant_acceptance(self, tenant_id: uuid.UUID) -> bool:
        """Redis-first check whether a tenant has accepted the current ToS.

        Caches result for ``_CACHE_TTL_SECONDS`` seconds. Used by middleware
        to avoid a DB round-trip on every request.

        Args:
            tenant_id: UUID of the tenant to check.

        Returns:
            True when the tenant has a valid acceptance for the active version.
        """
        cache_key = _CACHE_KEY_TEMPLATE.format(tenant_id=tenant_id)
        cached = await self._redis.get(cache_key)

        if cached == b"1":
            return True
        if cached == b"0":
            return False

        # Cache miss — query DB
        accepted = await self._repo.has_accepted_current_tos(tenant_id)
        await self._redis.set(cache_key, "1" if accepted else "0", ex=_CACHE_TTL_SECONDS)

        return accepted

    async def invalidate_acceptance_cache(self, tenant_id: uuid.UUID) -> None:
        """Evict the Redis acceptance cache entry for a tenant.

        Args:
            tenant_id: UUID whose cache entry should be deleted.
        """
        cache_key = _CACHE_KEY_TEMPLATE.format(tenant_id=tenant_id)
        await self._redis.delete(cache_key)

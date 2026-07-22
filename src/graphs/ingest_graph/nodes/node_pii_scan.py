"""node_pii_scan — regex-based PII detection for GDPR compliance.

Uses an injectable PIIScanner protocol for testability (DefaultPIIScanner uses
regex patterns for Polish PII: PESEL, phone, passport, postal code).

Security rules (GDPR Art. 5):
- Logs ONLY pii_detected (bool) and flags_count (int) — never actual matched text.
- pii_flags in ValidationResult contains ONLY type labels (e.g. "PESEL"), not values.
- PII content must never appear in logs, Langfuse traces, or any external store.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Any, ClassVar, Protocol

import structlog
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.document import Document
from src.db.models.ingestion_job import IngestionJob
from src.graphs.ingest_graph.helpers import get_collection, update_step, utcnow
from src.graphs.ingest_graph.state import IngestState, ValidationResult

logger = structlog.get_logger(__name__)


class PIIScanner(Protocol):
    """Injectable PII scanner interface for testability."""

    async def scan(self, text: str) -> tuple[bool, list[str]]:
        """Scan text for PII.

        Returns:
            (pii_detected, type_labels_only) — never actual matched values.
        """
        ...


class DefaultPIIScanner:
    """Regex-based PII scanner for Polish-language medical documents.

    Patterns detect structural PII markers only; matched text is NEVER stored.
    """

    _PATTERNS: ClassVar[list[tuple[str, str]]] = [
        (r"\b\d{11}\b", "PESEL"),
        (r"\b\d{3}[-\s]\d{3}[-\s]\d{3}\b", "PHONE"),
        (r"\b[A-Z]{2}\d{7}\b", "PASSPORT"),
        (r"\b\d{2}-\d{3}\b", "POSTAL_CODE"),
    ]
    _COMPILED: ClassVar[list[tuple[re.Pattern[str], str]]] = [
        (re.compile(pattern), label) for pattern, label in _PATTERNS
    ]

    async def scan(self, text: str) -> tuple[bool, list[str]]:
        """Scan text using compiled regex patterns.

        Only type labels are returned — never the matched values.
        """

        def _run_scan() -> list[str]:
            found: list[str] = []
            for compiled, label in self._COMPILED:
                if compiled.search(text):
                    found.append(label)
            return found

        loop = asyncio.get_running_loop()
        flags: list[str] = await loop.run_in_executor(None, _run_scan)
        return bool(flags), flags


_DEFAULT_SCANNER = DefaultPIIScanner()


async def node_pii_scan(
    state: IngestState,
    config: dict[str, Any],
    scanner: PIIScanner | None = None,
) -> dict[str, Any]:
    """Scan extracted text for PII; route to needs_review if detected.

    Args:
        state: Must have extracted_text from node_extract.
        config: RunnableConfig with configurable["db"].
        scanner: Injectable PIIScanner (defaults to DefaultPIIScanner).

    Returns:
        {} if no PII or pii_action != "review"; {"status": "needs_review"} if PII found.
    """
    cfg = config.get("configurable", {})
    session: AsyncSession = cfg["db"]
    active_scanner: PIIScanner = scanner or cfg.get("pii_scanner") or _DEFAULT_SCANNER
    step_start = utcnow()

    text = state.extracted_text or ""
    pii_detected, pii_type_labels = await active_scanner.scan(text)

    collection = await get_collection(session, state.collection_id)
    pii_action = (collection.validation_config or {}).get("pii_action", "review")

    # Log ONLY counts and type labels — never actual PII values
    await update_step(
        session,
        state.job_id,
        stage="pii_scan",
        status="completed",
        started_at=step_start,
        meta={
            "pii_detected": pii_detected,
            "flags_count": len(pii_type_labels),
            "latency_ms": _elapsed_ms(step_start),
        },
    )

    if pii_detected and pii_action == "review":
        existing_vr = state.validation_result or ValidationResult()
        updated_vr = existing_vr.model_copy(update={"pii_flags": pii_type_labels})

        await session.execute(
            update(Document)
            .where(
                Document.id == state.document_id,
                Document.tenant_id == state.tenant_id,
            )
            .values(status="needs_review", validation_result=updated_vr.model_dump())
        )
        await session.execute(
            update(IngestionJob)
            .where(IngestionJob.id == state.job_id)
            .values(status="awaiting_review")
        )
        await session.commit()

        logger.info(
            "node_pii_scan_needs_review",
            document_id=str(state.document_id),
            flags_count=len(pii_type_labels),
        )
        return {"validation_result": updated_vr, "status": "needs_review"}

    logger.info(
        "node_pii_scan_completed",
        document_id=str(state.document_id),
        pii_detected=pii_detected,
    )
    return {}


def _elapsed_ms(start: datetime) -> int:
    return int((datetime.now(UTC) - start).total_seconds() * 1000)

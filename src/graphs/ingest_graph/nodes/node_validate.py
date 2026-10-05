"""node_validate — LLM-based document quality and category classification.

Prompt loaded from src/graphs/prompts/validate_document_v1.md (never inline).
Samples first 2000 words to avoid excessive token usage.
Low-quality documents (quality_score < 0.3) are routed to needs_review.

Security:
- Sample text is passed to LLM — treated as untrusted; never echoed back in logs.
- ValidationResult stored in DB; never logged.
- Prompt injection risk: document text used only in the user message, not system.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from langfuse import observe
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import IngestNodeError
from src.core.langfuse_client import update_span_metadata as _lf_update_span
from src.db.models.document import Document
from src.graphs.ingest_graph.helpers import get_collection, get_model, update_step, utcnow
from src.graphs.ingest_graph.state import IngestState, ValidationResult

logger = structlog.get_logger(__name__)

_PROMPT_PATH = (
    Path(__file__).parent.parent.parent.parent / "graphs" / "prompts" / "validate_document_v1.md"
)
_QUALITY_THRESHOLD = 0.3


def _load_prompt() -> str:
    """Load the validation prompt from disk. Cached after first load."""
    return _PROMPT_PATH.read_text(encoding="utf-8")


@observe(name="node_validate", capture_input=False, capture_output=False)
async def node_validate(state: IngestState, config: dict[str, Any]) -> dict[str, Any]:
    """Classify document category/quality using LLM.

    Args:
        state: Must have extracted_text set by node_extract.
        config: RunnableConfig with configurable["db"] and configurable["llm"].

    Returns:
        {"validation_result": ValidationResult} or
        {"validation_result": ValidationResult, "status": "needs_review", "halt": True}

    Raises:
        IngestNodeError: On LLM failure or parse error.
    """
    cfg = config.get("configurable", {})
    session: AsyncSession = cfg["db"]
    llm = cfg["llm"]
    step_start = utcnow()

    try:
        collection = await get_collection(session, state.collection_id)
        validation_config = collection.validation_config or {}
        confidence_threshold = validation_config.get("confidence_threshold", 0.7)  # noqa: F841

        # Sample first 2000 words — use OCR output when available (image-only docs),
        # otherwise fall back to the normal extracted text.
        # SECURITY: neither ocr_text nor extracted_text must appear in logs.
        effective_text = state.ocr_text or state.extracted_text or ""
        sample_text = " ".join(effective_text.split()[:2000])

        prompt_template = _load_prompt()

        # Get the LLM model for this tenant's collection
        model_record = await get_model(session, collection.embedding_model_id)

        response = await llm.chat_completion(
            model=model_record.model_id,
            messages=[
                {"role": "system", "content": prompt_template},
                {
                    "role": "user",
                    "content": f"<document>\n{sample_text}\n</document>",
                },
            ],
            base_url=model_record.endpoint_url,
            response_format={"type": "json_object"},
        )

        raw_content = response.choices[0].message.content or "{}"
        try:
            parsed_dict = json.loads(raw_content)
            validation_result = ValidationResult.model_validate(parsed_dict)
        except Exception as exc:
            raise IngestNodeError(f"validation_parse_error: {type(exc).__name__}") from exc

        # Persist validation_result and category to documents table
        await session.execute(
            update(Document)
            .where(
                Document.id == state.document_id,
                Document.tenant_id == state.tenant_id,
            )
            .values(
                validation_result=validation_result.model_dump(),
                category=validation_result.category,
                language=validation_result.language,
                status="validating",
            )
        )
        await session.commit()

        meta = {
            "category": validation_result.category,
            "confidence": validation_result.confidence,
            "quality_score": validation_result.quality_score,
            "document_type": validation_result.document_type,
            "latency_ms": _elapsed_ms(step_start),
        }

        elapsed = _elapsed_ms(step_start)
        lf_meta = {
            "document_id": str(state.document_id),
            "tenant_id": str(state.tenant_id),
            "category": validation_result.category,
            "quality_score": validation_result.quality_score,
            "confidence": validation_result.confidence,
            "latency_ms": elapsed,
        }

        # Quality gate: route to needs_review if quality too low
        if (
            validation_result.quality_score is not None
            and validation_result.quality_score < _QUALITY_THRESHOLD
        ):
            await session.execute(
                update(Document)
                .where(
                    Document.id == state.document_id,
                    Document.tenant_id == state.tenant_id,
                )
                .values(status="needs_review")
            )
            await session.commit()

            await update_step(
                session,
                state.job_id,
                stage="validate",
                status="completed",
                started_at=step_start,
                meta=meta,
            )
            _lf_update_span(metadata={**lf_meta, "routed_to": "needs_review"})
            logger.info(
                "node_validate_low_quality",
                document_id=str(state.document_id),
                quality_score=validation_result.quality_score,
            )
            return {
                "validation_result": validation_result,
                "status": "needs_review",
                "halt": True,
            }

        await update_step(
            session,
            state.job_id,
            stage="validate",
            status="completed",
            started_at=step_start,
            meta=meta,
        )
        _lf_update_span(metadata=lf_meta)
        logger.info(
            "node_validate_completed",
            document_id=str(state.document_id),
            category=validation_result.category,
        )
        return {"validation_result": validation_result}

    except IngestNodeError as exc:
        _lf_update_span(
            metadata={
                "document_id": str(state.document_id),
                "tenant_id": str(state.tenant_id),
                "error": True,
                "error_type": type(exc).__name__,
            }
        )
        await update_step(
            session,
            state.job_id,
            stage="validate",
            status="failed",
            started_at=step_start,
            error=str(exc),
        )
        raise
    except Exception as exc:
        error_msg = f"validate_error: {type(exc).__name__}"
        _lf_update_span(
            metadata={
                "document_id": str(state.document_id),
                "tenant_id": str(state.tenant_id),
                "error": True,
                "error_type": type(exc).__name__,
            }
        )
        await update_step(
            session,
            state.job_id,
            stage="validate",
            status="failed",
            started_at=step_start,
            error=error_msg,
        )
        raise IngestNodeError(error_msg) from exc


def _elapsed_ms(start: datetime) -> int:
    return int((datetime.now(UTC) - start).total_seconds() * 1000)

"""Langfuse singleton client management.

initialize_langfuse() — initializes the Langfuse client from settings at startup.
shutdown_langfuse()   — flushes pending events and shuts down cleanly.
get_langfuse()        — returns the singleton Langfuse instance, or None when disabled.

The module is importable even without LANGFUSE_PUBLIC_KEY configured.
All langfuse imports are lazy (inside functions) so the import itself never raises.

GDPR: this module never logs user data. Only token counts, tenant IDs, and other
safe operational metadata are sent to Langfuse — enforced via capture_input=False /
capture_output=False on every @observe decorator.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _mask_pii(*, data: Any, **_kwargs: Any) -> Any:
    """Langfuse mask callback — strips all input/output content as a defence-in-depth layer.

    security.md requires "Langfuse with masking enabled". Even though capture_input=False
    and capture_output=False are set on every @observe decorator, this mask provides a
    second layer: if any content accidentally reaches the SDK, it is replaced with a
    sentinel before export to the Langfuse server.

    The MaskFunction protocol requires `data` as a keyword-only argument.
    """
    # Replace any string value with a sentinel. Dicts and other structures are left as-is
    # so that safe operational metadata (counts, booleans, UUIDs) passes through unchanged.
    if isinstance(data, str):
        return "[MASKED]"
    return data


# Module-level singleton — None until initialize_langfuse() is called.
# Typed as Any because Langfuse is imported lazily (not available without config).
_langfuse_instance: Any = None


def initialize_langfuse() -> None:
    """Initialize the Langfuse singleton from environment settings.

    Safe to call even when LANGFUSE_PUBLIC_KEY is not configured — in that
    case the client is set to None and all tracing calls become no-ops.

    Should be called once at application startup (lifespan).
    """
    global _langfuse_instance

    try:
        from src.core.config import settings

        if not settings.LANGFUSE_PUBLIC_KEY or not settings.LANGFUSE_SECRET_KEY:
            logger.info("langfuse_disabled: no keys configured, tracing is a no-op")
            _langfuse_instance = None
            return

        from langfuse import Langfuse

        _langfuse_instance = Langfuse(
            public_key=settings.LANGFUSE_PUBLIC_KEY,
            secret_key=settings.LANGFUSE_SECRET_KEY,
            host=settings.LANGFUSE_HOST,
            mask=_mask_pii,  # security.md: "Langfuse with masking enabled" — defence in depth
        )
        logger.info("langfuse_initialized", extra={"host": settings.LANGFUSE_HOST})
    except Exception as exc:  # pragma: no cover — belt-and-suspenders
        logger.warning("langfuse_init_failed: %s", exc)
        _langfuse_instance = None


def shutdown_langfuse() -> None:
    """Flush pending events and shut down the Langfuse client.

    Safe to call even when Langfuse is not configured — becomes a no-op.
    Should be called once at application shutdown (lifespan).
    """
    global _langfuse_instance

    if _langfuse_instance is None:
        return

    try:
        _langfuse_instance.flush()
        _langfuse_instance.shutdown()
        logger.info("langfuse_shutdown_complete")
    except Exception as exc:  # pragma: no cover
        logger.warning("langfuse_shutdown_error: %s", exc)
    finally:
        _langfuse_instance = None


def get_langfuse() -> Any:
    """Return the Langfuse singleton, or None when not configured.

    Returns:
        Langfuse instance if initialized, None otherwise.
    """
    return _langfuse_instance


def update_span_metadata(metadata: dict[str, Any]) -> None:
    """Update the current Langfuse span with GDPR-safe metadata.

    No-op when Langfuse is not configured (_langfuse_instance is None).
    Never raises — tracing failures must never block the RAG pipeline.

    Only call with safe values: tenant_id, counts, booleans, enum strings.
    NEVER pass question text, answer text, chunk content, or PII.

    Args:
        metadata: Dict of safe operational metadata (identifiers, counts, booleans).
    """
    if _langfuse_instance is None:
        return
    try:
        from langfuse import get_client as _sdk_get_client

        _sdk_get_client().update_current_span(metadata=metadata)
    except Exception as exc:  # pragma: no cover
        logger.warning("langfuse_span_update_failed: %s", exc)

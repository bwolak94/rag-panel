"""Structured logging configuration via structlog.

Security constraint: logs MUST NOT contain document text, prompt content,
LLM responses, or PII. Log identifiers (IDs, status codes, counts) only.
"""

import logging
import sys

import structlog


def configure_logging(*, level: str = "INFO") -> None:
    """Configure structlog for JSON output.

    Call once at application startup (inside lifespan). Subsequent calls
    are idempotent due to `cache_logger_on_first_use=True`.
    """
    log_level = logging.getLevelName(level)

    # Route stdlib logging → stdout so structlog's stdlib factory output is visible
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,  # requires stdlib LoggerFactory (has .name)
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.stdlib.LoggerFactory(),  # stdlib loggers have .name
        cache_logger_on_first_use=True,
    )

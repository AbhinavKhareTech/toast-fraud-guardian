"""
Structured logging configuration using structlog.
Outputs JSON in production, pretty-printed in development.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from app.core.config import Environment, get_settings


def setup_logging() -> None:
    settings = get_settings()

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        _add_service_context,
    ]

    if settings.app_env == Environment.PRODUCTION:
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, settings.log_level.upper()))

    # Silence noisy libraries
    for name in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _add_service_context(
    logger: Any, method: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    settings = get_settings()
    event_dict["service"] = "toast-fraud-guardian"
    event_dict["environment"] = settings.app_env.value
    event_dict["model_version"] = settings.model_version
    return event_dict


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)

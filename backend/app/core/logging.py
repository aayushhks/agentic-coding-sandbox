import logging
from typing import cast

import structlog
from structlog.typing import Processor


def configure_logging(log_level: str = "INFO", *, json_logs: bool = False) -> None:
    """Configure structlog once at application startup.

    Uses a human-readable console renderer by default and JSON when ``json_logs`` is set,
    so the same setup works for local development and structured production logging.
    """
    level = logging.getLevelNamesMapping().get(log_level.upper(), logging.INFO)

    renderer: Processor = (
        structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer()
    )
    processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        renderer,
    ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger."""
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name))

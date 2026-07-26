import contextvars
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog

from app.config import settings

_job_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("job_id", default=None)


def _inject_job_id(logger: object, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    job_id = _job_id_var.get()
    if job_id is not None and "job_id" not in event_dict:
        event_dict["job_id"] = job_id
    return event_dict


def configure_logging() -> None:
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        _inject_job_id,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if settings.env == "local":
        renderer: Any = structlog.dev.ConsoleRenderer()
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(settings.log_level.upper())
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]


@contextmanager
def bind_job(job_id: str) -> Iterator[None]:
    """Context manager binding job_id onto every log line emitted within it."""
    token = _job_id_var.set(job_id)
    try:
        yield
    finally:
        _job_id_var.reset(token)

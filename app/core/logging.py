import logging

import structlog

from app.core.security import sanitize


def _sanitize_event(_logger: object, _method_name: str, event_dict: dict) -> dict:
    return sanitize(event_dict)


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _sanitize_event,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )


logger = structlog.get_logger()

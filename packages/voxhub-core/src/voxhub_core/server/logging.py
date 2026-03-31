"""Structlog configuration for the server.

All server logging is structured JSON to stderr, suitable for
ingestion by systemd journal, logrotate, or similar tools.
"""

import structlog


def configure_logging() -> None:
    """Configure structlog for server-side structured JSON logging."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt='iso'),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(0),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(**kwargs: object) -> structlog.stdlib.BoundLogger:
    """Get a bound structlog logger with optional initial context.

    Parameters
    ----------
    **kwargs
        Initial context bindings (e.g. ``command='list-stores'``).

    Returns
    -------
    structlog.stdlib.BoundLogger
    """
    return structlog.get_logger(**kwargs)  # type: ignore[return-value]

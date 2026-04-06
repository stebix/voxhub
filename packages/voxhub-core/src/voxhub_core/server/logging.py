"""Structlog configuration for the voxhub server.

All server logging is structured JSON.  Two outputs are supported:

* **stderr** — configurable minimum level (default: WARNING), suitable
  for ingestion by systemd journal or logrotate.
* **rotating file** — DEBUG and above, enabled when ``log_file`` is set
  in :class:`~voxhub_core.server.settings.LoggingSettings`.

Both outputs share the same JSON processor chain so log lines are
uniform and can be processed with the same tooling.
"""

import logging
import logging.handlers
import sys

import structlog

from voxhub_core.server.settings import LoggingSettings


def configure_logging(settings: LoggingSettings | None = None) -> None:
    """Configure structlog with optional rotating file output.

    Sets up stdlib ``logging`` as the backend so that both a stderr
    handler and an optional rotating file handler can receive records
    simultaneously.  structlog acts as the structured frontend for all
    application code; third-party stdlib log records are also captured
    and rendered as JSON.

    Parameters
    ----------
    settings : LoggingSettings or None
        Logging configuration.  When *None* the built-in defaults are
        used: WARNING to stderr, no log file.
    """
    if settings is None:
        settings = LoggingSettings()

    stderr_level = getattr(logging, settings.stderr_level, logging.WARNING)

    # Shared pre-chain: runs for both structlog-originated records and
    # foreign stdlib records (e.g. from zarr, pydicom, etc.).
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt='iso'),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=shared_processors,
    )

    root = logging.getLogger()
    root.handlers.clear()

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    stderr_handler.setLevel(stderr_level)
    root.addHandler(stderr_handler)

    if settings.log_file is not None:
        settings.log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            settings.log_file,
            maxBytes=settings.log_max_bytes,
            backupCount=settings.log_backup_count,
            encoding='utf-8',
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.DEBUG)
        root.addHandler(file_handler)

    root.setLevel(logging.DEBUG)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
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

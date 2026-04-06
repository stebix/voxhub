"""Server settings loaded from a TOML configuration file.

Config file path resolution order:

1. ``VOXHUB_SERVER_CONFIG`` environment variable
2. ``~/.config/voxhub/server.toml``
3. Built-in defaults (WARNING to stderr, no log file)

Example config file::

    [logging]
    log_file = '/var/log/voxhub/debug.log'
    log_max_bytes = 52428800  # 50 MB
    log_backup_count = 10
    stderr_level = 'WARNING'
"""

import os
import tomllib
from pathlib import Path

import attrs

_DEFAULT_CONFIG_PATH = Path.home() / '.config' / 'voxhub' / 'server.toml'
_CONFIG_ENV_VAR = 'VOXHUB_SERVER_CONFIG'
_VALID_LOG_LEVELS = frozenset({'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'})


@attrs.define
class LoggingSettings:
    """File-logging and level configuration for the voxhub server.

    Parameters
    ----------
    log_file : Path or None
        Absolute path for the rotating debug log file.
        ``None`` disables file logging entirely.
    log_max_bytes : int
        Maximum size of a single log file before rotation (default: 50 MB).
    log_backup_count : int
        Number of rotated backup files to retain (default: 10).
    stderr_level : str
        Minimum log level emitted to stderr / systemd journal.
        Must be one of ``DEBUG``, ``INFO``, ``WARNING``, ``ERROR``,
        ``CRITICAL`` (default: ``WARNING``).
    """

    log_file: Path | None = None
    log_max_bytes: int = 50 * 1024 * 1024
    log_backup_count: int = 10
    stderr_level: str = 'WARNING'


@attrs.define
class ServerSettings:
    """Top-level voxhub server configuration.

    Parameters
    ----------
    logging : LoggingSettings
        Logging sub-configuration.
    """

    logging: LoggingSettings = attrs.Factory(LoggingSettings)


def _parse_logging(raw: dict[str, object]) -> LoggingSettings:
    defaults = LoggingSettings()
    log_file_raw = raw.get('log_file')
    stderr_level = str(raw.get('stderr_level', defaults.stderr_level)).upper()
    if stderr_level not in _VALID_LOG_LEVELS:
        stderr_level = 'WARNING'
    max_bytes_raw = raw.get('log_max_bytes', defaults.log_max_bytes)
    backup_count_raw = raw.get('log_backup_count', defaults.log_backup_count)
    return LoggingSettings(
        log_file=Path(str(log_file_raw)) if log_file_raw is not None else None,
        log_max_bytes=int(max_bytes_raw),  # type: ignore[arg-type]
        log_backup_count=int(backup_count_raw),  # type: ignore[arg-type]
        stderr_level=stderr_level,
    )


def load_settings() -> ServerSettings:
    """Load server settings from the TOML configuration file.

    Config file path resolution order:

    1. ``VOXHUB_SERVER_CONFIG`` environment variable
    2. ``~/.config/voxhub/server.toml``
    3. Built-in defaults

    Returns
    -------
    ServerSettings
        Parsed server settings, or defaults if no config file is found.
    """
    env_path = os.environ.get(_CONFIG_ENV_VAR)
    config_path = Path(env_path) if env_path else _DEFAULT_CONFIG_PATH

    if not config_path.exists():
        return ServerSettings()

    with open(config_path, 'rb') as fh:
        raw = tomllib.load(fh)

    return ServerSettings(logging=_parse_logging(raw.get('logging', {})))

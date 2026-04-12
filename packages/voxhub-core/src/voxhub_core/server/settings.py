"""Server settings loaded from a TOML configuration file.

Config file path resolution order:

1. ``VOXHUB_SERVER_CONFIG`` environment variable
2. ``~/.config/voxhub/server.toml``

The config file is mandatory and must declare ``[storage].stores_dir``.
Missing or invalid configuration is a hard error: the server refuses to
start.  There is no ``backwards-compatible defaults`` path.

Example config file::

    [logging]
    log_file = '/var/log/voxhub/debug.log'
    log_max_bytes = 52428800  # 50 MB
    log_backup_count = 10
    stderr_level = 'WARNING'

    [storage]
    stores_dir = '/srv/voxhub/zarr'
"""

import os
import tomllib
from pathlib import Path

import attrs

_DEFAULT_CONFIG_PATH = Path.home() / '.config' / 'voxhub' / 'server.toml'
_CONFIG_ENV_VAR = 'VOXHUB_SERVER_CONFIG'
_VALID_LOG_LEVELS = frozenset({'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'})


class SettingsError(Exception):
    """Raised when the server configuration is missing or invalid."""


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
class StorageSettings:
    """Storage paths for the voxhub server.

    Parameters
    ----------
    stores_dir : Path
        Absolute path to the directory containing ``*.zarr`` stores.
        This is the operator-owned location the server walks when serving
        ``list-stores``, ``prepare-pull`` and related commands.
    """

    stores_dir: Path


@attrs.define
class ServerSettings:
    """Top-level voxhub server configuration.

    Parameters
    ----------
    logging : LoggingSettings
        Logging sub-configuration.
    storage : StorageSettings
        Storage sub-configuration.  Mandatory: the server refuses to
        start without a valid ``[storage].stores_dir``.
    """

    logging: LoggingSettings
    storage: StorageSettings


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


def _parse_storage(raw: dict[str, object]) -> StorageSettings:
    stores_dir_raw = raw.get('stores_dir')
    if stores_dir_raw is None:
        raise SettingsError("[storage].stores_dir is required but missing")
    stores_dir = Path(str(stores_dir_raw)).expanduser()
    if not stores_dir.is_dir():
        raise SettingsError(
            f'[storage].stores_dir does not exist or is not a directory: {stores_dir}'
        )
    return StorageSettings(stores_dir=stores_dir)


def load_settings() -> ServerSettings:
    """Load server settings from the TOML configuration file.

    Config file path resolution order:

    1. ``VOXHUB_SERVER_CONFIG`` environment variable
    2. ``~/.config/voxhub/server.toml``

    Returns
    -------
    ServerSettings
        Parsed server settings.

    Raises
    ------
    SettingsError
        If the config file is missing, cannot be parsed, or declares a
        missing / invalid ``[storage].stores_dir``.
    """
    env_path = os.environ.get(_CONFIG_ENV_VAR)
    config_path = Path(env_path) if env_path else _DEFAULT_CONFIG_PATH

    if not config_path.exists():
        raise SettingsError(
            f'Server config file not found at {config_path}. '
            f'Set {_CONFIG_ENV_VAR} or create {_DEFAULT_CONFIG_PATH}.'
        )

    with open(config_path, 'rb') as fh:
        raw = tomllib.load(fh)

    storage_raw = raw.get('storage')
    if storage_raw is None:
        raise SettingsError(
            f"Server config at {config_path} is missing the [storage] section"
        )

    return ServerSettings(
        logging=_parse_logging(raw.get('logging', {})),  # type: ignore[arg-type]
        storage=_parse_storage(storage_raw),  # type: ignore[arg-type]
    )

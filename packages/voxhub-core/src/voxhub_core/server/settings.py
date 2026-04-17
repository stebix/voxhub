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
    staging_dir = '/srv/voxhub/staging'  # optional, defaults to $TMPDIR

    [memory]
    max_safe_volume_mb = 256  # warn at this size; default 256
    refuse_when_low_memory = true  # refuse rather than risk OOM; default true
    safety_factor = 2.0  # required headroom = volume * factor
"""

import os
import tempfile
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
    staging_dir : Path
        Operator-controlled parent directory under which ``prepare-pull``
        creates per-session staging sub-directories (each with the
        ``vxhb-staging-`` prefix).  ``cleanup`` and ``integrate-annotations``
        accept only echoed paths that live inside this root and carry the
        prefix; ``gc`` reaps expired sub-directories from it.  Defaults to
        ``tempfile.gettempdir()`` when the TOML key is absent; operators
        running on small root disks should point this at the data volume.
    """

    stores_dir: Path
    staging_dir: Path


@attrs.define
class MemorySettings:
    """RAM-budget policy for in-memory volume materialization.

    Parameters
    ----------
    max_safe_volume_mb : int
        Threshold (MB) above which a ``large_volume`` warning is emitted
        for any planned ``arr[:]`` materialization in the extraction
        path.  Default 256 MB matches the safe envelope on a 4 GB VPS at
        the documented 2-5 concurrent annotators.
    refuse_when_low_memory : bool
        When ``True`` (the default), refuse to materialize a volume if
        live ``MemAvailable`` is below ``volume_bytes * safety_factor``.
        Refusal raises a clean ``insufficient_memory`` server error
        instead of letting the OOM killer reap the process.
    safety_factor : float
        Multiplier on the raw volume size used to compute the required
        free-RAM headroom.  ``2.0`` covers the current
        ``arr[:] + tobytes()`` peak in :func:`extract_volume`; the
        segmentation path uses an internal bump to ``3.0`` to cover the
        extra ``pynrrd`` write copy.
    """

    max_safe_volume_mb: int = 256
    refuse_when_low_memory: bool = True
    safety_factor: float = 2.0


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
    memory : MemorySettings
        RAM-budget policy.  Optional in the TOML; defaults are tuned for
        a small VPS.
    """

    logging: LoggingSettings
    storage: StorageSettings
    memory: MemorySettings


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
        raise SettingsError('[storage].stores_dir is required but missing')
    stores_dir = Path(str(stores_dir_raw)).expanduser().resolve()
    if not stores_dir.is_dir():
        raise SettingsError(
            f'[storage].stores_dir does not exist or is not a directory: {stores_dir}'
        )

    staging_dir_raw = raw.get('staging_dir')
    if staging_dir_raw is None:
        staging_dir = Path(tempfile.gettempdir()).resolve()
    else:
        staging_dir = Path(str(staging_dir_raw)).expanduser().resolve()
        if not staging_dir.is_dir():
            raise SettingsError(
                f'[storage].staging_dir does not exist or is not a directory: '
                f'{staging_dir}'
            )
        if not os.access(staging_dir, os.W_OK):
            raise SettingsError(f'[storage].staging_dir is not writable: {staging_dir}')

    # Operator misconfiguration guard: equal paths would let ``gc`` rmtree
    # inside ``stores_dir`` on any reaped staging dir that happens to sit
    # at the same root.  This is the hard failure mode worth rejecting at
    # load time; the nesting cases (one inside the other) are unusual and
    # left to operator vigilance — both the echoed-path validator and
    # ``gc``'s ``STAGING_DIR_PREFIX`` filter already keep them contained
    # to directories the server itself created.
    if staging_dir == stores_dir:
        raise SettingsError(
            '[storage].staging_dir must not equal [storage].stores_dir '
            f'(both resolve to {staging_dir})'
        )

    return StorageSettings(stores_dir=stores_dir, staging_dir=staging_dir)


def _parse_memory(raw: dict[str, object]) -> MemorySettings:
    defaults = MemorySettings()
    max_mb_raw = raw.get('max_safe_volume_mb', defaults.max_safe_volume_mb)
    refuse_raw = raw.get('refuse_when_low_memory', defaults.refuse_when_low_memory)
    factor_raw = raw.get('safety_factor', defaults.safety_factor)
    try:
        max_mb = int(max_mb_raw)  # type: ignore[arg-type]
        factor = float(factor_raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise SettingsError(
            f'[memory] section has malformed numeric value: {exc}'
        ) from exc
    if max_mb <= 0:
        raise SettingsError('[memory].max_safe_volume_mb must be positive')
    if factor < 1.0:
        raise SettingsError('[memory].safety_factor must be >= 1.0')
    return MemorySettings(
        max_safe_volume_mb=max_mb,
        refuse_when_low_memory=bool(refuse_raw),
        safety_factor=factor,
    )


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
            f'Server config at {config_path} is missing the [storage] section'
        )

    return ServerSettings(
        logging=_parse_logging(raw.get('logging', {})),  # type: ignore[arg-type]
        storage=_parse_storage(storage_raw),  # type: ignore[arg-type]
        memory=_parse_memory(raw.get('memory', {})),  # type: ignore[arg-type]
    )

"""Remote server configuration.

Stores the server connection details in ``~/.config/voxhub/server.json``.

The SSH interaction user (``SERVER_INTERACTION_USER``) is an infrastructure
constant — it matches the service account created by the deploy scripts and
must only be changed in lockstep with those scripts and the server's sshd
configuration.  It is deliberately not stored per-annotator.
"""

import json
from pathlib import Path
from typing import Self

import attrs

from voxhub_client.ssh import SshTarget

# Infrastructure constant — the OS user under which the voxhub server process
# runs and to which all annotators SSH.  Only change in lockstep with the
# deploy scripts (add-annotator.sh, deploy.sh) and sshd ForceCommand config.
SERVER_INTERACTION_USER: str = 'voxhub'

_CONFIG_DIR = Path.home() / '.config' / 'voxhub'
_SERVER_FILE = _CONFIG_DIR / 'server.json'


@attrs.define
class ServerConfig:
    """Remote server connection parameters.

    The SSH user is the shared infrastructure service account
    (``SERVER_INTERACTION_USER``) and is intentionally not stored here — it
    is an infrastructure constant, not a per-annotator setting.

    Parameters
    ----------
    host : str
        SSH hostname or IP address of the voxhub server.
    port : int | None
        SSH port override. ``None`` uses the SSH default (22).
    """

    host: str
    port: int | None = None

    def to_dict(self) -> dict[str, object]:
        """Serialize to a plain dict."""
        return {'host': self.host, 'port': self.port}

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> Self:
        """Deserialize from a plain dict."""
        port_raw = d.get('port')
        return cls(
            host=str(d['host']),
            port=int(port_raw) if port_raw is not None else None,  # type: ignore[arg-type]
        )

    def to_ssh_target(self) -> SshTarget:
        """Build an ``SshTarget`` using the infrastructure interaction user.

        Returns
        -------
        SshTarget
            Ready for use with ``SshRunner``.
        """
        return SshTarget(user=SERVER_INTERACTION_USER, host=self.host, port=self.port)

    @property
    def connection_string(self) -> str:
        """Human-readable SSH connection string, e.g. ``voxhub@host:2222``."""
        base = f'{SERVER_INTERACTION_USER}@{self.host}'
        return f'{base}:{self.port}' if self.port is not None else base


def get_server() -> ServerConfig:
    """Read the configured remote server.

    Returns
    -------
    ServerConfig

    Raises
    ------
    FileNotFoundError
        If no server has been configured.  The user should run
        ``voxhub set-server <host>`` first.
    """
    if not _SERVER_FILE.exists():
        msg = (
            f'Server not configured. Run '
            f"'voxhub set-server <host>' first.\n"
            f'Expected: {_SERVER_FILE}'
        )
        raise FileNotFoundError(msg)

    data = json.loads(_SERVER_FILE.read_text())
    return ServerConfig.from_dict(data)


def set_server(host: str, port: int | None = None) -> ServerConfig:
    """Set the remote server connection parameters.

    Parameters
    ----------
    host : str
        SSH hostname or IP address.
    port : int | None
        SSH port override. ``None`` uses the SSH default (22).

    Returns
    -------
    ServerConfig
        The newly stored configuration.
    """
    config = ServerConfig(host=host, port=port)
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _SERVER_FILE.write_text(json.dumps(config.to_dict(), indent=2) + '\n')
    return config

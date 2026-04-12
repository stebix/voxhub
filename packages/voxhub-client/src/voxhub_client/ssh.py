"""SSH transport for server communication.

Invokes ``voxhub-server`` commands over SSH and parses JSON responses.
"""

import getpass
import json
import subprocess
from typing import Any, Self

import attrs

from voxhub_schema import PROTOCOL_VERSION


class RemoteError(Exception):
    """Raised when the remote server returns an error."""

    def __init__(
        self, code: str, message: str, protocol_version: int | None = None
    ) -> None:
        self.code = code
        self.protocol_version = protocol_version
        super().__init__(f'[{code}] {message}')


class ProtocolMismatchError(RemoteError):
    """Raised when server and client protocol versions differ."""


@attrs.define
class SshTarget:
    """Parsed SSH target from ``user@host`` notation.

    The server owns its stores directory path (via
    ``[storage].stores_dir`` in the server TOML config) and the client
    no longer supplies it.  Accepted forms:

    * ``user@host``
    * ``host`` (user defaults to the local OS user)

    A trailing ``:/path`` is rejected — it's a relic of the old
    protocol and signals a caller that hasn't been updated.

    Parameters
    ----------
    user : str
        SSH username.
    host : str
        SSH hostname.
    port : int | None
        SSH port override.
    """

    user: str
    host: str
    port: int | None = None

    @classmethod
    def parse(cls, target: str) -> Self:
        """Parse a target string like ``user@host`` or ``host``.

        Parameters
        ----------
        target : str
            SSH target string (no trailing ``:/path``).

        Returns
        -------
        SshTarget

        Raises
        ------
        ValueError
            If the string cannot be parsed or still carries a trailing
            path component.
        """
        if ':' in target:
            msg = (
                f"Invalid SSH target '{target}'. Expected 'user@host' "
                f'or just host — the server reads its stores directory '
                f'from its own configuration.'
            )
            raise ValueError(msg)

        if '@' in target:
            user, host = target.split('@', 1)
        else:
            user = getpass.getuser()
            host = target

        if not host:
            msg = f"Invalid SSH target '{target}': host is empty."
            raise ValueError(msg)

        return cls(user=user, host=host)

    @property
    def ssh_destination(self) -> str:
        """SSH destination string (``user@host``)."""
        return f'{self.user}@{self.host}'


@attrs.define
class SshRunner:
    """Runs voxhub-server commands over SSH.

    Parameters
    ----------
    target : SshTarget
        Parsed SSH target.
    remote_command : str
        Name of the remote binary (default ``voxhub-server``).
    """

    target: SshTarget
    remote_command: str = 'voxhub-server'

    def _ssh_args(self) -> list[str]:
        """Build the base SSH command arguments."""
        args = ['ssh']
        if self.target.port is not None:
            args.extend(['-p', str(self.target.port)])
        args.extend(
            [
                '-o',
                'BatchMode=yes',
                self.target.ssh_destination,
                '--',
            ]
        )
        return args

    def run(
        self,
        *args: str,
        timeout: float | None = 60,
    ) -> dict[str, Any]:
        """Run a voxhub-server subcommand over SSH.

        Parameters
        ----------
        *args : str
            Command and arguments (e.g. ``'list-stores'``,
            ``'/data/zarr'``).
        timeout : float | None
            Command timeout in seconds.

        Returns
        -------
        dict[str, Any]
            Parsed JSON response.

        Raises
        ------
        RemoteError
            If the server returns a structured error.
        ProtocolMismatchError
            If protocol versions differ.
        subprocess.TimeoutExpired
            If the command times out.
        """
        cmd = [*self._ssh_args(), self.remote_command, *args]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        if result.returncode != 0 and not result.stdout.strip():
            msg = (
                result.stderr.strip() or f'SSH command failed (exit {result.returncode})'
            )
            raise RemoteError('ssh_failed', msg)

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            msg = (
                f'Failed to parse server response as JSON: {exc}\n'
                f'stdout: {result.stdout[:200]}'
            )
            raise RemoteError('parse_error', msg) from exc

        # Check for structured error envelope.
        if data.get('error'):
            raise RemoteError(
                code=data.get('code', 'unknown'),
                message=data.get('message', 'Unknown error'),
                protocol_version=data.get('protocol_version'),
            )

        # Check protocol version.
        server_version = data.get('protocol_version')
        if server_version is not None and server_version != PROTOCOL_VERSION:
            raise ProtocolMismatchError(
                code='protocol_mismatch',
                message=(
                    f'Server protocol version {server_version}, '
                    f'client expects {PROTOCOL_VERSION}. '
                    f'Update voxhub-schema on both sides.'
                ),
                protocol_version=server_version,
            )

        return data

    def mktemp(self) -> str:
        """Create a temp directory on the server.

        Returns
        -------
        str
            Path to the temp directory on the server.
        """
        cmd = [
            *self._ssh_args(),
            'mktemp',
            '-d',
            '-t',
            'dt-push-XXXXXXXX',
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            msg = result.stderr.strip() or 'Failed to create temp directory'
            raise RemoteError('mktemp_failed', msg)
        return result.stdout.strip()

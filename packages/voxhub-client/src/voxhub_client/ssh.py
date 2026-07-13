"""SSH transport for server communication.

Speaks the JSON-over-stdin RPC contract: the remote command is always
the constant ``voxhub-server rpc``; the request
``{"protocol_version": N, "method": ..., "params": {...}}`` travels on
the subprocess's stdin, and a single JSON response object comes back on
stdout.  No method or parameter ever appears in argv, so quoting and
injection concerns vanish by construction.
"""

import getpass
import json
import subprocess
from typing import Any, Self

import attrs

from voxhub_schema import PROTOCOL_VERSION

RPC_REMOTE_COMMAND: str = 'voxhub-server rpc'
"""The one and only remote command for RPC calls — no other argv ever."""

DEFAULT_TIMEOUT_S: float = 60.0
"""Fallback timeout for methods without an explicit policy entry."""

METHOD_TIMEOUTS_S: dict[str, float] = {
    'prepare-pull': 1800.0,
    'integrate-annotations': 1800.0,
}
"""Per-method timeout policy.

``prepare-pull`` stages a full volume server-side (RAM load + NRRD
write + sha256) and routinely exceeds the 60 s default on large
stores; ``integrate-annotations`` materializes the pushed
segmentation into RAM, validates it voxel-by-voxel, and writes it to
zarr under the store lock, so it gets the same headroom.  Everything
else (``list-stores``, ``prepare-push``, ``cleanup``,
``healthcheck``) is metadata-sized and keeps the short default.
"""


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
    """Runs voxhub-server RPC methods over SSH.

    Every call executes the constant remote command
    ``voxhub-server rpc`` and pipes one JSON request object to its
    stdin — methods and parameters never touch argv.

    Parameters
    ----------
    target : SshTarget
        Parsed SSH target.
    ssh_options : tuple[str, ...]
        Extra raw ssh arguments spliced into the command line before
        the destination — e.g. ``('-i', '/path/key', '-o',
        'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null')``.
        Intended for test harnesses (loopback sshd on a high port);
        production callers should leave it empty and configure ssh via
        ``~/.ssh/config``.
    """

    target: SshTarget
    ssh_options: tuple[str, ...] = ()

    def _ssh_args(self) -> list[str]:
        """Build the base SSH command arguments (up to and incl. ``--``)."""
        args = ['ssh']
        if self.target.port is not None:
            args.extend(['-p', str(self.target.port)])
        args.extend(['-o', 'BatchMode=yes'])
        args.extend(self.ssh_options)
        args.extend([self.target.ssh_destination, '--'])
        return args

    def run(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Invoke a voxhub-server RPC method over SSH.

        Parameters
        ----------
        method : str
            RPC method name (e.g. ``'prepare-pull'``).
        params : dict[str, Any]
            JSON-serializable method parameters; the serialized form of
            the method's schema request model where one exists.
        timeout : float | None
            Explicit timeout override in seconds.  ``None`` (default)
            applies the per-method policy: ``METHOD_TIMEOUTS_S`` if the
            method has an entry, else ``DEFAULT_TIMEOUT_S``.

        Returns
        -------
        dict[str, Any]
            Parsed JSON response.

        Raises
        ------
        RemoteError
            If the server returns a structured error, the response is
            unparseable, or the command times out.
        ProtocolMismatchError
            If the response is missing ``protocol_version`` or carries
            a different one.
        """
        payload = json.dumps(
            {
                'protocol_version': PROTOCOL_VERSION,
                'method': method,
                'params': params,
            }
        )
        cmd = [*self._ssh_args(), RPC_REMOTE_COMMAND]
        effective_timeout = (
            timeout
            if timeout is not None
            else METHOD_TIMEOUTS_S.get(method, DEFAULT_TIMEOUT_S)
        )

        try:
            result = subprocess.run(
                cmd,
                input=payload,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            msg = (
                f'server command timed out after {effective_timeout:g} s '
                f'(method {method!r})'
            )
            raise RemoteError('timeout', msg) from exc

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

        _check_protocol_version(data)
        return data


def _check_protocol_version(data: dict[str, Any]) -> None:
    """Enforce the protocol version contract on a success response.

    A missing field is treated as seriously as a mismatch — it is the
    exact signature of a wrong or outdated binary on the other end of
    the pipe (CLAUDE.md rule 4 promises an error, not a shrug).

    Raises
    ------
    ProtocolMismatchError
        If ``protocol_version`` is missing or differs from the
        client's ``PROTOCOL_VERSION``.
    """
    server_version = data.get('protocol_version')
    if server_version is None:
        raise ProtocolMismatchError(
            code='protocol_mismatch',
            message=(
                f'Server response is missing protocol_version — the remote '
                f'voxhub-server is outdated or not a voxhub server at all. '
                f'Client expects protocol version {PROTOCOL_VERSION}.'
            ),
            protocol_version=None,
        )
    if server_version != PROTOCOL_VERSION:
        raise ProtocolMismatchError(
            code='protocol_mismatch',
            message=(
                f'Server protocol version {server_version}, '
                f'client expects {PROTOCOL_VERSION}. '
                f'Update voxhub-schema on both sides.'
            ),
            protocol_version=server_version,
        )

"""Loopback transport shims for end-to-end pull tests.

Substitute for :class:`voxhub_client.ssh.SshRunner` and
:class:`voxhub_client.transfer.RsyncTransfer` so tests can drive the
real server entrypoint (``python -m voxhub_core.server.cli``) as a
local subprocess without requiring ``ssh``, ``sshd``, ``rsync``, or a
network listener.

The shims preserve the production control flow — JSON parsing,
``RemoteError`` on the error envelope, ``ProtocolMismatchError`` on
version drift, trailing-slash directory-contents copy semantics — so
the client orchestration code exercises the same branches it would
over a real SSH connection.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import attrs

from voxhub_client.ssh import (
    ProtocolMismatchError,
    RemoteError,
    SshTarget,
)
from voxhub_schema import PROTOCOL_VERSION


@attrs.define
class LoopbackSshRunner:
    """Drop-in substitute for :class:`voxhub_client.ssh.SshRunner`.

    ``run(*args)`` dispatches to
    ``[sys.executable, '-m', 'voxhub_core.server.cli', *args]`` with
    ``VOXHUB_SERVER_CONFIG`` pointing at the fixture-provided TOML.
    Stdout is parsed as one JSON object and returned; stderr is parsed
    as JSONL and the events are appended to ``last_stderr_events`` for
    the test to inspect (e.g. to assert on ``staging_dir_reaped`` with
    ``reason='client_ack'``).  Non-JSON stderr lines are silently
    skipped.

    Parameters
    ----------
    target : SshTarget
        Signature parity with the real runner; unused at runtime.
    server_config_path : Path
        Path to the server TOML the spawned subprocess should read.
    last_responses : list[dict]
        Parsed stdout dicts from each ``run()`` call, in order.  Used
        by tests that assert on the raw wire shape.
    last_stderr_events : list[dict]
        Structlog events emitted across all ``run()`` calls, in order.
    """

    target: SshTarget
    server_config_path: Path
    last_responses: list[dict[str, Any]] = attrs.Factory(list)
    last_stderr_events: list[dict[str, Any]] = attrs.Factory(list)

    def run(
        self,
        *args: str,
        timeout: float | None = 60,
    ) -> dict[str, Any]:
        """Invoke a server subcommand as a local subprocess.

        Mirrors :meth:`voxhub_client.ssh.SshRunner.run` error handling:
        ``RemoteError('ssh_failed', ...)`` on non-zero exit with empty
        stdout, ``RemoteError('parse_error', ...)`` on unparseable
        stdout, ``RemoteError(code, message)`` on the structured error
        envelope, ``ProtocolMismatchError`` on version drift.
        """
        cmd = [sys.executable, '-m', 'voxhub_core.server.cli', *args]
        env = os.environ.copy()
        env['VOXHUB_SERVER_CONFIG'] = str(self.server_config_path)
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )

        for raw_line in result.stderr.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                self.last_stderr_events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        if result.returncode != 0 and not result.stdout.strip():
            msg = (
                result.stderr.strip()
                or f'Server command failed (exit {result.returncode})'
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

        self.last_responses.append(data)

        if data.get('error'):
            raise RemoteError(
                code=data.get('code', 'unknown'),
                message=data.get('message', 'Unknown error'),
                protocol_version=data.get('protocol_version'),
            )

        server_version = data.get('protocol_version')
        if server_version is not None and server_version != PROTOCOL_VERSION:
            raise ProtocolMismatchError(
                code='protocol_mismatch',
                message=(
                    f'Server protocol version {server_version}, '
                    f'client expects {PROTOCOL_VERSION}.'
                ),
                protocol_version=server_version,
            )

        return data


@attrs.define
class LoopbackRsyncTransfer:
    """Drop-in substitute for :class:`voxhub_client.transfer.RsyncTransfer`.

    ``pull(remote_path, local_path)`` copies directory contents via
    :func:`shutil.copytree` with ``dirs_exist_ok=True``.  This matches
    the real rsync invocation at ``transfer.py:51-54``, which appends
    trailing slashes to both src and dst so the *contents* of the
    source directory land inside the destination.

    Parameters
    ----------
    target : SshTarget
        Signature parity with the real transfer; unused at runtime.
    """

    target: SshTarget

    def pull(
        self,
        remote_path: str,
        local_path: str,
        *,
        progress: bool = True,
    ) -> None:
        """Copy ``remote_path/*`` into ``local_path`` (trailing-slash semantics).

        ``rsync`` replaces an existing destination file via a temp-file +
        rename, so it overwrites even a read-only file as long as the
        parent directory is writable (as after a repeat pull, where
        ``_lock_session`` left the prior artefacts at 0o444).
        :func:`shutil.copy2` instead opens the destination for writing and
        would raise ``PermissionError`` on a 0o444 file, so remove
        overlapping destination files first to mirror rsync's
        inode-replacement semantics.  Destination-only files (e.g. the
        client-written ``.voxhub_pull.sha256`` sidecar) are left untouched,
        exactly as rsync-without-``--delete`` leaves them.
        """
        del progress  # signature parity with the real RsyncTransfer
        src = Path(remote_path)
        dst = Path(local_path)
        dst.mkdir(parents=True, exist_ok=True)
        for source_file in src.rglob('*'):
            if not source_file.is_file():
                continue
            existing = dst / source_file.relative_to(src)
            if existing.exists():
                existing.unlink()
        shutil.copytree(src, dst, dirs_exist_ok=True)

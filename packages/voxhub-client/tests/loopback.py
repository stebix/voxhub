"""Loopback transport shims for end-to-end pull and push tests.

Substitute for :class:`voxhub_client.ssh.SshRunner` and
:class:`voxhub_client.transfer.RsyncTransfer` so tests can drive the
real server entrypoint (``python -m voxhub_core.server.cli``) as a
local subprocess without requiring ``ssh``, ``sshd``, ``rsync``, or a
network listener.

The shims speak the JSON-over-stdin RPC wire contract exactly as
production does: one request object
``{"protocol_version": N, "method": ..., "params": {...}}`` goes to
the fake server's stdin, one JSON response object comes back on
stdout, and no method or parameter ever appears in argv.

The fake server is this very file executed as a script (see the
``__main__`` block): it ``os.execv``-s the real server's native ``rpc``
subcommand with stdin passing through untouched, mirroring the
production forced-command wrapper's rpc branch.

Error handling in :meth:`LoopbackSshRunner.run` mirrors production —
``RemoteError`` on the error envelope, ``ProtocolMismatchError`` on
missing or drifted ``protocol_version``, ``RemoteError('timeout')`` on
``subprocess.TimeoutExpired`` — so the client orchestration code
exercises the same branches it would over a real SSH connection.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import attrs

from voxhub_client.ssh import (
    DEFAULT_TIMEOUT_S,
    METHOD_TIMEOUTS_S,
    ProtocolMismatchError,
    RemoteError,
    SshTarget,
)
from voxhub_schema import PROTOCOL_VERSION


@attrs.define
class LoopbackSshRunner:
    """Drop-in substitute for :class:`voxhub_client.ssh.SshRunner`.

    ``run(method, params)`` serializes the RPC request exactly like the
    production runner and pipes it to the fake server subprocess
    (``[sys.executable, __file__]``) with ``VOXHUB_SERVER_CONFIG``
    pointing at the fixture-provided TOML.  Stdout is parsed as one
    JSON object and returned; stderr is parsed as JSONL and the events
    are appended to ``last_stderr_events`` for the test to inspect
    (e.g. to assert on ``staging_dir_reaped`` with
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
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Invoke an RPC method against the fake server subprocess.

        Mirrors :meth:`voxhub_client.ssh.SshRunner.run` error handling:
        ``RemoteError('timeout', ...)`` on ``TimeoutExpired``,
        ``RemoteError('ssh_failed', ...)`` on non-zero exit with empty
        stdout, ``RemoteError('parse_error', ...)`` on unparseable
        stdout, ``RemoteError(code, message)`` on the structured error
        envelope, ``ProtocolMismatchError`` on missing or drifted
        ``protocol_version``.
        """
        payload = json.dumps(
            {
                'protocol_version': PROTOCOL_VERSION,
                'method': method,
                'params': params,
            }
        )
        cmd = [sys.executable, str(Path(__file__).resolve())]
        env = os.environ.copy()
        env['VOXHUB_SERVER_CONFIG'] = str(self.server_config_path)
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
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            msg = (
                f'server command timed out after {effective_timeout:g} s '
                f'(method {method!r})'
            )
            raise RemoteError('timeout', msg) from exc

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
        if server_version is None:
            raise ProtocolMismatchError(
                code='protocol_mismatch',
                message=(
                    f'Server response is missing protocol_version; '
                    f'client expects {PROTOCOL_VERSION}.'
                ),
                protocol_version=None,
            )
        if server_version != PROTOCOL_VERSION:
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

    Like production rrsync (which the forced-command wrapper roots at
    the operator's staging root), ``remote_path`` is resolved relative
    to ``staging_root`` — leading slashes are stripped, exactly as
    rrsync does.  A client regression back to sending absolute staging
    paths therefore fails these loopback tests the same way it fails
    against real rrsync.

    Parameters
    ----------
    target : SshTarget
        Signature parity with the real transfer; unused at runtime.
    staging_root : Path
        Root the remote path is resolved against.  Defaults to
        :func:`tempfile.gettempdir`, matching the server settings
        default for ``[storage].staging_dir`` (the loopback fixture's
        ``server.toml`` does not set one).
    """

    target: SshTarget
    staging_root: Path = attrs.Factory(lambda: Path(tempfile.gettempdir()))

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
        # rrsync semantics: strip leading slashes, resolve under the root.
        src = self.staging_root / remote_path.lstrip('/')
        dst = Path(local_path)
        dst.mkdir(parents=True, exist_ok=True)
        for source_file in src.rglob('*'):
            if not source_file.is_file():
                continue
            existing = dst / source_file.relative_to(src)
            if existing.exists():
                existing.unlink()
        shutil.copytree(src, dst, dirs_exist_ok=True)

    def push(
        self,
        local_path: str,
        remote_path: str,
        *,
        progress: bool = True,
    ) -> None:
        """Copy ``local_path/*`` into ``staging_root/remote_path``.

        Mirrors the real ``RsyncTransfer.push`` (trailing-slash
        contents-into-dir semantics) with the same rrsync root-relative
        resolution as :meth:`pull` — a client regression back to
        pushing absolute staging paths fails here the same way it fails
        against real rrsync.  Symlinks in the source tree are skipped,
        mirroring the production ``--no-links`` flag (there is no
        legitimate symlink in a push).
        """
        del progress  # signature parity with the real RsyncTransfer
        src = Path(local_path)
        dst = self.staging_root / remote_path.lstrip('/')
        dst.mkdir(parents=True, exist_ok=True)
        for source_file in src.rglob('*'):
            if source_file.is_symlink() or not source_file.is_file():
                continue
            target = dst / source_file.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                target.unlink()
            shutil.copy2(source_file, target)


# -- Fake server (script mode) -------------------------------------------------


def _fake_server_main() -> None:
    """Exec the real server's ``rpc`` subcommand, stdin passing through.

    Mirrors the production forced-command wrapper's rpc branch: the RPC
    request on stdin reaches the real ``voxhub-server rpc`` dispatch
    untouched, and its stdout/stderr/exit code pass through verbatim —
    so request parsing, version checking, and method dispatch are
    exercised on the real server code path, not reimplemented here.
    """
    os.execv(
        sys.executable,
        [sys.executable, '-m', 'voxhub_core.server.cli', 'rpc'],
    )


if __name__ == '__main__':
    _fake_server_main()

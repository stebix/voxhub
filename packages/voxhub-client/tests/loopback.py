"""Loopback transport shims for end-to-end pull tests.

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
``__main__`` block): it reads the stdin request, validates
``protocol_version`` bidirectionally, then bridges the method to the
real server's per-method subcommand surface via ``os.execv``.  The
bridge exists only until the server's native ``rpc`` subcommand lands
(Task A1 of the transport plan); at integration time the ``__main__``
block collapses to an exec of ``voxhub-server rpc``.

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


# -- Fake server (script mode) -------------------------------------------------


def _write_error(code: str, message: str) -> None:
    """Emit a ``ServerError``-shaped envelope on stdout."""
    sys.stdout.write(
        json.dumps(
            {
                'protocol_version': PROTOCOL_VERSION,
                'error': True,
                'code': code,
                'message': message,
            }
        )
    )
    sys.stdout.write('\n')


def _legacy_argv(method: str, params: dict[str, Any]) -> list[str] | None:
    """Translate an RPC ``(method, params)`` pair to the legacy argv surface.

    Returns ``None`` for methods this bridge does not map — the caller
    answers with an ``unknown_method`` envelope, matching the pinned
    contract for the real ``rpc`` dispatch.
    """
    if method == 'list-stores':
        argv = ['list-stores']
        if params.get('if_version') is not None:
            argv += ['--if-version', str(params['if_version'])]
        return argv
    if method == 'prepare-pull':
        argv = ['prepare-pull', '--store', str(params['store_name'])]
        if params.get('compress'):
            argv.append('--compress')
        include = params.get('include_existing_annotations')
        if include:
            argv.append('--include-existing-annotations')
            argv.extend(str(p) for p in include)
        return argv
    if method == 'cleanup':
        return ['cleanup', str(params['staging_dir'])]
    if method == 'healthcheck':
        return ['healthcheck']
    return None


def _fake_server_main() -> None:
    """Read one RPC request from stdin and bridge it to the real server.

    Implements the request half of the pinned wire contract (read stdin
    to EOF, parse once, validate ``protocol_version`` bidirectionally,
    dispatch on ``method``), then ``os.execv``-s the real per-method
    server subcommand so its stdout/stderr/exit code pass through
    untouched.
    """
    raw = sys.stdin.read()
    try:
        request = json.loads(raw)
    except json.JSONDecodeError:
        _write_error('malformed_request', 'stdin is not a single JSON object')
        sys.exit(1)
    if not isinstance(request, dict):
        _write_error('malformed_request', 'stdin is not a single JSON object')
        sys.exit(1)

    client_version = request.get('protocol_version')
    if client_version != PROTOCOL_VERSION:
        _write_error(
            'protocol_mismatch',
            f'Client protocol version {client_version}, '
            f'server expects {PROTOCOL_VERSION}.',
        )
        sys.exit(1)

    method = request.get('method')
    params = request.get('params') or {}
    if not isinstance(method, str) or not isinstance(params, dict):
        _write_error('malformed_request', 'method/params have the wrong shape')
        sys.exit(1)

    argv = _legacy_argv(method, params)
    if argv is None:
        _write_error('unknown_method', f'Unknown RPC method: {method!r}')
        sys.exit(1)

    os.execv(
        sys.executable,
        [sys.executable, '-m', 'voxhub_core.server.cli', *argv],
    )


if __name__ == '__main__':
    _fake_server_main()

"""Loopback-sshd end-to-end suite (launch plan 1.4 — the regression net).

The loopback shims (``loopback.py``) deliberately bypass ssh/rsync/sshd;
this suite uses all three **for real**, on localhost, no root needed:

* a throwaway ``sshd`` on a free high port, launched as the current user,
* the repo's real ``scripts/deploy/voxhub-forced-command.sh`` as the
  per-key forced command (unrendered — env fallbacks supply the staging
  root and binary paths, exactly the contract the wrapper documents),
* the real ``rrsync`` confining the rsync branch (read-write since
  launch 4.3) to the staging root,
* the real ``SshRunner`` / ``RsyncTransfer`` / ``_run_pull`` /
  ``_run_push`` client code.

The ``authorized_keys`` entry mirrors ``scripts/deploy/add-annotator.sh``
verbatim (``command=``, ``environment="VOXHUB_ANNOTATOR=..."``, the
``no-*`` restriction flags), with additional ``environment=`` options
standing in for what deploy.sh renders/installs on a real server
(staging root, server binary, server config).  The test ``sshd_config``
therefore allowlists ``PermitUserEnvironment VOXHUB_*`` where production
allowlists only ``VOXHUB_ANNOTATOR``.

Everything is ``slow``-marked and skipped wholesale when ``sshd`` /
``ssh`` / ``ssh-keygen`` are missing; the rsync-branch tests additionally
skip when ``rsync`` / ``rrsync`` are unavailable (Debian ships rrsync in
the rsync package — possibly gzipped under ``/usr/share/doc/rsync/``, in
which case a copy is gunzipped into the fixture tmpdir).

Plan: docs/plans/c-transport-rpc-implementation-plan.md Task B1;
launch-readiness-implementation-plan.md 1.4.  Documented in
docs/testing/loopback-sshd-e2e.md.
"""

import argparse
import getpass
import gzip
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any

import attrs
import pytest
from _core_helpers import (  # pyright: ignore[reportMissingImports]
    create_zarr_store,
    default_seg_label_map,
    write_seg_nrrd,
)

from voxhub_client.cli import _compute_sha256
from voxhub_client.identity import Identity
from voxhub_client.server_config import ServerConfig
from voxhub_client.ssh import RemoteError, SshRunner, SshTarget
from voxhub_client.transfer import RsyncTransfer
from voxhub_schema import (
    PROTOCOL_VERSION,
    PreparePushResponse,
    PrepareResponse,
    PullManifest,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WRAPPER = _REPO_ROOT / 'scripts' / 'deploy' / 'voxhub-forced-command.sh'

# sshd is traditionally installed in sbin, which is often absent from a
# non-root user's PATH — check the conventional locations explicitly.
_SSHD = shutil.which('sshd') or next(
    (p for p in ('/usr/sbin/sshd', '/usr/local/sbin/sshd') if os.access(p, os.X_OK)),
    None,
)
_SSH = shutil.which('ssh')
_SSH_KEYGEN = shutil.which('ssh-keygen')
_RSYNC = shutil.which('rsync')


def _locate_rrsync() -> tuple[Path, bool] | None:
    """Find an rrsync we can execute, or a gzipped copy we can extract.

    Returns ``(path, needs_gunzip)`` or ``None``.  Debian ships rrsync
    with the rsync package: as a real binary on trixie+, as an
    executable script (sometimes only via ``/usr/share/rsync/scripts/``)
    or gzipped under ``/usr/share/doc/rsync/scripts/`` on
    bullseye/bookworm.
    """
    found = shutil.which('rrsync')
    if found is not None:
        return Path(found), False
    for candidate in (
        '/usr/bin/rrsync',
        '/usr/local/bin/rrsync',
        '/usr/share/rsync/scripts/rrsync',
        '/usr/share/doc/rsync/scripts/rrsync',
    ):
        if os.access(candidate, os.X_OK):
            return Path(candidate), False
    gz = Path('/usr/share/doc/rsync/scripts/rrsync.gz')
    if gz.is_file():
        return gz, True
    return None


_RRSYNC_SOURCE = _locate_rrsync()

pytestmark = [
    pytest.mark.slow,
    pytest.mark.e2e,
    pytest.mark.skipif(_SSHD is None, reason='sshd not available'),
    pytest.mark.skipif(_SSH is None, reason='ssh not available'),
    pytest.mark.skipif(_SSH_KEYGEN is None, reason='ssh-keygen not available'),
    pytest.mark.skipif(not _WRAPPER.is_file(), reason='wrapper script not found'),
]

# The rsync branch (real bulk transfer through rrsync) has two extra
# machine requirements; rpc-path tests run without them.
requires_rsync = pytest.mark.skipif(
    _RSYNC is None or _RRSYNC_SOURCE is None,
    reason='rsync and/or rrsync not available',
)

_ANNOTATOR = 'alice'
_STORE = 'alpha'


def _free_port() -> int:
    """Ask the kernel for a free loopback TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


@attrs.define
class _KeyedRsyncTransfer(RsyncTransfer):
    """Real ``RsyncTransfer`` with extra ssh options in the ``-e`` string.

    Production configures identity/known-hosts via ``~/.ssh/config``;
    the loopback harness has to splice them into rsync's remote shell
    the same way ``SshRunner.ssh_options`` does for the RPC path.  Only
    ``_ssh_option`` is overridden — the transfer logic under test is the
    real thing.
    """

    extra_ssh: str = ''

    def _ssh_option(self) -> str:
        return f'{super()._ssh_option()} {self.extra_ssh}'.strip()


@attrs.define
class SshdLoopback:
    """Handle onto the throwaway loopback sshd and its voxhub server env.

    Parameters
    ----------
    target : SshTarget
        Current user @ 127.0.0.1 on the fixture's high port.
    ssh_options : tuple[str, ...]
        Extra ssh arguments (identity file, known-hosts policy) for both
        ``SshRunner`` and raw ``ssh`` invocations.
    stores_dir, staging_root : Path
        The server-side storage layout referenced by ``server.toml``
        (``VOXHUB_SERVER_CONFIG``) and the wrapper's rrsync root
        (``VOXHUB_STAGING_ROOT``).
    sshd_log : Path
        ``sshd -E`` log file — first stop when debugging.
    """

    target: SshTarget
    ssh_options: tuple[str, ...]
    stores_dir: Path
    staging_root: Path
    server_config: Path
    sshd_log: Path

    def runner(self) -> SshRunner:
        """A real ``SshRunner`` wired to the loopback sshd."""
        return SshRunner(target=self.target, ssh_options=self.ssh_options)

    def transfer(self) -> _KeyedRsyncTransfer:
        """A real ``RsyncTransfer`` wired to the loopback sshd."""
        return _KeyedRsyncTransfer(
            target=self.target, extra_ssh=' '.join(self.ssh_options)
        )

    def raw_ssh(
        self, remote_command: str, *, stdin: str = ''
    ) -> subprocess.CompletedProcess[str]:
        """Run ``ssh ... <remote_command>`` verbatim (no client code)."""
        assert _SSH is not None
        cmd = [
            _SSH,
            '-p',
            str(self.target.port),
            '-o',
            'BatchMode=yes',
            *self.ssh_options,
            self.target.ssh_destination,
            remote_command,
        ]
        return subprocess.run(
            cmd, input=stdin, capture_output=True, text=True, timeout=60
        )

    def rsync_pull(
        self, remote_path: str, dest: Path
    ) -> subprocess.CompletedProcess[str]:
        """Run a raw ``rsync`` pull through the loopback connection."""
        assert _RSYNC is not None
        remote = remote_path if remote_path.endswith('/') else f'{remote_path}/'
        ssh_cmd = f'ssh -p {self.target.port} ' + ' '.join(self.ssh_options)
        cmd = [
            _RSYNC,
            '-az',
            '-e',
            ssh_cmd,
            f'{self.target.ssh_destination}:{remote}',
            f'{dest}/',
        ]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    def rsync_push(self, src: Path, remote_path: str) -> subprocess.CompletedProcess[str]:
        """Run a raw ``rsync`` push (no client code, symlinks preserved).

        Deliberately plain ``-az`` (which includes ``-l``): this is the
        hostile-client path — the voxhub client's ``--no-links`` is a
        courtesy, not a defense, so the raw invocation must be able to
        deliver symlinks for the server-side refusal tests.
        """
        assert _RSYNC is not None
        remote = remote_path if remote_path.endswith('/') else f'{remote_path}/'
        ssh_cmd = f'ssh -p {self.target.port} ' + ' '.join(self.ssh_options)
        cmd = [
            _RSYNC,
            '-az',
            '-e',
            ssh_cmd,
            f'{src}/',
            f'{self.target.ssh_destination}:{remote}',
        ]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    def staging_sessions(self) -> set[str]:
        """Names of the staging dirs currently minted under the root."""
        return {p.name for p in self.staging_root.iterdir() if p.is_dir()}

    def rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """One RPC round-trip through a fresh runner."""
        return self.runner().run(method, params)


@pytest.fixture(scope='module')
def sshd_loopback(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SshdLoopback]:
    """Launch a loopback sshd fronting the repo's forced-command wrapper.

    Recipe (launch plan 1.4): ed25519 host + client keys, sshd_config on
    a free high port with ``StrictModes no`` (tmpdirs fail ownership
    checks) and ``PermitUserEnvironment VOXHUB_*``, an authorized_keys
    line mirroring ``add-annotator.sh``, a seeded zarr store, then
    ``sshd -D -f <config>`` as the current user, port-polled, terminated
    in teardown.  sshd requires absolute paths throughout.
    """
    root = tmp_path_factory.mktemp('sshd_loopback')

    # -- server binary (the uv venv entrypoint) -----------------------------
    voxhub_server = Path(sys.executable).parent / 'voxhub-server'
    if not voxhub_server.is_file():
        pytest.skip(f'voxhub-server entrypoint not found at {voxhub_server}')

    # -- rrsync (optional: only the rsync branch needs it) ------------------
    rrsync: Path | None = None
    if _RRSYNC_SOURCE is not None:
        source, needs_gunzip = _RRSYNC_SOURCE
        if needs_gunzip:
            rrsync = root / 'rrsync'
            rrsync.write_bytes(gzip.decompress(source.read_bytes()))
            rrsync.chmod(0o755)
        else:
            rrsync = source

    # -- keys ----------------------------------------------------------------
    assert _SSH_KEYGEN is not None
    host_key = root / 'host_key'
    client_key = root / 'client_key'
    for key in (host_key, client_key):
        subprocess.run(
            [_SSH_KEYGEN, '-q', '-t', 'ed25519', '-f', str(key), '-N', ''],
            check=True,
            capture_output=True,
        )

    # -- server-side storage --------------------------------------------------
    stores_dir = root / 'stores'
    staging_root = root / 'staging'
    stores_dir.mkdir()
    staging_root.mkdir()
    create_zarr_store(stores_dir / f'{_STORE}.zarr')

    server_config = root / 'server.toml'
    server_config.write_text(
        f"[storage]\nstores_dir = '{stores_dir}'\nstaging_dir = '{staging_root}'\n"
    )

    # -- authorized_keys (mirrors add-annotator.sh) ---------------------------
    # command= points at the REPO's unrendered wrapper; the extra
    # environment= options stand in for what deploy.sh renders/installs
    # (staging root token, /usr/local/bin binaries, /home/voxhub config).
    # sshd strips the environment, so everything the wrapper and server
    # need must arrive via PermitUserEnvironment-allowlisted options.
    pubkey = (client_key.parent / f'{client_key.name}.pub').read_text().strip()
    env_opts = [
        f'VOXHUB_ANNOTATOR={_ANNOTATOR}',
        f'VOXHUB_STAGING_ROOT={staging_root}',
        f'VOXHUB_SERVER={voxhub_server}',
        f'VOXHUB_SERVER_CONFIG={server_config}',
    ]
    if rrsync is not None:
        env_opts.append(f'VOXHUB_RRSYNC={rrsync}')
    key_opts = ','.join(
        [
            f'command="{_WRAPPER}"',
            *(f'environment="{opt}"' for opt in env_opts),
            'no-port-forwarding',
            'no-X11-forwarding',
            'no-agent-forwarding',
            'no-pty',
        ]
    )
    authorized_keys = root / 'authorized_keys'
    authorized_keys.write_text(f'{key_opts} {pubkey} annotator:{_ANNOTATOR}\n')
    authorized_keys.chmod(0o600)

    # -- sshd ------------------------------------------------------------------
    port = _free_port()
    sshd_log = root / 'sshd.log'
    sshd_config = root / 'sshd_config'
    sshd_config.write_text(
        f'Port {port}\n'
        f'ListenAddress 127.0.0.1\n'
        f'HostKey {host_key}\n'
        f'PidFile {root / "sshd.pid"}\n'
        f'AuthorizedKeysFile {authorized_keys}\n'
        f'StrictModes no\n'
        f'PasswordAuthentication no\n'
        f'ChallengeResponseAuthentication no\n'
        f'PubkeyAuthentication yes\n'
        f'PermitUserEnvironment VOXHUB_*\n'
        f'UsePAM no\n'
        f'LogLevel DEBUG1\n'
    )

    assert _SSHD is not None
    proc = subprocess.Popen(
        [_SSHD, '-D', '-f', str(sshd_config), '-E', str(sshd_log)],
    )
    try:
        deadline = time.monotonic() + 15.0
        while True:
            if proc.poll() is not None:
                log = sshd_log.read_text() if sshd_log.exists() else '(no log)'
                pytest.fail(f'sshd exited during startup (rc={proc.returncode}):\n{log}')
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=0.25):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    log = sshd_log.read_text() if sshd_log.exists() else '(no log)'
                    pytest.fail(f'sshd did not come up on port {port}:\n{log}')
                time.sleep(0.1)

        known_hosts = root / 'known_hosts'
        ssh_options = (
            '-i',
            str(client_key),
            '-o',
            'IdentitiesOnly=yes',
            '-o',
            'StrictHostKeyChecking=no',
            '-o',
            f'UserKnownHostsFile={known_hosts}',
        )
        yield SshdLoopback(
            target=SshTarget(user=getpass.getuser(), host='127.0.0.1', port=port),
            ssh_options=ssh_options,
            stores_dir=stores_dir,
            staging_root=staging_root,
            server_config=server_config,
            sshd_log=sshd_log,
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


@pytest.fixture
def e2e_pull_env(
    sshd_loopback: SshdLoopback,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> SimpleNamespace:
    """Wire the real ``_run_pull`` to the loopback sshd.

    Same monkeypatch surface as the loopback suite's ``loopback_pull_env``
    (module-scope names on ``voxhub_client.cli``), but every transport is
    the real one — the factories only inject the loopback port, client
    key, and known-hosts policy that production would get from
    ``~/.ssh/config``.
    """
    import voxhub_client.cli as client_cli
    from voxhub_client import pull_log as client_pull_log

    lb = sshd_loopback
    monkeypatch.setattr(client_pull_log, '_PULL_LOG', tmp_path / 'pulls.jsonl')

    identity = Identity(annotator_id=_ANNOTATOR, nano_id='e2e12345', machine_id='m-e2e')
    server = ServerConfig(host=lb.target.host, port=lb.target.port)
    monkeypatch.setattr(client_cli, 'get_identity', lambda: identity)
    monkeypatch.setattr(client_cli, 'get_server', lambda: server)
    monkeypatch.setattr(client_cli, 'SshRunner', lambda target: lb.runner())
    monkeypatch.setattr(client_cli, 'RsyncTransfer', lambda target: lb.transfer())

    def run_pull(dest: Path, store: str = _STORE) -> None:
        ns = argparse.Namespace(
            store=store,
            dest=str(dest),
            compress=False,
            include_existing_annotations=None,
        )
        client_cli._run_pull(ns)

    return SimpleNamespace(run_pull=run_pull, lb=lb)


_SEG_SEGMENTS: list[dict[str, object]] = [
    {'id': 's0', 'name': 'cochlea', 'label_value': 1, 'color': '1 0 0'},
    {'id': 's1', 'name': 'vestibule', 'label_value': 2, 'color': '0 1 0'},
    {'id': 's2', 'name': 'semicircular_canals', 'label_value': 3, 'color': '0 0 1'},
]


@pytest.fixture
def e2e_push_env(
    e2e_pull_env: SimpleNamespace,
    tmp_path: Path,
) -> SimpleNamespace:
    """A real pulled session plus a ``run_push`` over the same transports.

    The zarr store and provenance log are module-scoped (shared with the
    other tests), so the helpers report *deltas*: capture state before
    the push, assert on what changed.
    """
    import voxhub_client.cli as client_cli

    lb = e2e_pull_env.lb
    session = tmp_path / 'push-session'
    session.mkdir()
    e2e_pull_env.run_pull(session)

    def _write_seg(name: str = 'my-work.seg.nrrd') -> Path:
        return write_seg_nrrd(session / name, default_seg_label_map(), _SEG_SEGMENTS)

    def _run_push(
        *,
        ontology: list[str] | None = None,
        unconstrained: bool = False,
        validate_only: bool = False,
        force: bool = False,
    ) -> None:
        ns = argparse.Namespace(
            session_dir=str(session),
            ontology=ontology,
            unconstrained=unconstrained,
            validate_only=validate_only,
            force=force,
        )
        client_cli._run_push(ns)

    zarr_path = lb.stores_dir / f'{_STORE}.zarr'
    provenance_path = lb.stores_dir / '.meta' / 'provenance.jsonl'

    def _annotation_instances() -> set[str]:
        """Server-side annotation instance dirs for this suite's annotator."""
        slug_dir = zarr_path / 'annotations' / f'{_ANNOTATOR}-e2e12345'
        if not slug_dir.is_dir():
            return set()
        return {p.name for p in slug_dir.iterdir() if p.is_dir()}

    def _provenance_lines() -> list[str]:
        if not provenance_path.is_file():
            return []
        return provenance_path.read_text().splitlines()

    return SimpleNamespace(
        lb=lb,
        session=session,
        zarr_path=zarr_path,
        write_seg=_write_seg,
        run_push=_run_push,
        annotation_instances=_annotation_instances,
        provenance_lines=_provenance_lines,
    )


def _assert_valid_session(dest: Path) -> PullManifest:
    """Common post-pull assertions: manifest, checksums, trust sidecar."""
    manifest = PullManifest.read(dest)
    assert manifest.protocol_version == PROTOCOL_VERSION
    assert manifest.store_name == _STORE

    raw = dest / manifest.raw_name
    assert raw.is_file()
    assert _compute_sha256(raw) == manifest.raw_checksum

    sidecar = dest / '.voxhub_pull.sha256'
    assert sidecar.is_file()
    assert sidecar.read_text().strip() == _compute_sha256(dest / '.voxhub_pull.json')
    return manifest


class TestRpcPath:
    """RPC branch through real sshd + wrapper (no rsync required)."""

    def test_list_stores_end_to_end(self, sshd_loopback: SshdLoopback):
        """list-stores returns the seeded store with the current protocol."""
        response = sshd_loopback.rpc('list-stores', {})
        assert response['protocol_version'] == PROTOCOL_VERSION
        names = [s['name'] for s in response['stores']]
        assert names == [_STORE]

    def test_forbidden_subcommand_rejected(self, sshd_loopback: SshdLoopback):
        """Raw ``ssh ... 'voxhub-server gc'`` yields the forbidden envelope."""
        proc = sshd_loopback.raw_ssh('voxhub-server gc')
        assert proc.returncode == 1
        envelope = json.loads(proc.stdout)
        assert envelope['error'] is True
        assert envelope['code'] == 'forbidden'
        assert envelope['protocol_version'] == PROTOCOL_VERSION

    def test_key_bound_identity_reaches_server(self, sshd_loopback: SshdLoopback):
        """The authorized_keys ``environment="VOXHUB_ANNOTATOR=..."`` option
        survives sshd + wrapper exec and is authoritative in the server.

        A client-sent ``annotator_id`` that disagrees with the key-bound
        identity must be refused — which is only possible if the env var
        actually arrived.  The agreeing spelling succeeds.
        """
        lb = sshd_loopback
        with pytest.raises(RemoteError) as excinfo:
            lb.rpc('prepare-pull', {'store_name': _STORE, 'annotator_id': 'mallory'})
        assert excinfo.value.code == 'identity_mismatch'
        assert _ANNOTATOR in str(excinfo.value)

        response = lb.rpc(
            'prepare-pull', {'store_name': _STORE, 'annotator_id': _ANNOTATOR}
        )
        prepare = PrepareResponse.from_dict(response)
        assert Path(prepare.staging_dir).is_dir()
        lb.rpc('cleanup', {'staging_dir': prepare.staging_dir})


class TestRsyncPath:
    """Bulk-transfer branch through real sshd + wrapper + rrsync."""

    @requires_rsync
    def test_pull_end_to_end(self, e2e_pull_env: SimpleNamespace, tmp_path: Path):
        """Full ``_run_pull``: NRRD lands, checksums verify, manifest and
        sidecar written, and the server staging dir is reaped by the ACK."""
        lb = e2e_pull_env.lb
        sessions_before = lb.staging_sessions()

        dest = tmp_path / 'session'
        dest.mkdir()
        e2e_pull_env.run_pull(dest)

        _assert_valid_session(dest)
        # Delivery-ACK cleanup removed the staging dir this pull minted.
        assert lb.staging_sessions() == sessions_before

    @requires_rsync
    def test_pull_with_spaces_in_dest(
        self, e2e_pull_env: SimpleNamespace, tmp_path: Path
    ):
        """A local destination containing spaces survives the real rsync."""
        dest = tmp_path / 'my annotations' / 'session one'
        dest.mkdir(parents=True)
        e2e_pull_env.run_pull(dest)
        _assert_valid_session(dest)

    @requires_rsync
    def test_rsync_confined_to_staging(self, sshd_loopback: SshdLoopback, tmp_path: Path):
        """rrsync serves the issued staging dir and nothing else.

        The issued session (addressed by its staging-root-relative name)
        transfers; the stores_dir — via absolute path or ``..``
        traversal — is refused, so annotators can never rsync zarr
        stores or provenance.
        """
        lb = sshd_loopback
        response = lb.rpc('prepare-pull', {'store_name': _STORE})
        staging_dir = PrepareResponse.from_dict(response).staging_dir

        try:
            # Issued staging dir, root-relative: succeeds.
            ok_dest = tmp_path / 'ok'
            ok_dest.mkdir()
            proc = lb.rsync_pull(Path(staging_dir).name, ok_dest)
            assert proc.returncode == 0, proc.stderr
            assert (ok_dest / 'raw.nrrd').is_file()
            assert (ok_dest / '.voxhub_pull.json').is_file()

            # stores_dir by absolute path: re-rooted under the staging
            # root by rrsync, so nothing exists to transfer.
            for name, remote in [
                ('abs', str(lb.stores_dir)),
                ('dotdot', f'../{lb.stores_dir.name}'),
            ]:
                denied_dest = tmp_path / f'denied-{name}'
                denied_dest.mkdir()
                proc = lb.rsync_pull(remote, denied_dest)
                assert proc.returncode != 0, f'rsync of {remote!r} must fail, got rc=0'
                assert list(denied_dest.iterdir()) == []
        finally:
            lb.rpc('cleanup', {'staging_dir': staging_dir})


class TestPushPath:
    """Full push over real sshd + wrapper + writable rrsync (launch 4.4).

    This is the final launch gate: pull → annotate (simulated) → push,
    over real ssh + rsync + forced command.
    """

    @requires_rsync
    def test_push_end_to_end(self, e2e_push_env: SimpleNamespace):
        """Full ``_run_push``: the annotation lands in the server zarr with
        provenance attrs (forced flag absent), .meta/provenance.jsonl
        gains exactly one line, and the push staging dir is reaped."""
        env = e2e_push_env
        lb = env.lb
        instances_before = env.annotation_instances()
        provenance_before = env.provenance_lines()
        sessions_before = lb.staging_sessions()

        seg = env.write_seg()
        seg_digest = _compute_sha256(seg)
        env.run_push(ontology=['inner-ear-structures'])

        # -- annotation group landed at the annotator-scoped path ---------
        new_instances = env.annotation_instances() - instances_before
        assert len(new_instances) == 1
        instance = new_instances.pop()
        assert instance.startswith('inner-ear-structures-')

        # -- zarr attrs carry provenance; forced flag absent ---------------
        import zarr

        root = zarr.open_group(env.zarr_path, mode='r')
        arr = root[f'annotations/{_ANNOTATOR}-e2e12345/{instance}/data']
        a = dict(arr.attrs)
        assert a['annotator_id'] == _ANNOTATOR
        assert a['machine_id'] == 'm-e2e'
        assert a['nano_id'] == 'e2e12345'
        assert a['ontology'] == 'inner-ear-structures'
        assert a['source_nrrd_checksum'] == seg_digest
        # The key-bound VOXHUB_ANNOTATOR travelled sshd → wrapper → server.
        assert a['identity_source'] == 'ssh_key'
        assert 'forced' not in a

        # -- exactly one new provenance line --------------------------------
        new_lines = env.provenance_lines()[len(provenance_before) :]
        assert len(new_lines) == 1
        record = json.loads(new_lines[0])
        assert record['event'] == 'push'
        assert record['annotator_id'] == _ANNOTATOR
        assert record['identity_source'] == 'ssh_key'
        assert 'forced' not in record

        # -- push staging dir reaped by the cleanup ACK ---------------------
        assert lb.staging_sessions() == sessions_before

    @requires_rsync
    def test_push_rejects_bad_checksum(
        self,
        e2e_push_env: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ):
        """A file corrupted after checksum computation (simulated in-flight
        tamper) fails the store server-side; nothing is integrated."""
        import voxhub_client.cli as client_cli

        env = e2e_push_env
        lb = env.lb

        @attrs.define
        class _TamperingTransfer(_KeyedRsyncTransfer):
            """Corrupts the local upload mirror between checksum and rsync."""

            def push(
                self, local_path: str, remote_path: str, *, progress: bool = True
            ) -> None:
                for f in Path(local_path).rglob('*.seg.nrrd'):
                    f.write_bytes(f.read_bytes() + b'CORRUPTED-IN-FLIGHT')
                super().push(local_path, remote_path, progress=progress)

        monkeypatch.setattr(
            client_cli,
            'RsyncTransfer',
            lambda target: _TamperingTransfer(
                target=lb.target, extra_ssh=' '.join(lb.ssh_options)
            ),
        )

        instances_before = env.annotation_instances()
        provenance_before = env.provenance_lines()
        env.write_seg()

        with pytest.raises(SystemExit) as excinfo:
            env.run_push(ontology=['inner-ear-structures'])
        assert excinfo.value.code == 1

        captured = capsys.readouterr()
        assert 'Checksum mismatch' in captured.out
        assert 'failed' in captured.out

        # Nothing integrated: no new annotation, no new provenance line.
        assert env.annotation_instances() == instances_before
        assert env.provenance_lines() == provenance_before

    @requires_rsync
    def test_rsync_write_confined_to_staging(
        self, sshd_loopback: SshdLoopback, tmp_path: Path
    ):
        """Writable rrsync still confines writes to the staging root: a raw
        rsync write aimed at stores_dir never lands there."""
        lb = sshd_loopback
        payload = tmp_path / 'payload'
        payload.mkdir()
        (payload / 'evil.txt').write_text('injected\n')
        stores_before = sorted(p.name for p in lb.stores_dir.rglob('*'))

        # ``..`` traversal: refused outright by rrsync.
        proc = lb.rsync_push(payload, f'../{lb.stores_dir.name}')
        assert proc.returncode != 0

        # Absolute path: rrsync re-roots it under the staging root, so the
        # write may "succeed" — but only inside the confined area.
        lb.rsync_push(payload, str(lb.stores_dir))

        assert sorted(p.name for p in lb.stores_dir.rglob('*')) == stores_before
        assert not (lb.stores_dir / 'evil.txt').exists()

        # Hygiene: drop whatever the re-rooted absolute write left under
        # the module-scoped staging root.
        for leftover in lb.staging_root.iterdir():
            if not leftover.name.startswith('vxhb-staging-'):
                shutil.rmtree(leftover, ignore_errors=True)

    @requires_rsync
    def test_symlink_via_raw_rsync_is_refused_by_integrate(
        self, sshd_loopback: SshdLoopback, tmp_path: Path
    ):
        """A symlink planted in staging via raw rsync (bypassing the
        client's --no-links) is refused by integrate-annotations with
        code='invalid_staging_content'; nothing is integrated."""
        lb = sshd_loopback
        response = lb.rpc('prepare-push', {})
        staging_dir = PreparePushResponse.from_dict(response).staging_dir

        try:
            # Local payload: <store>/segmentation.seg.nrrd is a symlink to
            # a server-side file inside the zarr store.
            payload = tmp_path / 'payload'
            store_dir = payload / _STORE
            store_dir.mkdir(parents=True)
            (store_dir / 'segmentation.seg.nrrd').symlink_to(
                lb.stores_dir / f'{_STORE}.zarr' / 'zarr.json'
            )

            proc = lb.rsync_push(payload, PurePosixPath(staging_dir).name)
            assert proc.returncode == 0, proc.stderr
            planted = Path(staging_dir) / _STORE / 'segmentation.seg.nrrd'
            assert planted.is_symlink(), 'test premise: the symlink must land'

            result = lb.rpc(
                'integrate-annotations',
                {
                    'staging_dir': staging_dir,
                    'annotator_id': _ANNOTATOR,
                    'machine_id': 'm-e2e',
                    'nano_id': 'e2e12345',
                    'expected_ontology': ['inner-ear-structures'],
                },
            )
            store_result = result['stores'][_STORE]
            assert store_result['status'] == 'failed'
            assert store_result['code'] == 'invalid_staging_content'
            assert store_result['annotations'] == []
            assert any('symlink' in i['message'] for i in store_result['issues'])
        finally:
            lb.rpc('cleanup', {'staging_dir': staging_dir})

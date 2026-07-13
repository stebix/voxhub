"""Tests for ``voxhub pull`` client logic.

No ``voxhub-core`` imports — these are client-unit tests.  The server
side is monkeypatched where needed; end-to-end integration belongs in
the e2e suite (future work).
"""

import argparse
import contextlib
import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from voxhub_client import cli as client_cli
from voxhub_client.cli import (
    ChecksumError,
    _compute_sha256,
    _lock_session,
    _run_pull,
    _verify_checksums,
    _write_trust_sidecar,
)
from voxhub_schema import (
    PROTOCOL_VERSION,
    PullAnnotationEntry,
    PullManifest,
)

# -- Fixtures ----------------------------------------------------------------


def _build_manifest(
    session_dir: Path,
    *,
    raw_name: str = 'raw.nrrd',
    annotations: list[PullAnnotationEntry] | None = None,
    protocol_version: int = PROTOCOL_VERSION,
) -> PullManifest:
    """Build a PullManifest aligned with existing files in *session_dir*.

    Computes the raw checksum from whatever bytes are at ``session_dir/raw_name``.
    """
    raw_path = session_dir / raw_name
    raw_checksum = _compute_sha256(raw_path)
    manifest = PullManifest(
        protocol_version=protocol_version,
        prepared_at='2026-04-14T12:00:00+00:00',
        server_host='server.example.com',
        server_stores_dir='/srv/voxhub/stores',
        store_name='patient-001',
        raw_name=raw_name,
        raw_checksum=raw_checksum,
        shape=[10, 12, 14],
        spacing_mm=[0.5, 0.5, 0.5],
        origin_lps=[-5.0, -6.0, -7.0],
        space_directions=[[0.5, 0, 0], [0, 0.5, 0], [0, 0, 0.5]],
        annotations=annotations or [],
    )
    manifest.write(session_dir)
    return manifest


def _seed_session(
    session_dir: Path,
    *,
    raw_bytes: bytes = b'fake-raw-volume-bytes',
    refs: dict[str, bytes] | None = None,
    raw_name: str = 'raw.nrrd',
    protocol_version: int = PROTOCOL_VERSION,
) -> PullManifest:
    """Create a pretend post-rsync session directory under *session_dir*."""
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / raw_name).write_bytes(raw_bytes)

    entries: list[PullAnnotationEntry] = []
    if refs:
        ref_dir = session_dir / 'reference'
        ref_dir.mkdir(exist_ok=True)
        for filename, payload in refs.items():
            ref_path = ref_dir / filename
            ref_path.write_bytes(payload)
            kind = 'segmentation' if filename.endswith('.seg.nrrd') else 'landmarks'
            entries.append(
                PullAnnotationEntry(
                    zarr_source_path=f'annotations/alice-xyz45678/{filename}',
                    kind=kind,
                    ontology='x',
                    ontology_version=1,
                    annotator_id='alice',
                    integrated_at='2026-01-01T00:00:00+00:00',
                    reference_filename=filename,
                    reference_checksum=_compute_sha256(ref_path),
                )
            )

    return _build_manifest(
        session_dir,
        raw_name=raw_name,
        annotations=entries,
        protocol_version=protocol_version,
    )


# -- _compute_sha256 ---------------------------------------------------------


class TestComputeSha256:
    def test_matches_hashlib(self, tmp_path):
        path = tmp_path / 'f'
        path.write_bytes(b'hello voxhub')
        assert _compute_sha256(path) == (
            'sha256:' + hashlib.sha256(b'hello voxhub').hexdigest()
        )


# -- _verify_checksums -------------------------------------------------------


class TestVerifyChecksums:
    def test_passes_on_match(self, tmp_path):
        m = _seed_session(tmp_path / 's', refs={'a.seg.nrrd': b'seg-bytes'})
        _verify_checksums(tmp_path / 's', m)

    def test_raw_missing_raises(self, tmp_path):
        session = tmp_path / 's'
        m = _seed_session(session)
        (session / m.raw_name).unlink()
        with pytest.raises(ChecksumError, match='raw volume missing'):
            _verify_checksums(session, m)

    def test_raw_mismatch_raises(self, tmp_path):
        session = tmp_path / 's'
        m = _seed_session(session)
        (session / m.raw_name).write_bytes(b'tampered')
        with pytest.raises(ChecksumError, match=r'raw\.nrrd: checksum mismatch'):
            _verify_checksums(session, m)

    def test_reference_missing_raises(self, tmp_path):
        session = tmp_path / 's'
        m = _seed_session(session, refs={'a.seg.nrrd': b'seg'})
        (session / 'reference' / 'a.seg.nrrd').unlink()
        with pytest.raises(ChecksumError, match='reference file missing'):
            _verify_checksums(session, m)

    def test_reference_mismatch_raises(self, tmp_path):
        session = tmp_path / 's'
        m = _seed_session(session, refs={'a.seg.nrrd': b'seg'})
        (session / 'reference' / 'a.seg.nrrd').write_bytes(b'tampered')
        with pytest.raises(ChecksumError, match='checksum mismatch'):
            _verify_checksums(session, m)

    def test_uses_manifest_raw_name(self, tmp_path):
        session = tmp_path / 's'
        m = _seed_session(session, raw_name='volume.nrrd')
        assert m.raw_name == 'volume.nrrd'
        _verify_checksums(session, m)


# -- _write_trust_sidecar ----------------------------------------------------


class TestWriteTrustSidecar:
    def test_writes_matching_digest(self, tmp_path):
        session = tmp_path / 's'
        _seed_session(session)
        digest = _write_trust_sidecar(session)
        sidecar = (session / '.voxhub_pull.sha256').read_text().strip()
        assert sidecar == digest
        expected = _compute_sha256(session / '.voxhub_pull.json')
        assert digest == expected

    def test_survives_rename(self, tmp_path):
        session = tmp_path / 's'
        _seed_session(session)
        _write_trust_sidecar(session)

        renamed = tmp_path / 'moved'
        session.rename(renamed)

        # Sidecar still matches the manifest in the new location.
        sidecar = (renamed / '.voxhub_pull.sha256').read_text().strip()
        actual = _compute_sha256(renamed / '.voxhub_pull.json')
        assert sidecar == actual

    def test_oserror_propagates(self, tmp_path, monkeypatch):
        session = tmp_path / 's'
        _seed_session(session)

        def boom(self, *_args, **_kwargs):
            raise OSError('read-only fs')

        monkeypatch.setattr(Path, 'write_text', boom)
        with pytest.raises(OSError, match='read-only fs'):
            _write_trust_sidecar(session)

    def test_refreshes_readonly_sidecar(self, tmp_path):
        """A read-only sidecar from a prior pull is refreshed, not fatal.

        ``_lock_session`` leaves ``.voxhub_pull.sha256`` at 0o444 after a
        pull; a repeat pull to the same dest must be able to rewrite it.
        """
        session = tmp_path / 's'
        _seed_session(session)
        first = _write_trust_sidecar(session)
        (session / '.voxhub_pull.sha256').chmod(0o444)

        # A second write onto the read-only sidecar must succeed.
        second = _write_trust_sidecar(session)
        assert second == first
        sidecar = (session / '.voxhub_pull.sha256').read_text().strip()
        assert sidecar == _compute_sha256(session / '.voxhub_pull.json')


# -- _lock_session -----------------------------------------------------------


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


class TestLockSession:
    def test_locks_manifest_sidecar_raw_and_references(self, tmp_path):
        session = tmp_path / 's'
        m = _seed_session(session, refs={'a.seg.nrrd': b'seg', 'b.mrk.json': b'lmk'})
        _write_trust_sidecar(session)
        _lock_session(session, m)

        assert _mode(session / '.voxhub_pull.json') == 0o444
        assert _mode(session / '.voxhub_pull.sha256') == 0o444
        assert _mode(session / m.raw_name) == 0o444
        for ref in (session / 'reference').iterdir():
            assert _mode(ref) == 0o444

    def test_raw_becomes_readonly(self, tmp_path):
        session = tmp_path / 's'
        m = _seed_session(session)
        _write_trust_sidecar(session)
        _lock_session(session, m)
        with pytest.raises(PermissionError):
            (session / m.raw_name).write_text('mutation')

    def test_session_root_still_writable(self, tmp_path):
        session = tmp_path / 's'
        m = _seed_session(session)
        _write_trust_sidecar(session)
        _lock_session(session, m)
        # Annotator can still create new files at session root.
        (session / 'my-new-annotation.seg.nrrd').write_text('new')

    def test_respects_manifest_raw_name(self, tmp_path):
        session = tmp_path / 's'
        m = _seed_session(session, raw_name='custom.nrrd')
        _write_trust_sidecar(session)
        _lock_session(session, m)
        assert _mode(session / 'custom.nrrd') == 0o444

    def test_oserror_on_chmod_is_non_fatal(self, tmp_path, monkeypatch):
        session = tmp_path / 's'
        m = _seed_session(session, refs={'a.seg.nrrd': b'seg'})
        _write_trust_sidecar(session)

        original = Path.chmod
        calls = {'n': 0}

        def flaky(self, mode):
            calls['n'] += 1
            if calls['n'] == 1:
                raise OSError('locked by another process')
            return original(self, mode)

        monkeypatch.setattr(Path, 'chmod', flaky)
        # Must not raise — at least one chmod fails, but others still run.
        _lock_session(session, m)

        # Reference file still got locked (subsequent chmod succeeded).
        assert _mode(session / 'reference' / 'a.seg.nrrd') == 0o444
        # Restore permissive mode so pytest tmp_path cleanup works.
        monkeypatch.setattr(Path, 'chmod', original)
        for p in [
            session / '.voxhub_pull.json',
            session / '.voxhub_pull.sha256',
            session / m.raw_name,
            session / 'reference',
            session / 'reference' / 'a.seg.nrrd',
        ]:
            with contextlib.suppress(OSError):
                p.chmod(0o755)


# -- _run_pull end-to-end (monkeypatched) ------------------------------------


class _FakeRunner:
    """Monkey substitute for SshRunner: scripted responses per RPC method."""

    def __init__(self, responses: list[object], *, cleanup_error: bool = False):
        self._responses = list(responses)
        self.calls: list[tuple[str, dict]] = []
        self._cleanup_error = cleanup_error

    def run(self, method, params, **_kwargs):
        self.calls.append((method, params))
        if method == 'cleanup' and self._cleanup_error:
            from voxhub_client.ssh import RemoteError

            raise RemoteError('ssh_failed', 'connection dropped')
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


class _FakeTransfer:
    """Monkey substitute for RsyncTransfer: fakes a 'delivered' session."""

    def __init__(
        self,
        *,
        seed_fn=None,
        fail: bool = False,
    ):
        self.seed_fn = seed_fn
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def pull(self, remote_path: str, local_path: str, **_kwargs) -> None:
        self.calls.append((remote_path, local_path))
        if self.fail:
            raise subprocess.CalledProcessError(23, ['rsync'])
        if self.seed_fn:
            self.seed_fn(Path(local_path))


def _pull_args(
    store: str = 'patient-001', dest: Path | None = None
) -> argparse.Namespace:
    return argparse.Namespace(
        store=store,
        dest=str(dest) if dest else None,
        compress=False,
        include_existing_annotations=None,
    )


def _prepare_response(staging_dir: str = '/tmp/vxhb-staging-xyz'):
    return {
        'protocol_version': PROTOCOL_VERSION,
        'staging_dir': staging_dir,
        'server_host': 'server.example.com',
        'server_stores_dir': '/srv/voxhub/stores',
        'store_name': 'patient-001',
        'raw_name': 'raw.nrrd',
        'raw_checksum': 'sha256:placeholder',
        'shape': [10, 12, 14],
        'spacing_mm': [0.5, 0.5, 0.5],
        'origin_lps': [-5.0, -6.0, -7.0],
        'space_directions': [[0.5, 0, 0], [0, 0.5, 0], [0, 0, 0.5]],
        'skipped_annotations': [],
    }


@pytest.fixture
def fake_identity(monkeypatch):
    from voxhub_client.identity import Identity

    def _get_identity():
        return Identity(
            annotator_id='alice',
            nano_id='xyz45678',
            machine_id='machine-abc',
        )

    monkeypatch.setattr(client_cli, 'get_identity', _get_identity)


@pytest.fixture
def fake_server(monkeypatch):
    from voxhub_client.server_config import ServerConfig

    def _get_server():
        return ServerConfig(host='server.example.com', port=None)

    monkeypatch.setattr(client_cli, 'get_server', _get_server)


@pytest.fixture
def isolate_pull_log(monkeypatch, tmp_path):
    """Redirect pull_log writes to a tmp path so tests don't pollute $HOME."""
    from voxhub_client import pull_log

    monkeypatch.setattr(pull_log, '_PULL_LOG', tmp_path / 'pulls.jsonl')


class TestRunPullHappyPath:
    def test_happy_path_writes_sidecar_and_logs(
        self,
        tmp_path,
        fake_identity,
        fake_server,
        isolate_pull_log,
        monkeypatch,
        capsys,
    ):
        dest = tmp_path / 'dest'
        transfer = _FakeTransfer(
            seed_fn=lambda p: _seed_session(p, refs={'a.seg.nrrd': b'seg'})
        )
        runner = _FakeRunner(
            responses=[
                _prepare_response(),
                {'protocol_version': PROTOCOL_VERSION, 'status': 'ok'},
            ]
        )

        monkeypatch.setattr(client_cli, 'SshRunner', lambda target: runner)
        monkeypatch.setattr(client_cli, 'RsyncTransfer', lambda target: transfer)

        _run_pull(_pull_args(dest=dest))

        # Sidecar exists and matches manifest.
        sidecar = (dest / '.voxhub_pull.sha256').read_text().strip()
        assert sidecar == _compute_sha256(dest / '.voxhub_pull.json')

        # Lock bits applied.
        assert _mode(dest / '.voxhub_pull.json') == 0o444
        assert _mode(dest / 'raw.nrrd') == 0o444
        assert _mode(dest / 'reference' / 'a.seg.nrrd') == 0o444

        # prepare-pull request is the serialized PrepareRequest model.
        assert runner.calls[0] == (
            'prepare-pull',
            {
                'store_name': 'patient-001',
                'include_existing_annotations': None,
                'compress': False,
                'annotator_id': None,
            },
        )

        # Cleanup was called with the staging dir as a params dict.
        assert ('cleanup', {'staging_dir': '/tmp/vxhb-staging-xyz'}) in runner.calls

        # Audit log entry was written.
        from voxhub_client import pull_log as pl

        entries = [
            json.loads(line) for line in pl._PULL_LOG.read_text().splitlines() if line
        ]
        assert len(entries) == 1
        e = entries[0]
        assert e['store'] == 'patient-001'
        assert e['dest'] == str(dest)
        assert e['annotator_id'] == 'alice'
        assert e['protocol_version'] == PROTOCOL_VERSION
        assert 'manifest_sha256' not in e  # the log is never a trust surface.

        # Restore writable mode so pytest cleanup works.
        for p in [
            dest / '.voxhub_pull.json',
            dest / '.voxhub_pull.sha256',
            dest / 'raw.nrrd',
            dest / 'reference',
            dest / 'reference' / 'a.seg.nrrd',
        ]:
            if p.exists():
                os.chmod(p, 0o755)

    def test_skipped_annotations_shown_in_summary(
        self,
        tmp_path,
        fake_identity,
        fake_server,
        isolate_pull_log,
        monkeypatch,
        capsys,
    ):
        dest = tmp_path / 'dest'
        transfer = _FakeTransfer(seed_fn=lambda p: _seed_session(p))
        resp = _prepare_response()
        resp['skipped_annotations'] = [
            {
                'path': 'annotations/alice-xyz45678/broken-inst',
                'reason': 'attribute missing',
            }
        ]
        runner = _FakeRunner(
            responses=[resp, {'protocol_version': PROTOCOL_VERSION, 'status': 'ok'}]
        )
        monkeypatch.setattr(client_cli, 'SshRunner', lambda target: runner)
        monkeypatch.setattr(client_cli, 'RsyncTransfer', lambda target: transfer)

        _run_pull(_pull_args(dest=dest))

        captured = capsys.readouterr()
        assert 'Skipped annotations' in captured.out
        assert 'broken-inst' in captured.out
        assert 'attribute missing' in captured.out

        # Restore writable mode.
        for p in [
            dest / '.voxhub_pull.json',
            dest / '.voxhub_pull.sha256',
            dest / 'raw.nrrd',
        ]:
            if p.exists():
                os.chmod(p, 0o755)


class TestRunPullFailurePaths:
    def test_rsync_failure_exits_without_cleanup(
        self,
        tmp_path,
        fake_identity,
        fake_server,
        isolate_pull_log,
        monkeypatch,
    ):
        transfer = _FakeTransfer(fail=True)
        runner = _FakeRunner(responses=[_prepare_response()])
        monkeypatch.setattr(client_cli, 'SshRunner', lambda target: runner)
        monkeypatch.setattr(client_cli, 'RsyncTransfer', lambda target: transfer)

        with pytest.raises(SystemExit) as excinfo:
            _run_pull(_pull_args(dest=tmp_path / 'dest'))
        assert excinfo.value.code == 1
        # No cleanup was attempted.
        assert all(c[0] != 'cleanup' for c in runner.calls)

    def test_checksum_failure_exits_without_cleanup(
        self,
        tmp_path,
        fake_identity,
        fake_server,
        isolate_pull_log,
        monkeypatch,
    ):
        dest = tmp_path / 'dest'

        def tampered_seed(p: Path) -> None:
            m = _seed_session(p)
            # Corrupt raw after the fact so the checksum in the manifest
            # no longer matches.
            (p / m.raw_name).write_bytes(b'tampered')

        transfer = _FakeTransfer(seed_fn=tampered_seed)
        runner = _FakeRunner(responses=[_prepare_response()])
        monkeypatch.setattr(client_cli, 'SshRunner', lambda target: runner)
        monkeypatch.setattr(client_cli, 'RsyncTransfer', lambda target: transfer)

        with pytest.raises(SystemExit) as excinfo:
            _run_pull(_pull_args(dest=dest))
        assert excinfo.value.code == 1
        assert all(c[0] != 'cleanup' for c in runner.calls)

    def test_missing_manifest_exits_without_cleanup(
        self,
        tmp_path,
        fake_identity,
        fake_server,
        isolate_pull_log,
        monkeypatch,
    ):
        dest = tmp_path / 'dest'

        def empty_seed(p: Path) -> None:
            p.mkdir(parents=True, exist_ok=True)
            # No manifest written.

        transfer = _FakeTransfer(seed_fn=empty_seed)
        runner = _FakeRunner(responses=[_prepare_response()])
        monkeypatch.setattr(client_cli, 'SshRunner', lambda target: runner)
        monkeypatch.setattr(client_cli, 'RsyncTransfer', lambda target: transfer)

        with pytest.raises(SystemExit) as excinfo:
            _run_pull(_pull_args(dest=dest))
        assert excinfo.value.code == 1
        assert all(c[0] != 'cleanup' for c in runner.calls)

    def test_corrupt_manifest_exits_without_cleanup(
        self,
        tmp_path,
        fake_identity,
        fake_server,
        isolate_pull_log,
        monkeypatch,
        capsys,
    ):
        """Manifest present but malformed: client exits 1, no cleanup ACK,
        no traceback to the user."""
        dest = tmp_path / 'dest'

        def corrupt_seed(p: Path) -> None:
            p.mkdir(parents=True, exist_ok=True)
            # Manifest exists but the JSON is unparseable.
            (p / '.voxhub_pull.json').write_text('{not valid json')

        transfer = _FakeTransfer(seed_fn=corrupt_seed)
        runner = _FakeRunner(responses=[_prepare_response()])
        monkeypatch.setattr(client_cli, 'SshRunner', lambda target: runner)
        monkeypatch.setattr(client_cli, 'RsyncTransfer', lambda target: transfer)

        with pytest.raises(SystemExit) as excinfo:
            _run_pull(_pull_args(dest=dest))
        assert excinfo.value.code == 1
        assert all(c[0] != 'cleanup' for c in runner.calls)

        # User-facing message names the failure mode; no JSONDecodeError
        # traceback leaks through.
        captured = capsys.readouterr()
        assert 'unreadable or malformed' in captured.err

    def test_old_manifest_version_exits_with_clear_error(
        self,
        tmp_path,
        fake_identity,
        fake_server,
        isolate_pull_log,
        monkeypatch,
        capsys,
    ):
        """A manifest from an old server → clear re-pull message, exit 1,
        no traceback, no cleanup ACK."""
        dest = tmp_path / 'dest'
        stale_version = PROTOCOL_VERSION - 1

        transfer = _FakeTransfer(
            seed_fn=lambda p: _seed_session(p, protocol_version=stale_version)
        )
        runner = _FakeRunner(responses=[_prepare_response()])
        monkeypatch.setattr(client_cli, 'SshRunner', lambda target: runner)
        monkeypatch.setattr(client_cli, 'RsyncTransfer', lambda target: transfer)

        with pytest.raises(SystemExit) as excinfo:
            _run_pull(_pull_args(dest=dest))
        assert excinfo.value.code == 1
        assert all(c[0] != 'cleanup' for c in runner.calls)

        # Rich wraps long lines at word boundaries, so assert on tokens
        # that cannot straddle a soft line break.
        captured = capsys.readouterr()
        assert str(stale_version) in captured.err
        assert str(PROTOCOL_VERSION) in captured.err
        assert 're-pull' in captured.err
        assert 'Traceback' not in captured.err

    def test_cleanup_ack_failure_is_non_fatal(
        self,
        tmp_path,
        fake_identity,
        fake_server,
        isolate_pull_log,
        monkeypatch,
    ):
        dest = tmp_path / 'dest'
        transfer = _FakeTransfer(seed_fn=lambda p: _seed_session(p))
        runner = _FakeRunner(responses=[_prepare_response()], cleanup_error=True)
        monkeypatch.setattr(client_cli, 'SshRunner', lambda target: runner)
        monkeypatch.setattr(client_cli, 'RsyncTransfer', lambda target: transfer)

        # No SystemExit — cleanup ACK failure is warned but swallowed.
        _run_pull(_pull_args(dest=dest))

        # Audit log still written.
        from voxhub_client import pull_log as pl

        assert pl._PULL_LOG.exists()

        for p in [
            dest / '.voxhub_pull.json',
            dest / '.voxhub_pull.sha256',
            dest / 'raw.nrrd',
        ]:
            if p.exists():
                os.chmod(p, 0o755)

    def test_remote_error_exits(
        self,
        tmp_path,
        fake_identity,
        fake_server,
        isolate_pull_log,
        monkeypatch,
    ):
        from voxhub_client.ssh import RemoteError

        runner = _FakeRunner(
            responses=[RemoteError('store_not_found', 'Store not found: ...')]
        )
        transfer = _FakeTransfer()
        monkeypatch.setattr(client_cli, 'SshRunner', lambda target: runner)
        monkeypatch.setattr(client_cli, 'RsyncTransfer', lambda target: transfer)

        with pytest.raises(SystemExit) as excinfo:
            _run_pull(_pull_args())
        assert excinfo.value.code == 1

    def test_dest_defaults_to_cwd(
        self,
        tmp_path,
        fake_identity,
        fake_server,
        isolate_pull_log,
        monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        captured: list[tuple[str, str]] = []

        def seed(p: Path) -> None:
            # Same as before but capture the path rsync was given.
            _seed_session(p)

        transfer = _FakeTransfer(seed_fn=seed)
        runner = _FakeRunner(
            responses=[
                _prepare_response(),
                {'protocol_version': PROTOCOL_VERSION, 'status': 'ok'},
            ]
        )
        monkeypatch.setattr(client_cli, 'SshRunner', lambda target: runner)
        monkeypatch.setattr(client_cli, 'RsyncTransfer', lambda target: transfer)

        _run_pull(_pull_args(dest=None))
        # rsync was given the cwd.
        assert transfer.calls[0][1] == str(tmp_path)
        captured.extend(transfer.calls)

        for p in [
            tmp_path / '.voxhub_pull.json',
            tmp_path / '.voxhub_pull.sha256',
            tmp_path / 'raw.nrrd',
        ]:
            if p.exists():
                os.chmod(p, 0o755)

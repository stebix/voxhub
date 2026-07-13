"""Tests for ``voxhub push`` client logic (launch plan 4.2).

Unit tests for the push helpers plus fake-transport orchestration tests
for ``_run_push``.  The zarr-touching end-to-end path lives in
``test_push_e2e.py`` (loopback shims) and ``test_e2e_sshd.py`` (real
sshd + rsync).

The valid annotation fixtures reuse the core test helpers
(``_core_helpers.py``, on ``sys.path`` via ``conftest.py``) so the
pull-manifest geometry matches what the canonical seg/landmark builders
produce.
"""

import argparse
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _core_helpers import (  # pyright: ignore[reportMissingImports]
    ORIGIN_LPS,
    SHAPE,
    SPACE_DIRECTIONS,
    SPACING_MM,
    default_lmk_labels,
    default_lmk_points,
    default_seg_label_map,
    write_mrk_json,
    write_seg_nrrd,
)

from voxhub_client import cli as client_cli
from voxhub_client.cli import (
    PushError,
    _compute_sha256,
    _discover_annotation_files,
    _match_files_to_ontologies,
    _push_checksum_entries,
    _resolve_ontology_declaration,
    _run_push,
    _verify_push_session,
    _write_trust_sidecar,
)
from voxhub_client.identity import Identity
from voxhub_client.server_config import ServerConfig
from voxhub_client.ssh import RemoteError
from voxhub_schema import (
    PROTOCOL_VERSION,
    UNCONSTRAINED_SEGMENTATION,
    PullAnnotationEntry,
    PullManifest,
    RemoteManifest,
)

_STORE = 'alpha'

_SEG_SEGMENTS: list[dict[str, object]] = [
    {'id': 's0', 'name': 'cochlea', 'label_value': 1, 'color': '1 0 0'},
    {'id': 's1', 'name': 'vestibule', 'label_value': 2, 'color': '0 1 0'},
    {'id': 's2', 'name': 'semicircular_canals', 'label_value': 3, 'color': '0 0 1'},
]


# -- Session seeding -----------------------------------------------------------


def _reference_entry(ontology: str, kind: str = 'segmentation') -> PullAnnotationEntry:
    """A manifest reference entry (files themselves are irrelevant to push)."""
    ext = '.seg.nrrd' if kind == 'segmentation' else '.mrk.json'
    return PullAnnotationEntry(
        zarr_source_path=f'annotations/bob-xyz45678/{ontology}-20260101-ab12',
        kind=kind,
        ontology=ontology,
        ontology_version=1,
        annotator_id='bob',
        integrated_at='2026-01-01T00:00:00+00:00',
        reference_filename=f'bob-xyz45678_{ontology}{ext}',
        reference_checksum='sha256:' + '0' * 64,
    )


def _seed_pull_session(
    session_dir: Path,
    *,
    manifest_annotations: list[PullAnnotationEntry] | None = None,
    protocol_version: int = PROTOCOL_VERSION,
    write_sidecar: bool = True,
) -> PullManifest:
    """Create a valid post-pull session dir: raw volume, manifest, sidecar.

    The spatial metadata matches the ``_core_helpers`` canonical geometry
    so annotation files from ``write_seg_nrrd`` / ``write_mrk_json``
    pass pre-flight validation against this manifest.
    """
    session_dir.mkdir(parents=True, exist_ok=True)
    raw_path = session_dir / 'raw.nrrd'
    raw_path.write_bytes(b'fake-raw-volume-bytes')

    manifest = PullManifest(
        protocol_version=protocol_version,
        prepared_at='2026-07-14T12:00:00+00:00',
        server_host='server.example.com',
        server_stores_dir='/srv/voxhub/stores',
        store_name=_STORE,
        raw_name='raw.nrrd',
        raw_checksum=_compute_sha256(raw_path),
        shape=list(SHAPE),
        spacing_mm=list(SPACING_MM),
        origin_lps=list(ORIGIN_LPS),
        space_directions=[list(row) for row in SPACE_DIRECTIONS],
        annotations=manifest_annotations or [],
    )
    manifest.write(session_dir)
    if write_sidecar:
        _write_trust_sidecar(session_dir)
    return manifest


def _write_valid_seg(session_dir: Path, name: str = 'my-work.seg.nrrd') -> Path:
    return write_seg_nrrd(session_dir / name, default_seg_label_map(), _SEG_SEGMENTS)


def _write_valid_lmk(session_dir: Path, name: str = 'my-points.mrk.json') -> Path:
    return write_mrk_json(session_dir / name, default_lmk_points(), default_lmk_labels())


# -- Fake transports -----------------------------------------------------------


class _FakeRunner:
    """Scripted SshRunner substitute recording every RPC call."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        self.calls.append((method, params))
        response = self.responses[method]
        if isinstance(response, Exception):
            raise response
        return response


class _FakeTransfer:
    """RsyncTransfer substitute recording pushes and their file listings."""

    def __init__(self) -> None:
        self.pushes: list[tuple[str, str, list[str]]] = []

    def push(self, local_path: str, remote_path: str, *, progress: bool = True) -> None:
        # Capture the upload layout NOW — the local mirror is a
        # TemporaryDirectory that is gone after _run_push returns.
        listing = sorted(
            str(p.relative_to(local_path))
            for p in Path(local_path).rglob('*')
            if p.is_file()
        )
        self.pushes.append((local_path, remote_path, listing))


def _integrated_response(store: str = _STORE) -> dict[str, Any]:
    return {
        'protocol_version': PROTOCOL_VERSION,
        'stores': {
            store: {
                'status': 'integrated',
                'annotations': [
                    {
                        'path': 'annotations/alice-push1234/'
                        'inner-ear-structures-20260714-ab12',
                        'ontology': 'inner-ear-structures',
                        'ontology_version': 1,
                    }
                ],
                'issues': [],
            }
        },
    }


_REMOTE_STAGING = '/srv/voxhub/staging/vxhb-staging-pushtest-x1'


def _standard_responses() -> dict[str, Any]:
    return {
        'prepare-push': {
            'protocol_version': PROTOCOL_VERSION,
            'staging_dir': _REMOTE_STAGING,
        },
        'integrate-annotations': _integrated_response(),
        'cleanup': {'protocol_version': PROTOCOL_VERSION, 'status': 'ok'},
    }


@pytest.fixture
def push_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Drive ``_run_push`` with all transports faked.

    ``run_push(**flags)`` invokes the real orchestrator against the
    seeded session; transports and identity/server lookups are module
    -scope monkeypatches, mirroring the pull test harness.
    """
    session_dir = tmp_path / 'session'
    responses = _standard_responses()

    identity = Identity(annotator_id='alice', nano_id='push1234', machine_id='m-push')
    server = ServerConfig(host='localhost', port=None)
    monkeypatch.setattr(client_cli, 'get_identity', lambda: identity)
    monkeypatch.setattr(client_cli, 'get_server', lambda: server)

    runners: list[_FakeRunner] = []
    transfers: list[_FakeTransfer] = []

    def _runner_factory(target: Any) -> _FakeRunner:
        runner = _FakeRunner(responses)
        runners.append(runner)
        return runner

    def _transfer_factory(target: Any) -> _FakeTransfer:
        transfer = _FakeTransfer()
        transfers.append(transfer)
        return transfer

    monkeypatch.setattr(client_cli, 'SshRunner', _runner_factory)
    monkeypatch.setattr(client_cli, 'RsyncTransfer', _transfer_factory)

    def _run(
        *,
        ontology: list[str] | None = None,
        unconstrained: bool = False,
        validate_only: bool = False,
        force: bool = False,
    ) -> None:
        ns = argparse.Namespace(
            session_dir=str(session_dir),
            ontology=ontology,
            unconstrained=unconstrained,
            validate_only=validate_only,
            force=force,
        )
        _run_push(ns)

    def _all_calls() -> list[tuple[str, dict[str, Any]]]:
        return [call for runner in runners for call in runner.calls]

    return SimpleNamespace(
        session_dir=session_dir,
        responses=responses,
        runners=runners,
        transfers=transfers,
        run_push=_run,
        all_calls=_all_calls,
    )


# -- _verify_push_session ------------------------------------------------------


class TestVerifyPushSession:
    def test_valid_session_returns_manifest(self, tmp_path):
        _seed_pull_session(tmp_path / 's')
        manifest = _verify_push_session(tmp_path / 's')
        assert manifest.store_name == _STORE

    def test_missing_manifest(self, tmp_path):
        (tmp_path / 's').mkdir()
        with pytest.raises(PushError, match='not a pull session'):
            _verify_push_session(tmp_path / 's')

    def test_protocol_version_drift(self, tmp_path):
        _seed_pull_session(tmp_path / 's', protocol_version=PROTOCOL_VERSION - 1)
        with pytest.raises(PushError, match='protocol version'):
            _verify_push_session(tmp_path / 's')

    def test_missing_sidecar(self, tmp_path):
        _seed_pull_session(tmp_path / 's', write_sidecar=False)
        with pytest.raises(PushError, match='trust sidecar missing'):
            _verify_push_session(tmp_path / 's')

    def test_tampered_manifest_fails_sidecar(self, tmp_path):
        session = tmp_path / 's'
        _seed_pull_session(session)
        manifest_path = session / '.voxhub_pull.json'
        data = json.loads(manifest_path.read_text())
        data['store_name'] = 'evil-store'
        manifest_path.write_text(json.dumps(data))
        with pytest.raises(PushError, match='trust sidecar'):
            _verify_push_session(session)

    def test_modified_raw_volume(self, tmp_path):
        session = tmp_path / 's'
        _seed_pull_session(session)
        (session / 'raw.nrrd').write_bytes(b'tampered raw')
        with pytest.raises(PushError, match='modified after pull'):
            _verify_push_session(session)


# -- _discover_annotation_files ------------------------------------------------


class TestDiscovery:
    def test_finds_files_including_nested(self, tmp_path):
        session = tmp_path / 's'
        nested = session / 'exports' / 'v2'
        nested.mkdir(parents=True)
        seg = _write_valid_seg(nested)
        lmk = _write_valid_lmk(session)
        (session / 'raw.nrrd').write_bytes(b'raw')  # never discovered

        assert _discover_annotation_files(session) == [seg, lmk]

    def test_reference_dir_excluded(self, tmp_path):
        session = tmp_path / 's'
        ref = session / 'reference'
        ref.mkdir(parents=True)
        _write_valid_seg(ref, name='bob-xyz45678_pulled.seg.nrrd')
        seg = _write_valid_seg(session)

        assert _discover_annotation_files(session) == [seg]

    def test_no_files_is_an_error(self, tmp_path):
        session = tmp_path / 's'
        (session / 'reference').mkdir(parents=True)
        _write_valid_seg(session / 'reference', name='only-reference.seg.nrrd')
        with pytest.raises(PushError, match='no annotation files'):
            _discover_annotation_files(session)

    def test_multiple_segmentations_is_an_error(self, tmp_path):
        session = tmp_path / 's'
        session.mkdir()
        _write_valid_seg(session, name='one.seg.nrrd')
        _write_valid_seg(session, name='two.seg.nrrd')
        with pytest.raises(PushError, match='at most one segmentation'):
            _discover_annotation_files(session)

    def test_multiple_landmarks_is_an_error(self, tmp_path):
        session = tmp_path / 's'
        session.mkdir()
        _write_valid_lmk(session, name='one.mrk.json')
        _write_valid_lmk(session, name='two.mrk.json')
        with pytest.raises(PushError, match='at most one landmark'):
            _discover_annotation_files(session)

    def test_symlinked_annotation_is_an_error(self, tmp_path):
        session = tmp_path / 's'
        session.mkdir()
        real = _write_valid_seg(tmp_path, name='outside.seg.nrrd')
        (session / 'linked.seg.nrrd').symlink_to(real)
        with pytest.raises(PushError, match='symlink'):
            _discover_annotation_files(session)


# -- _resolve_ontology_declaration ----------------------------------------------


class TestOntologyResolution:
    def _manifest(self, tmp_path, annotations=None) -> PullManifest:
        return _seed_pull_session(tmp_path / 'm', manifest_annotations=annotations or [])

    def test_flags_are_mutually_exclusive(self, tmp_path):
        manifest = self._manifest(tmp_path)
        with pytest.raises(PushError, match='mutually exclusive'):
            _resolve_ontology_declaration(['x'], True, manifest)

    def test_explicit_flags_win(self, tmp_path):
        manifest = self._manifest(tmp_path, [_reference_entry('other-ontology')])
        declared, unconstrained = _resolve_ontology_declaration(
            ['inner-ear-structures'], False, manifest
        )
        assert declared == ['inner-ear-structures']
        assert unconstrained is False

    def test_unconstrained(self, tmp_path):
        manifest = self._manifest(tmp_path)
        assert _resolve_ontology_declaration(None, True, manifest) == ([], True)

    def test_derived_from_manifest_deduplicated(self, tmp_path):
        manifest = self._manifest(
            tmp_path,
            [
                _reference_entry('inner-ear-structures'),
                _reference_entry('inner-ear-structures'),
                _reference_entry('inner-ear-landmarks', kind='landmarks'),
            ],
        )
        declared, unconstrained = _resolve_ontology_declaration(None, False, manifest)
        assert declared == ['inner-ear-structures', 'inner-ear-landmarks']
        assert unconstrained is False

    def test_insufficient_manifest_requires_flags(self, tmp_path):
        manifest = self._manifest(tmp_path)
        with pytest.raises(PushError, match='no ontology declared'):
            _resolve_ontology_declaration(None, False, manifest)


# -- _match_files_to_ontologies --------------------------------------------------


class TestOntologyMatching:
    def test_multi_ontology_session(self, tmp_path):
        seg = _write_valid_seg(tmp_path)
        lmk = _write_valid_lmk(tmp_path)
        matched = _match_files_to_ontologies(
            [seg, lmk], ['inner-ear-structures', 'inner-ear-landmarks'], False
        )
        assert matched[seg].name == 'inner-ear-structures'
        assert matched[seg].type == 'segmentation'
        assert matched[lmk].name == 'inner-ear-landmarks'
        assert matched[lmk].type == 'landmarks'

    def test_unknown_ontology(self, tmp_path):
        seg = _write_valid_seg(tmp_path)
        with pytest.raises(PushError, match='unknown ontology'):
            _match_files_to_ontologies([seg], ['no-such-ontology'], False)

    def test_unmatched_annotation_type(self, tmp_path):
        lmk = _write_valid_lmk(tmp_path)
        with pytest.raises(PushError, match='matches no declared ontology'):
            _match_files_to_ontologies([lmk], ['inner-ear-structures'], False)

    def test_unconstrained_mapping(self, tmp_path):
        seg = _write_valid_seg(tmp_path)
        lmk = _write_valid_lmk(tmp_path)
        matched = _match_files_to_ontologies([seg, lmk], [], True)
        assert matched[seg] is UNCONSTRAINED_SEGMENTATION
        assert matched[lmk] is None


# -- _push_checksum_entries ------------------------------------------------------


class TestChecksumAssembly:
    def test_store_relative_paths_and_bare_hex(self, tmp_path):
        f1 = tmp_path / 'my-work.seg.nrrd'
        f1.write_bytes(b'seg-bytes')
        f2 = tmp_path / 'points.mrk.json'
        f2.write_bytes(b'lmk-bytes')

        entries = _push_checksum_entries([f1, f2], 'alpha')

        assert [e.path for e in entries] == [
            'alpha/my-work.seg.nrrd',
            'alpha/points.mrk.json',
        ]
        # Bare 64-hex digests — the 'sha256:' prefix is stripped for the
        # ChecksumEntry wire format.
        assert entries[0].sha256 == hashlib.sha256(b'seg-bytes').hexdigest()
        assert all(':' not in e.sha256 and len(e.sha256) == 64 for e in entries)


# -- --validate-only and validation gating ---------------------------------------


class TestValidateOnly:
    def test_short_circuits_with_zero_ssh_calls(self, push_env, capsys):
        _seed_pull_session(push_env.session_dir)
        _write_valid_seg(push_env.session_dir)

        push_env.run_push(ontology=['inner-ear-structures'], validate_only=True)

        assert push_env.all_calls() == []
        assert all(t.pushes == [] for t in push_env.transfers)
        out = capsys.readouterr().out
        assert 'validation passed' in out

    def test_validation_errors_abort_before_ssh(self, push_env, capsys):
        _seed_pull_session(push_env.session_dir)
        # Wrong shape → error-severity issue.
        import numpy as np

        write_seg_nrrd(
            push_env.session_dir / 'bad.seg.nrrd',
            np.zeros((4, 4, 4), dtype=np.int16),
            _SEG_SEGMENTS,
        )

        with pytest.raises(SystemExit) as excinfo:
            push_env.run_push(ontology=['inner-ear-structures'])
        assert excinfo.value.code == 1

        assert push_env.all_calls() == []
        err = capsys.readouterr().err
        assert 'validation error' in err

    def test_force_never_bypasses_errors(self, push_env):
        """--force accepts warnings only; error-severity issues still
        abort with zero SSH calls (deliberate deviation from the
        pre-rpc spec wording — matches the hardened server semantics)."""
        _seed_pull_session(push_env.session_dir)
        import numpy as np

        write_seg_nrrd(
            push_env.session_dir / 'bad.seg.nrrd',
            np.zeros((4, 4, 4), dtype=np.int16),
            _SEG_SEGMENTS,
        )

        with pytest.raises(SystemExit) as excinfo:
            push_env.run_push(ontology=['inner-ear-structures'], force=True)
        assert excinfo.value.code == 1
        assert push_env.all_calls() == []


# -- Full orchestration over fakes ------------------------------------------------


class TestPushOrchestration:
    def test_happy_path_wire_flow(self, push_env, capsys):
        _seed_pull_session(push_env.session_dir)
        seg = _write_valid_seg(push_env.session_dir)

        push_env.run_push(ontology=['inner-ear-structures'])

        methods = [m for m, _ in push_env.all_calls()]
        assert methods == ['prepare-push', 'integrate-annotations', 'cleanup']

        # prepare-push params are minimal.
        assert push_env.all_calls()[0][1] == {}

        # rsync addressed the staging dir by BASENAME (rrsync contract)
        # and uploaded <store>/<basename>.
        (_local, remote, listing) = push_env.transfers[0].pushes[0]
        assert remote == 'vxhb-staging-pushtest-x1'
        assert listing == [f'{_STORE}/{seg.name}']

        # integrate params: absolute staging dir verbatim, identity,
        # path-keyed bare-hex checksums, declared ontology, force off.
        params = push_env.all_calls()[1][1]
        assert params['staging_dir'] == _REMOTE_STAGING
        assert params['annotator_id'] == 'alice'
        assert params['machine_id'] == 'm-push'
        assert params['nano_id'] == 'push1234'
        assert params['expected_ontology'] == ['inner-ear-structures']
        assert params['unconstrained'] is False
        assert params['force'] is False
        assert params['checksums'] == [
            {
                'path': f'{_STORE}/{seg.name}',
                'sha256': _compute_sha256(seg).removeprefix('sha256:'),
            }
        ]

        # cleanup echoes the absolute staging dir verbatim.
        assert push_env.all_calls()[2][1] == {'staging_dir': _REMOTE_STAGING}

        out = capsys.readouterr().out
        assert 'integrated' in out
        assert 'push complete' in out

    def test_force_flag_forwarded(self, push_env):
        _seed_pull_session(push_env.session_dir)
        _write_valid_seg(push_env.session_dir)

        push_env.run_push(ontology=['inner-ear-structures'], force=True)

        params = dict(push_env.all_calls())['integrate-annotations']
        assert params['force'] is True

    def test_manifest_status_flips_to_integrated(self, push_env):
        _seed_pull_session(push_env.session_dir)
        _write_valid_seg(push_env.session_dir)

        push_env.run_push(ontology=['inner-ear-structures'])

        local = RemoteManifest.read(push_env.session_dir)
        assert local.stores[_STORE].status == 'integrated'
        assert local.stores[_STORE].expected_ontologies == ['inner-ear-structures']
        assert local.pull_session_id == 'vxhb-staging-pushtest-x1'

    def test_failed_store_renders_issues_and_exits_nonzero(self, push_env, capsys):
        _seed_pull_session(push_env.session_dir)
        _write_valid_seg(push_env.session_dir)
        push_env.responses['integrate-annotations'] = {
            'protocol_version': PROTOCOL_VERSION,
            'stores': {
                _STORE: {
                    'status': 'failed',
                    'annotations': [],
                    'issues': [
                        {
                            'severity': 'error',
                            'message': 'Checksum mismatch for alpha/my-work.seg.nrrd',
                        }
                    ],
                }
            },
        }

        with pytest.raises(SystemExit) as excinfo:
            push_env.run_push(ontology=['inner-ear-structures'])
        assert excinfo.value.code == 1

        captured = capsys.readouterr()
        assert 'Checksum mismatch' in captured.out
        assert 'failed' in captured.out
        # cleanup still ran (staging must not leak just because the
        # integration failed).
        assert [m for m, _ in push_env.all_calls()][-1] == 'cleanup'
        # The local manifest was NOT flipped.
        with pytest.raises(FileNotFoundError):
            RemoteManifest.read(push_env.session_dir)

    def test_cleanup_failure_is_nonfatal(self, push_env, capsys):
        _seed_pull_session(push_env.session_dir)
        _write_valid_seg(push_env.session_dir)
        push_env.responses['cleanup'] = RemoteError('boom', 'cleanup exploded')

        push_env.run_push(ontology=['inner-ear-structures'])  # no SystemExit

        captured = capsys.readouterr()
        assert 'warning' in captured.err
        assert 'GC will reap' in captured.err
        assert 'push complete' in captured.out

    def test_prepare_push_remote_error_is_fatal(self, push_env, capsys):
        _seed_pull_session(push_env.session_dir)
        _write_valid_seg(push_env.session_dir)
        push_env.responses['prepare-push'] = RemoteError('disk_full', 'no space')

        with pytest.raises(SystemExit) as excinfo:
            push_env.run_push(ontology=['inner-ear-structures'])
        assert excinfo.value.code == 1
        assert 'prepare-push failed' in capsys.readouterr().err
        # Nothing was uploaded or integrated.
        assert all(t.pushes == [] for t in push_env.transfers)
        assert [m for m, _ in push_env.all_calls()] == ['prepare-push']

    def test_malformed_prepare_push_response_is_fatal(self, push_env, capsys):
        _seed_pull_session(push_env.session_dir)
        _write_valid_seg(push_env.session_dir)
        push_env.responses['prepare-push'] = {'protocol_version': PROTOCOL_VERSION}

        with pytest.raises(SystemExit) as excinfo:
            push_env.run_push(ontology=['inner-ear-structures'])
        assert excinfo.value.code == 1
        assert 'malformed prepare-push response' in capsys.readouterr().err

    def test_stale_session_aborts_before_ssh(self, push_env, capsys):
        _seed_pull_session(push_env.session_dir)
        _write_valid_seg(push_env.session_dir)
        (push_env.session_dir / 'raw.nrrd').chmod(0o644)
        (push_env.session_dir / 'raw.nrrd').write_bytes(b'server data changed')

        with pytest.raises(SystemExit) as excinfo:
            push_env.run_push(ontology=['inner-ear-structures'])
        assert excinfo.value.code == 1
        assert push_env.all_calls() == []
        assert 'modified after pull' in capsys.readouterr().err

    def test_warnings_do_not_abort(self, push_env, capsys):
        """Warning-severity issues print but the push proceeds (server
        integrates warnings by default; --force only stamps)."""
        _seed_pull_session(push_env.session_dir)
        # A landmark outside the volume bbox is a warning, not an error.
        far_points = [[500.0, 500.0, 500.0], [-3.0, -4.0, -5.0], [-2.0, -3.0, -4.0]]
        write_mrk_json(
            push_env.session_dir / 'points.mrk.json', far_points, default_lmk_labels()
        )

        push_env.run_push(ontology=['inner-ear-landmarks'])

        assert [m for m, _ in push_env.all_calls()] == [
            'prepare-push',
            'integrate-annotations',
            'cleanup',
        ]
        assert 'warning' in capsys.readouterr().out

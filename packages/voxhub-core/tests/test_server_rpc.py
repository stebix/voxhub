"""Tests for the ``voxhub-server rpc`` subcommand (wire protocol v2).

Covers the pinned JSON-over-stdin contract from
``docs/plans/c-transport-rpc-implementation-plan.md``: golden
request/response pairs per method, error envelopes (malformed stdin,
unknown method, missing fields, protocol mismatch), injection-shaped
params round-tripping as literals, shim/rpc response equivalence, and
the path-keyed checksum regression (basename collision, triage
2026-07-11 P1).

Function-level tests drive ``_run_rpc`` with a monkeypatched stdin;
subprocess-level tests drive ``python -m voxhub_core.server.cli rpc``
with the request on real stdin to assert exit codes and the absence of
tracebacks.
"""

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _core_helpers import (
    build_staging_dir_entries,
    default_seg_label_map,
    write_seg_nrrd,
)

from voxhub_core.server import cli as server_cli
from voxhub_schema import (
    PROTOCOL_VERSION,
    IntegrateResponse,
    PreparePushResponse,
    PrepareResponse,
)

# ===========================================================================
# Helpers
# ===========================================================================


def _request(method: str, params: dict | None = None, **envelope) -> dict:
    """Build a well-formed rpc request envelope (overridable via kwargs)."""
    req: dict = {
        'protocol_version': PROTOCOL_VERSION,
        'method': method,
        'params': params or {},
    }
    req.update(envelope)
    return req


def _bare_hex(path: Path) -> str:
    """Bare 64-hex sha256 of a file (compute_sha256 returns 'sha256:<hex>')."""
    return server_cli.compute_sha256(path).removeprefix('sha256:')


@pytest.fixture
def run_rpc(server_argv, monkeypatch):
    """Invoke ``_run_rpc`` with a JSON payload on a fake stdin.

    Returns a callable ``(payload, **argv_overrides) -> None``.
    ``payload`` may be a dict (dumped to JSON) or a raw string (written
    verbatim, for malformed-stdin cases).
    """

    def _run(payload, **argv_overrides) -> None:
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        monkeypatch.setattr('sys.stdin', io.StringIO(raw))
        server_cli._run_rpc(server_argv(**argv_overrides))

    return _run


# ===========================================================================
# Path-keyed checksum regression (basename collision — triage P1)
# ===========================================================================


class TestChecksumPathKeyedRegression:
    """Two stores with the SAME basename and DIFFERENT contents must both
    verify via staging-dir-relative path-keyed checksum entries.

    Under the old basename-keyed dict, one shared ``segmentation.seg.nrrd``
    key covered both files, so at most one store's checksum could match.
    """

    def test_two_stores_same_basename_different_contents_both_integrate(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        run_rpc,
        parsed_stdout,
    ):
        stores_dir = stores_dir_factory(('alpha', 'bravo'))
        staging = staging_dir_with_annotations(store_names=['alpha', 'bravo'])
        # Give bravo different (still ontology-valid) bytes than alpha.
        bravo_map = default_seg_label_map()
        bravo_map[3, 3, 3] = 1
        write_seg_nrrd(
            staging / 'bravo' / 'segmentation.seg.nrrd',
            bravo_map,
            [
                {'id': 's0', 'name': 'cochlea', 'label_value': 1, 'color': '1 0 0'},
                {'id': 's1', 'name': 'vestibule', 'label_value': 2, 'color': '0 1 0'},
                {
                    'id': 's2',
                    'name': 'semicircular_canals',
                    'label_value': 3,
                    'color': '0 0 1',
                },
            ],
        )
        alpha_seg = staging / 'alpha' / 'segmentation.seg.nrrd'
        bravo_seg = staging / 'bravo' / 'segmentation.seg.nrrd'
        assert _bare_hex(alpha_seg) != _bare_hex(bravo_seg)

        run_rpc(
            _request(
                'integrate-annotations',
                {
                    'staging_dir': str(staging),
                    'annotator_id': 'alice',
                    'machine_id': 'machine-xyz',
                    'nano_id': 'deadbeef',
                    'expected_ontology': ['inner-ear-structures'],
                    'checksums': [
                        {
                            'path': 'alpha/segmentation.seg.nrrd',
                            'sha256': _bare_hex(alpha_seg),
                        },
                        {
                            'path': 'bravo/segmentation.seg.nrrd',
                            'sha256': _bare_hex(bravo_seg),
                        },
                    ],
                },
            ),
            stores_dir=stores_dir,
        )

        payload = parsed_stdout()
        assert payload['stores']['alpha']['status'] == 'integrated'
        assert payload['stores']['bravo']['status'] == 'integrated'


# ===========================================================================
# Golden request/response pairs — every annotator-reachable method
# ===========================================================================


class TestRpcGoldenPairs:
    """Each method of the pinned surface is callable via stdin JSON."""

    def test_list_stores(self, stores_dir_factory, run_rpc, parsed_stdout):
        root = stores_dir_factory(('alpha',))
        run_rpc(_request('list-stores'), stores_dir=root)

        payload = parsed_stdout()
        assert payload['protocol_version'] == PROTOCOL_VERSION
        assert 'catalog_version' in payload
        assert [s['name'] for s in payload['stores']] == ['alpha']

    def test_list_stores_if_version_short_circuit(
        self, stores_dir_factory, run_rpc, parsed_stdout
    ):
        root = stores_dir_factory(('alpha',))
        run_rpc(_request('list-stores'), stores_dir=root)
        version = parsed_stdout()['catalog_version']

        run_rpc(
            _request('list-stores', {'if_version': version}),
            stores_dir=root,
        )
        payload = parsed_stdout()
        assert payload == {
            'protocol_version': PROTOCOL_VERSION,
            'catalog_version': version,
            'unchanged': True,
        }

    def test_prepare_pull(self, tmp_path, stores_dir_factory, run_rpc, parsed_stdout):
        root = stores_dir_factory(('alpha',))
        staging_root = tmp_path / 'staging_root'
        staging_root.mkdir()

        run_rpc(
            _request('prepare-pull', {'store_name': 'alpha'}),
            stores_dir=root,
            staging_root=staging_root,
        )

        payload = parsed_stdout()
        assert payload['protocol_version'] == PROTOCOL_VERSION
        assert payload['store_name'] == 'alpha'
        assert payload['raw_name'] == 'raw.nrrd'
        staging_dir = Path(payload['staging_dir'])
        assert staging_dir.parent == staging_root
        assert (staging_dir / 'raw.nrrd').is_file()
        # The wire shape parses through the pinned response model.
        parsed = PrepareResponse.from_dict(payload)
        assert parsed.raw_checksum.startswith('sha256:')
        assert parsed.skipped_annotations == []
        assert parsed.memory_warnings == []

    def test_integrate_annotations(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        run_rpc,
        parsed_stdout,
    ):
        stores_dir = stores_dir_factory(('alpha',))
        staging = staging_dir_with_annotations(store_names=['alpha'])
        seg = staging / 'alpha' / 'segmentation.seg.nrrd'

        run_rpc(
            _request(
                'integrate-annotations',
                {
                    'staging_dir': str(staging),
                    'annotator_id': 'alice',
                    'machine_id': 'machine-xyz',
                    'nano_id': 'deadbeef',
                    'expected_ontology': ['inner-ear-structures'],
                    'checksums': [
                        {
                            'path': 'alpha/segmentation.seg.nrrd',
                            'sha256': _bare_hex(seg),
                        },
                    ],
                },
            ),
            stores_dir=stores_dir,
        )

        payload = parsed_stdout()
        assert payload['protocol_version'] == PROTOCOL_VERSION
        parsed = IntegrateResponse.from_dict(payload)
        result = parsed.stores['alpha']
        assert result.status == 'integrated'
        assert len(result.annotations) == 1
        assert result.annotations[0].ontology == 'inner-ear-structures'
        assert result.annotations[0].path.startswith('annotations/alice-deadbeef/')

    def test_prepare_push(self, tmp_path, run_rpc, parsed_stdout):
        """prepare-push mints a prefixed mkdtemp child of the staging root
        and responds ``{protocol_version, staging_dir}`` (rpc-only, 4.1)."""
        staging_root = tmp_path / 'staging_root'
        staging_root.mkdir()

        run_rpc(_request('prepare-push'), staging_root=staging_root)

        payload = parsed_stdout()
        assert set(payload) == {'protocol_version', 'staging_dir'}
        parsed = PreparePushResponse.from_dict(payload)
        assert parsed.protocol_version == PROTOCOL_VERSION
        staging_dir = Path(parsed.staging_dir)
        assert staging_dir.is_absolute()
        assert staging_dir.parent == staging_root
        assert staging_dir.name.startswith(server_cli.STAGING_DIR_PREFIX)
        assert staging_dir.is_dir()
        assert list(staging_dir.iterdir()) == []

    def test_prepare_push_dirs_are_unique(self, tmp_path, run_rpc, parsed_stdout):
        staging_root = tmp_path / 'staging_root'
        staging_root.mkdir()

        minted: set[str] = set()
        for _ in range(2):
            run_rpc(_request('prepare-push'), staging_root=staging_root)
            minted.add(parsed_stdout()['staging_dir'])
        assert len(minted) == 2

    def test_cleanup(self, tmp_path, run_rpc, parsed_stdout):
        staging = tmp_path / f'{server_cli.STAGING_DIR_PREFIX}golden'
        staging.mkdir()
        (staging / 'raw.nrrd').write_bytes(b'x')

        run_rpc(_request('cleanup', {'staging_dir': str(staging)}))

        assert parsed_stdout() == {
            'protocol_version': PROTOCOL_VERSION,
            'status': 'ok',
        }
        assert not staging.exists()

    def test_healthcheck(self, stores_dir_factory, run_rpc, parsed_stdout):
        root = stores_dir_factory(('alpha',))
        run_rpc(_request('healthcheck'), stores_dir=root)

        payload = parsed_stdout()
        assert payload['protocol_version'] == PROTOCOL_VERSION
        assert payload['status'] == 'healthy'
        assert {c['name'] for c in payload['checks']} >= {
            'python_version',
            'packages',
            'stores_dir',
        }


# ===========================================================================
# prepare-push preconditions and surface (launch plan 4.1)
# ===========================================================================


class TestPreparePushPreconditions:
    """Disk-full refusal, key-bound identity, and the rpc-only surface."""

    def test_refuses_when_disk_full(self, tmp_path, run_rpc, parsed_stdout, monkeypatch):
        """A staging filesystem >=90% full → ``disk_full`` envelope, exit 1,
        and no staging dir minted (same precondition as prepare-pull)."""
        import collections

        staging_root = tmp_path / 'staging_root'
        staging_root.mkdir()
        usage = collections.namedtuple('usage', ['total', 'used', 'free'])
        monkeypatch.setattr(
            server_cli.shutil, 'disk_usage', lambda _p: usage(1000, 950, 50)
        )

        with pytest.raises(SystemExit) as excinfo:
            run_rpc(_request('prepare-push'), staging_root=staging_root)
        assert excinfo.value.code == 1

        envelope = parsed_stdout()
        assert envelope['error'] is True
        assert envelope['code'] == 'disk_full'
        assert list(staging_root.iterdir()) == []

    def test_key_bound_identity_mismatch_refused(
        self, tmp_path, run_rpc, parsed_stdout, monkeypatch
    ):
        """A client-sent annotator_id disagreeing with VOXHUB_ANNOTATOR fails
        the request — same transport-enforced binding as prepare-pull."""
        staging_root = tmp_path / 'staging_root'
        staging_root.mkdir()
        monkeypatch.setenv('VOXHUB_ANNOTATOR', 'alice')

        with pytest.raises(SystemExit) as excinfo:
            run_rpc(
                _request('prepare-push', {'annotator_id': 'mallory'}),
                staging_root=staging_root,
            )
        assert excinfo.value.code == 1

        envelope = parsed_stdout()
        assert envelope['code'] == 'identity_mismatch'
        assert list(staging_root.iterdir()) == []

    def test_non_string_annotator_id_is_invalid_params(
        self, tmp_path, run_rpc, parsed_stdout
    ):
        staging_root = tmp_path / 'staging_root'
        staging_root.mkdir()

        with pytest.raises(SystemExit) as excinfo:
            run_rpc(
                _request('prepare-push', {'annotator_id': 42}),
                staging_root=staging_root,
            )
        assert excinfo.value.code == 1
        assert parsed_stdout()['code'] == 'invalid_params'

    def test_no_legacy_subcommand_exists(self):
        """prepare-push is rpc-only: no argparse shim (new surface needs no
        one-release compatibility window)."""
        result = subprocess.run(
            [sys.executable, '-m', 'voxhub_core.server.cli', 'prepare-push'],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 2  # argparse: invalid choice
        assert 'invalid choice' in result.stderr


# ===========================================================================
# Error envelopes
# ===========================================================================


class TestRpcErrorEnvelopes:
    """Malformed stdin, version drift, unknown methods, bad params."""

    def _assert_error(self, parsed_stdout, code: str) -> dict:
        payload = parsed_stdout()
        assert payload['error'] is True
        assert payload['code'] == code
        assert payload['protocol_version'] == PROTOCOL_VERSION
        assert isinstance(payload['message'], str)
        return payload

    @pytest.mark.parametrize(
        'raw_stdin',
        [
            '',
            'not json {',
            (
                f'{{"protocol_version": {PROTOCOL_VERSION}, '
                f'"method": "healthcheck"}} {{"again": 1}}'
            ),
            '"just a string"',
            '[1, 2, 3]',
            '42',
        ],
    )
    def test_malformed_stdin(self, raw_stdin, run_rpc, parsed_stdout):
        with pytest.raises(SystemExit) as excinfo:
            run_rpc(raw_stdin)
        assert excinfo.value.code == 1
        self._assert_error(parsed_stdout, 'malformed_request')

    @pytest.mark.parametrize(
        'declared',
        [None, PROTOCOL_VERSION - 1, PROTOCOL_VERSION + 1, str(PROTOCOL_VERSION), 'two'],
    )
    def test_protocol_version_missing_or_mismatched(
        self, declared, run_rpc, parsed_stdout
    ):
        request: dict = {'method': 'healthcheck', 'params': {}}
        if declared is not None:
            request['protocol_version'] = declared

        with pytest.raises(SystemExit) as excinfo:
            run_rpc(request)
        assert excinfo.value.code == 1
        payload = self._assert_error(parsed_stdout, 'protocol_mismatch')
        # The message names both versions.
        assert str(PROTOCOL_VERSION) in payload['message']
        assert repr(declared) in payload['message']

    @pytest.mark.parametrize(
        'envelope',
        [
            {},  # method missing entirely
            {'method': 42},  # not a string
            {'method': ''},  # empty
        ],
    )
    def test_missing_or_non_string_method(self, envelope, run_rpc, parsed_stdout):
        with pytest.raises(SystemExit) as excinfo:
            run_rpc({'protocol_version': PROTOCOL_VERSION, **envelope})
        assert excinfo.value.code == 1
        self._assert_error(parsed_stdout, 'malformed_request')

    def test_params_not_an_object(self, run_rpc, parsed_stdout):
        with pytest.raises(SystemExit) as excinfo:
            run_rpc(_request('healthcheck') | {'params': [1]})
        assert excinfo.value.code == 1
        self._assert_error(parsed_stdout, 'malformed_request')

    @pytest.mark.parametrize(
        'method',
        [
            'does-not-exist',
            'gc',  # operator command, not annotator-reachable
            'catalog',
            'validate-attributes',
        ],
    )
    def test_unknown_and_operator_methods_rejected(self, method, run_rpc, parsed_stdout):
        with pytest.raises(SystemExit) as excinfo:
            run_rpc(_request(method))
        assert excinfo.value.code == 1
        payload = self._assert_error(parsed_stdout, 'unknown_method')
        assert method in payload['message']

    @pytest.mark.parametrize(
        ('method', 'params', 'expected_fragment'),
        [
            ('prepare-pull', {}, 'store_name'),
            (
                'integrate-annotations',
                {
                    'staging_dir': '/tmp/vxhb-staging-x',
                    'annotator_id': 'alice',
                    'nano_id': 'deadbeef',
                },
                'machine_id',
            ),
            ('cleanup', {}, 'staging_dir'),
        ],
    )
    def test_missing_param_field(
        self, method, params, expected_fragment, run_rpc, parsed_stdout
    ):
        with pytest.raises(SystemExit) as excinfo:
            run_rpc(_request(method, params))
        assert excinfo.value.code == 1
        payload = self._assert_error(parsed_stdout, 'invalid_params')
        assert expected_fragment in payload['message']

    def test_invalid_checksum_entry_rejected_wholesale(self, run_rpc, parsed_stdout):
        """The rpc path refuses malformed checksum entries at
        deserialization — before anything integrates."""
        with pytest.raises(SystemExit) as excinfo:
            run_rpc(
                _request(
                    'integrate-annotations',
                    {
                        'staging_dir': '/tmp/vxhb-staging-x',
                        'annotator_id': 'alice',
                        'machine_id': 'machine-xyz',
                        'nano_id': 'deadbeef',
                        'checksums': [{'path': 'a/b.nrrd', 'sha256': 'NOT-HEX'}],
                    },
                )
            )
        assert excinfo.value.code == 1
        payload = self._assert_error(parsed_stdout, 'invalid_params')
        assert '64 lowercase hex' in payload['message']


# ===========================================================================
# Injection-shaped params round-trip as literals
# ===========================================================================

_INJECTION = '"; echo pwned" with spaces and *'


class TestInjectionParamsAreLiterals:
    """Shell-metacharacter params never tokenize — the design motivator."""

    def test_store_name_round_trips_byte_identically(
        self, stores_dir_factory, run_rpc, parsed_stdout
    ):
        root = stores_dir_factory(('alpha',))
        with pytest.raises(SystemExit) as excinfo:
            run_rpc(
                _request('prepare-pull', {'store_name': _INJECTION}),
                stores_dir=root,
            )
        assert excinfo.value.code == 1

        payload = parsed_stdout()
        assert payload['code'] == 'store_not_found'
        # The exact byte sequence — quotes, spaces, glob — comes back
        # unexpanded and untokenized in the response message.
        assert _INJECTION in payload['message']

    def test_staging_dir_round_trips_byte_identically(self, run_rpc, parsed_stdout):
        with pytest.raises(SystemExit) as excinfo:
            run_rpc(_request('cleanup', {'staging_dir': _INJECTION}))
        assert excinfo.value.code == 1

        payload = parsed_stdout()
        assert payload['code'] == 'invalid_staging_dir'
        assert _INJECTION in payload['message']


# ===========================================================================
# Shim <-> rpc response equivalence
# ===========================================================================


class TestShimRpcEquivalence:
    """The deprecated per-method subcommands produce responses identical
    to their rpc equivalents (they share ``_dispatch``)."""

    def test_list_stores(self, stores_dir_factory, server_argv, run_rpc, parsed_stdout):
        root = stores_dir_factory(('alpha', 'bravo'))

        server_cli._shim_list_stores(server_argv(stores_dir=root))
        via_shim = parsed_stdout()

        run_rpc(_request('list-stores'), stores_dir=root)
        via_rpc = parsed_stdout()

        assert via_shim == via_rpc

    def test_healthcheck(self, stores_dir_factory, server_argv, run_rpc, parsed_stdout):
        root = stores_dir_factory(('alpha',))

        server_cli._shim_healthcheck(server_argv(stores_dir=root))
        via_shim = parsed_stdout()

        run_rpc(_request('healthcheck'), stores_dir=root)
        via_rpc = parsed_stdout()

        assert via_shim == via_rpc

    def test_cleanup(self, tmp_path, server_argv, run_rpc, parsed_stdout):
        dir_a = tmp_path / f'{server_cli.STAGING_DIR_PREFIX}shim'
        dir_b = tmp_path / f'{server_cli.STAGING_DIR_PREFIX}rpc'
        dir_a.mkdir()
        dir_b.mkdir()

        server_cli._shim_cleanup(server_argv(staging_dir=str(dir_a)))
        via_shim = parsed_stdout()

        run_rpc(_request('cleanup', {'staging_dir': str(dir_b)}))
        via_rpc = parsed_stdout()

        assert via_shim == via_rpc
        assert not dir_a.exists()
        assert not dir_b.exists()

    def test_prepare_pull_modulo_staging_dir(
        self, tmp_path, stores_dir_factory, server_argv, run_rpc, parsed_stdout
    ):
        root = stores_dir_factory(('alpha',))
        staging_root = tmp_path / 'staging_root'
        staging_root.mkdir()

        server_cli._shim_prepare_pull(
            server_argv(stores_dir=root, staging_root=staging_root, store='alpha')
        )
        via_shim = parsed_stdout()

        run_rpc(
            _request(
                'prepare-pull',
                {'store_name': 'alpha', 'annotator_id': 'alice'},
            ),
            stores_dir=root,
            staging_root=staging_root,
        )
        via_rpc = parsed_stdout()

        # The minted staging dir is unique per invocation; everything else
        # must be identical.
        assert via_shim.pop('staging_dir') != via_rpc.pop('staging_dir')
        assert via_shim == via_rpc

    def test_integrate_legacy_checksums_match_rpc_path_entries(
        self,
        tmp_path,
        stores_dir_factory,
        server_argv,
        run_rpc,
        parsed_stdout,
    ):
        """A legacy basename token and a path-keyed rpc entry verify the
        same file identically."""
        stores_dir = stores_dir_factory(('alpha',))

        staging_shim = tmp_path / f'{server_cli.STAGING_DIR_PREFIX}shim'
        build_staging_dir_entries(staging_shim / 'alpha')
        # Copy (not re-generate): the NRRD writer embeds a timestamp
        # comment, so only a byte copy guarantees identical digests.
        staging_rpc = tmp_path / f'{server_cli.STAGING_DIR_PREFIX}rpc'
        shutil.copytree(staging_shim, staging_rpc)
        digest = server_cli.compute_sha256(
            staging_shim / 'alpha' / 'segmentation.seg.nrrd'
        )

        server_cli._shim_integrate_annotations(
            server_argv(
                stores_dir=stores_dir,
                staging_dir=str(staging_shim),
                annotator_id='alice',
                machine_id='machine-xyz',
                nano_id='deadbeef',
                checksums=[f'segmentation.seg.nrrd:{digest}'],
                expected_ontology=['inner-ear-structures'],
            )
        )
        via_shim = parsed_stdout()

        run_rpc(
            _request(
                'integrate-annotations',
                {
                    'staging_dir': str(staging_rpc),
                    'annotator_id': 'alice',
                    'machine_id': 'machine-xyz',
                    'nano_id': 'deadbeef',
                    'expected_ontology': ['inner-ear-structures'],
                    'checksums': [
                        {
                            'path': 'alpha/segmentation.seg.nrrd',
                            'sha256': digest.removeprefix('sha256:'),
                        },
                    ],
                },
            ),
            stores_dir=stores_dir,
        )
        via_rpc = parsed_stdout()

        # Annotation instance paths embed a random suffix; compare shape.
        shim_result = via_shim['stores']['alpha']
        rpc_result = via_rpc['stores']['alpha']
        assert shim_result['status'] == rpc_result['status'] == 'integrated'
        assert shim_result['issues'] == rpc_result['issues']
        assert [a['ontology'] for a in shim_result['annotations']] == [
            a['ontology'] for a in rpc_result['annotations']
        ]


# ===========================================================================
# Subprocess-level: real stdin, exit codes, no tracebacks
# ===========================================================================


def _rpc_subprocess(
    payload: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``python -m voxhub_core.server.cli rpc`` with ``payload`` on stdin."""
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, '-m', 'voxhub_core.server.cli', 'rpc'],
        input=payload,
        capture_output=True,
        text=True,
        env=full_env,
        check=False,
    )


def _single_json_line(stdout: str) -> dict:
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f'expected a single JSON line, got: {stdout!r}'
    return json.loads(lines[0])


class TestRpcSubprocess:
    """End-to-end over a real process boundary."""

    def test_golden_list_stores(self, stores_dir_factory, server_config_env):
        root = stores_dir_factory(('alpha',))
        server_config_env(root)

        result = _rpc_subprocess(json.dumps(_request('list-stores')))

        assert result.returncode == 0
        payload = _single_json_line(result.stdout)
        assert payload['protocol_version'] == PROTOCOL_VERSION
        assert [s['name'] for s in payload['stores']] == ['alpha']
        assert 'Traceback' not in result.stderr

    @pytest.mark.parametrize(
        ('payload', 'code'),
        [
            ('this is not json', 'malformed_request'),
            ('{"method": "healthcheck", "params": {}}', 'protocol_mismatch'),
            (
                json.dumps(_request('healthcheck', protocol_version=1)),
                'protocol_mismatch',
            ),
            (json.dumps(_request('gc')), 'unknown_method'),
            (json.dumps(_request('prepare-pull', {})), 'invalid_params'),
        ],
    )
    def test_error_exit_1_no_traceback(
        self, payload, code, stores_dir_factory, server_config_env
    ):
        root = stores_dir_factory(('alpha',))
        server_config_env(root)

        result = _rpc_subprocess(payload)

        assert result.returncode == 1
        envelope = _single_json_line(result.stdout)
        assert envelope['error'] is True
        assert envelope['code'] == code
        assert envelope['protocol_version'] == PROTOCOL_VERSION
        assert 'Traceback' not in result.stderr
        assert 'Traceback' not in result.stdout

    def test_version_mismatch_names_both_versions(
        self, stores_dir_factory, server_config_env
    ):
        root = stores_dir_factory(('alpha',))
        server_config_env(root)

        result = _rpc_subprocess(json.dumps(_request('healthcheck', protocol_version=1)))

        assert result.returncode == 1
        envelope = _single_json_line(result.stdout)
        assert envelope['code'] == 'protocol_mismatch'
        assert str(PROTOCOL_VERSION) in envelope['message']
        assert '1' in envelope['message']

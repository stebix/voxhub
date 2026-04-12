"""Subprocess-level smoke tests for the voxhub-server CLI.

These tests invoke the actual ``voxhub-server`` entrypoint via subprocess
and assert on the parsed JSON stdout.  They validate argparse wiring and
the real end-to-end binary contract; bulk logic coverage lives in
``test_server_cli.py`` (function-level tests).

Plan: docs/testing/server-cli.md §5
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from voxhub_schema import PROTOCOL_VERSION

pytestmark = pytest.mark.slow


def _parse_json_stdout(proc: subprocess.CompletedProcess[str]) -> dict:
    """Parse the last non-empty stdout line as JSON."""
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert lines, (
        f'expected JSON on stdout, got nothing.\n'
        f'stdout={proc.stdout!r} stderr={proc.stderr!r}'
    )
    return json.loads(lines[-1])


class TestEntryPoint:
    """Validate the voxhub-server entry point itself."""

    def test_voxhub_server_entry_point_installed(self):
        """The voxhub-server console script is installed OR the module is
        runnable via ``python -m``.  The ``-m`` path is authoritative for
        this test suite; the ``which`` probe is a bonus check."""
        entry = shutil.which('voxhub-server')
        # If the entry script exists, it must at least print help on --help.
        if entry is not None:
            result = subprocess.run([entry, '--help'], capture_output=True, text=True)
            assert result.returncode == 0
            assert 'voxhub' in result.stdout.lower()

        # The module-based invocation must always work.
        result = subprocess.run(
            [sys.executable, '-m', 'voxhub_core.server.cli', '--help'],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert 'usage' in result.stdout.lower()

    def test_no_command_prints_help_exit_zero(self, subprocess_server):
        result = subprocess_server()
        assert result.returncode == 0
        assert 'usage' in result.stdout.lower()

    def test_invalid_subcommand_argparse_error(self, subprocess_server):
        result = subprocess_server('nonsense-cmd')
        assert result.returncode == 2
        assert 'invalid choice' in result.stderr.lower()


class TestCommandSmoke:
    """One happy-path test per subcommand via real subprocess."""

    def test_list_stores(self, zarr_root_factory, subprocess_server):
        root = zarr_root_factory(('alpha',))
        result = subprocess_server('list-stores', str(root))

        assert result.returncode == 0, result.stderr
        payload = _parse_json_stdout(result)
        assert payload['protocol_version'] == PROTOCOL_VERSION
        assert isinstance(payload['stores'], list)
        assert len(payload['stores']) == 1
        assert payload['stores'][0]['name'] == 'alpha'

    def test_prepare_pull(self, zarr_root_factory, subprocess_server, tmp_path):
        root = zarr_root_factory(('alpha',))
        wip = tmp_path / 'subproc_wip'
        result = subprocess_server(
            'prepare-pull',
            str(root),
            '--stores',
            'alpha',
            '--wip-dir',
            str(wip),
        )

        assert result.returncode == 0, result.stderr
        payload = _parse_json_stdout(result)
        assert Path(payload['wip_dir']) == wip
        assert 'alpha' in payload['stores']

    def test_integrate_annotations_full_roundtrip(
        self,
        zarr_root_factory,
        wip_dir_with_manifest,
        subprocess_server,
    ):
        """End-to-end: stage → build annotation → integrate → verify.

        This is the critical server-path smoke test that exercises real
        argparse, real I/O, and the installed entrypoint.
        """
        zarr_root = zarr_root_factory(('alpha',))
        wip = wip_dir_with_manifest(store_names=['alpha'])

        result = subprocess_server(
            'integrate-annotations',
            str(zarr_root),
            str(wip),
            '--annotator-id',
            'alice',
            '--machine-id',
            'machine-abc',
            '--nano-id',
            'sub12345',
        )

        assert result.returncode == 0, result.stderr
        payload = _parse_json_stdout(result)
        assert payload['stores']['alpha']['status'] == 'integrated'

        # Verify the zarr store actually gained an annotation on disk.
        ann_root = zarr_root / 'alpha.zarr' / 'annotations'
        assert ann_root.is_dir()
        annotator_dirs = [p for p in ann_root.iterdir() if p.is_dir()]
        assert len(annotator_dirs) == 1
        assert annotator_dirs[0].name == 'alice-sub12345'

    def test_cleanup(self, tmp_path, subprocess_server):
        wip = tmp_path / 'dt-pull-smoke'
        wip.mkdir()
        (wip / 'payload').write_text('x')

        result = subprocess_server('cleanup', str(wip))

        assert result.returncode == 0, result.stderr
        assert _parse_json_stdout(result)['status'] == 'ok'
        assert not wip.exists()

    def test_gc(self, tmp_path, subprocess_server):
        """``gc`` scans ``tempfile.gettempdir()``.  Redirect via TMPDIR so
        the subprocess doesn't touch the real /tmp."""
        fake_tmp = tmp_path / 'fake_tmp'
        fake_tmp.mkdir()
        # Seed an old dt-* dir that must be reaped.
        old = fake_tmp / 'dt-pull-old'
        old.mkdir()
        old_ts = old.stat().st_mtime - 48 * 3600
        os.utime(old, (old_ts, old_ts))

        result = subprocess_server(
            'gc',
            '--ttl-hours',
            '24',
            env={'TMPDIR': str(fake_tmp)},
        )

        assert result.returncode == 0, result.stderr
        payload = _parse_json_stdout(result)
        assert payload['count'] == 1
        assert not old.exists()

    def test_validate_attributes(self, zarr_root_factory, subprocess_server):
        root = zarr_root_factory(('alpha',))
        result = subprocess_server('validate-attributes', str(root))

        assert result.returncode == 0, result.stderr
        payload = _parse_json_stdout(result)
        assert payload['results']['alpha']['status'] == 'missing'

    def test_healthcheck_exit_code_on_degraded(self, tmp_path, subprocess_server):
        missing = tmp_path / 'nonexistent'
        result = subprocess_server('healthcheck', str(missing))

        assert result.returncode == 1
        payload = _parse_json_stdout(result)
        assert payload['status'] == 'degraded'


class TestProtocolContract:
    """Invariants that every subprocess response must satisfy."""

    def test_every_command_emits_protocol_version(
        self, zarr_root_factory, subprocess_server, tmp_path
    ):
        root = zarr_root_factory(('alpha',))

        # list-stores
        r1 = subprocess_server('list-stores', str(root))
        assert _parse_json_stdout(r1)['protocol_version'] == PROTOCOL_VERSION

        # validate-attributes
        r2 = subprocess_server('validate-attributes', str(root))
        assert _parse_json_stdout(r2)['protocol_version'] == PROTOCOL_VERSION

        # healthcheck (healthy)
        r3 = subprocess_server('healthcheck', str(root))
        assert r3.returncode == 0
        assert _parse_json_stdout(r3)['protocol_version'] == PROTOCOL_VERSION

        # cleanup (noop)
        r4 = subprocess_server('cleanup', str(tmp_path / 'nope'))
        assert _parse_json_stdout(r4)['protocol_version'] == PROTOCOL_VERSION

        # gc (empty)
        fake_tmp = tmp_path / 'fake_tmp_proto'
        fake_tmp.mkdir()
        r5 = subprocess_server('gc', env={'TMPDIR': str(fake_tmp)})
        assert _parse_json_stdout(r5)['protocol_version'] == PROTOCOL_VERSION

    def test_error_envelope_structure(self, tmp_path, subprocess_server):
        """A deliberately-failing invocation produces a structured
        ServerError envelope — never a raw traceback."""
        # prepare-pull against a non-existent zarr root triggers stage()
        # FileNotFoundError → prepare_pull_failed envelope.
        missing_root = tmp_path / 'no-such-root'
        result = subprocess_server(
            'prepare-pull',
            str(missing_root),
            '--wip-dir',
            str(tmp_path / 'wip'),
        )

        assert result.returncode == 1
        payload = _parse_json_stdout(result)
        assert payload['protocol_version'] == PROTOCOL_VERSION
        assert payload['error'] is True
        assert payload['code'] == 'prepare_pull_failed'
        assert isinstance(payload['message'], str)
        # No traceback leaked onto stdout.
        assert 'Traceback' not in result.stdout

    def test_stdout_is_single_json_object(self, zarr_root_factory, subprocess_server):
        root = zarr_root_factory(('alpha',))
        result = subprocess_server('list-stores', str(root))

        lines = [line for line in result.stdout.splitlines() if line.strip()]
        assert len(lines) == 1, f'expected single JSON line, got: {result.stdout!r}'
        parsed = json.loads(lines[0])
        assert isinstance(parsed, dict)

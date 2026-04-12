"""Subprocess-level smoke tests for the voxhub-server CLI.

These tests invoke the actual ``voxhub-server`` entrypoint via subprocess and
assert on the parsed JSON stdout.  They validate argparse wiring and the
real end-to-end binary contract; bulk logic coverage lives in
``test_server_cli.py`` (function-level tests).

Plan: docs/testing/server-cli.md §5

Each test is currently skipped.  Remove the skip marker as tests are
implemented in a downstream worktree.

Fixtures expected:
    - zarr_root_factory
    - wip_dir_with_manifest
    - subprocess_server — session-scoped callable that runs voxhub-server
      and returns parsed JSON stdout.  Prefer invoking
      ``[sys.executable, '-m', 'voxhub_core.server.cli', ...]`` for
      hermeticity; one dedicated test exercises ``shutil.which('voxhub-server')``
      to verify the entry point is installed.
"""

import pytest

pytestmark = [
    pytest.mark.skip(reason='stub — see docs/testing/server-cli.md §5'),
    pytest.mark.slow,
]


class TestEntryPoint:
    """Validate the voxhub-server entry point itself."""

    def test_voxhub_server_entry_point_installed(self):
        """shutil.which('voxhub-server') returns a path OR
        `python -m voxhub_core.server.cli --help` exits 0."""

    def test_no_command_prints_help_exit_zero(self, subprocess_server):
        """voxhub-server with no args → exit 0, help on stdout."""
        del subprocess_server

    def test_invalid_subcommand_argparse_error(self, subprocess_server):
        """voxhub-server nonsense-cmd → exit 2 (argparse)."""
        del subprocess_server


class TestCommandSmoke:
    """One happy-path test per subcommand via real subprocess."""

    def test_list_stores(self, zarr_root_factory, subprocess_server):
        """voxhub-server list-stores <root> → valid JSON, protocol_version=1,
        stores is a list."""
        del zarr_root_factory, subprocess_server

    def test_prepare_pull(self, zarr_root_factory, subprocess_server, tmp_path):
        """voxhub-server prepare-pull <root> --wip-dir <tmp> → JSON with
        wip_dir and stores dict. Cleans up the returned wip_dir."""
        del zarr_root_factory, subprocess_server, tmp_path

    def test_integrate_annotations_full_roundtrip(
        self, zarr_root_factory, wip_dir_with_manifest, subprocess_server
    ):
        """Full round trip: stage → build annotation → integrate → verify
        zarr state. One end-to-end test exercising real argparse, real I/O,
        and real entrypoint. This is the critical server-path smoke test."""
        del zarr_root_factory, wip_dir_with_manifest, subprocess_server

    def test_cleanup(self, tmp_path, subprocess_server):
        """voxhub-server cleanup <wip_dir> → exit 0, status=='ok'."""
        del tmp_path, subprocess_server

    def test_gc(self, tmp_path, subprocess_server, monkeypatch):
        """voxhub-server gc --ttl-hours 24 → exit 0, response has removed/count.
        Must monkey-patch TMPDIR so the test doesn't scan the real /tmp."""
        del tmp_path, subprocess_server, monkeypatch

    def test_validate_attributes(self, zarr_root_factory, subprocess_server):
        """voxhub-server validate-attributes <root> → exit 0, results dict."""
        del zarr_root_factory, subprocess_server

    def test_healthcheck_exit_code_on_degraded(
        self, tmp_path, subprocess_server
    ):
        """voxhub-server healthcheck <nonexistent> → exit 1, stdout still
        valid JSON with status=='degraded'."""
        del tmp_path, subprocess_server


class TestProtocolContract:
    """Invariants that every subprocess response must satisfy."""

    def test_every_command_emits_protocol_version(
        self, zarr_root_factory, subprocess_server
    ):
        """For each subcommand that returns successfully, response JSON has
        protocol_version == PROTOCOL_VERSION."""
        del zarr_root_factory, subprocess_server

    def test_error_envelope_structure(self, tmp_path, subprocess_server):
        """A deliberately-failing call produces a ServerError envelope:
        {protocol_version, error=True, code, message} — never a traceback."""
        del tmp_path, subprocess_server

    def test_stdout_is_single_json_object(
        self, zarr_root_factory, subprocess_server
    ):
        """stdout parses as a single JSON object followed by at most one
        trailing newline — no log interleaving, no multi-line JSON."""
        del zarr_root_factory, subprocess_server

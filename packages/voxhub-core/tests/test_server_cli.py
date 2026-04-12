"""Function-level tests for voxhub_core.server.cli handlers.

Each test calls ``_run_X(args)`` directly with a hand-built
``argparse.Namespace``, captures stdout via ``capsys``, and asserts on the
parsed JSON envelope.  Subprocess-level smoke tests live in
``test_server_cli_subprocess.py``.

Plan: docs/testing/server-cli.md

This file is a set of ``pytest.mark.skip`` stubs.  Remove the skip marker
as each test is fleshed out in a downstream worktree.  Each stub body has
a ``del`` statement that references its fixture parameters so the type
checker doesn't flag them as unused — replace the ``del`` with the real
test body when implementing.

Fixtures expected (to be added to conftest.py):
    - zarr_root_factory
    - wip_dir_with_manifest
    - server_argv
    - parsed_stdout
"""

import pytest

pytestmark = pytest.mark.skip(reason='stub — see docs/testing/server-cli.md')


# ===========================================================================
# _run_list_stores
# ===========================================================================


class TestListStores:
    """Covers voxhub_core.server.cli._run_list_stores."""

    def test_lists_single_empty_store(self, zarr_root_factory, server_argv, capsys):
        """One store, no annotations → single entry with populated geometry."""
        del zarr_root_factory, server_argv, capsys

    def test_lists_multiple_stores_sorted(
        self, zarr_root_factory, server_argv, capsys
    ):
        """Three stores → all appear, order matches discover_zarr_stores."""
        del zarr_root_factory, server_argv, capsys

    def test_lists_store_with_annotations(
        self, zarr_root_factory, server_argv, capsys
    ):
        """Store with a pre-populated annotation → annotations list has one entry
        with correct path, ontology, annotator_id, integrated_at."""
        del zarr_root_factory, server_argv, capsys

    def test_includes_dataset_attributes_when_present(
        self, zarr_root_factory, server_argv, capsys
    ):
        """Root dataset_attributes → populated in response; absent → None."""
        del zarr_root_factory, server_argv, capsys

    def test_protocol_version_present(
        self, zarr_root_factory, server_argv, capsys
    ):
        """Response must always carry protocol_version == PROTOCOL_VERSION."""
        del zarr_root_factory, server_argv, capsys

    def test_store_with_probe_error(
        self, zarr_root_factory, server_argv, capsys
    ):
        """Corrupted store → entry has error populated, other stores still OK."""
        del zarr_root_factory, server_argv, capsys

    def test_store_missing_spatial_metadata(
        self, zarr_root_factory, server_argv, capsys
    ):
        """Missing ImagePosition*/PixelSpacing → error='Missing spatial metadata',
        annotations still discovered."""
        del zarr_root_factory, server_argv, capsys

    def test_nonexistent_zarr_root(self, tmp_path, server_argv, capsys):
        """Nonexistent root → empty stores list, not a crash."""
        del tmp_path, server_argv, capsys

    def test_logs_duration_on_completion(
        self, zarr_root_factory, server_argv, capsys
    ):
        """stderr has structured log record list_stores_completed with duration_s."""
        del zarr_root_factory, server_argv, capsys


# ===========================================================================
# _run_prepare_pull
# ===========================================================================


class TestPreparePull:
    """Covers voxhub_core.server.cli._run_prepare_pull."""

    def test_stages_single_store_to_tempdir(
        self, zarr_root_factory, server_argv, capsys
    ):
        """One store → response has wip_dir and PreparedStore dict with all
        spatial metadata fields and raw_checksum."""
        del zarr_root_factory, server_argv, capsys

    def test_uses_explicit_wip_dir_when_provided(
        self, zarr_root_factory, tmp_path, server_argv, capsys
    ):
        """args.wip_dir=path → response points at that directory, not dt-pull-*."""
        del zarr_root_factory, tmp_path, server_argv, capsys

    def test_filters_stores_by_name(
        self, zarr_root_factory, server_argv, capsys
    ):
        """--stores a c → only those two appear in response."""
        del zarr_root_factory, server_argv, capsys

    def test_records_expected_ontologies(
        self, zarr_root_factory, server_argv, capsys
    ):
        """--ontologies x y z → stores[name].expected_ontologies == [x, y, z]."""
        del zarr_root_factory, server_argv, capsys

    def test_copies_existing_annotations_when_requested(
        self, zarr_root_factory, server_argv, capsys
    ):
        """--include-existing-annotations <path> → annotation copied into WIP."""
        del zarr_root_factory, server_argv, capsys

    def test_compression_flag_propagates_to_stage(
        self, zarr_root_factory, server_argv, capsys, monkeypatch
    ):
        """--compress flag reaches stage() (spy on voxhub_core.staging.stage)."""
        del zarr_root_factory, server_argv, capsys, monkeypatch

    def test_stage_failure_writes_error_envelope_and_exits(
        self, zarr_root_factory, server_argv, capsys, monkeypatch
    ):
        """Patch stage to raise → ServerError with code='prepare_pull_failed',
        SystemExit(1)."""
        del zarr_root_factory, server_argv, capsys, monkeypatch

    def test_nonexistent_store_name(
        self, zarr_root_factory, server_argv, capsys
    ):
        """--stores does-not-exist → document current behavior (empty dict?)."""
        del zarr_root_factory, server_argv, capsys

    def test_protocol_version_present(
        self, zarr_root_factory, server_argv, capsys
    ):
        """Response envelope always has protocol_version field."""
        del zarr_root_factory, server_argv, capsys

    def test_wip_dir_is_string_not_path_object(
        self, zarr_root_factory, server_argv, capsys
    ):
        """JSON serialization: wip_dir serializes as str, not repr(Path)."""
        del zarr_root_factory, server_argv, capsys


# ===========================================================================
# _run_integrate_annotations (happy paths)
# ===========================================================================


class TestIntegrateAnnotationsHappy:
    """Covers voxhub_core.server.cli._run_integrate_annotations — success paths."""

    def test_integrates_segmentation_writes_to_annotator_scoped_path(
        self, zarr_root_factory, wip_dir_with_manifest, server_argv, capsys
    ):
        """Valid seg.nrrd → zarr array at
        annotations/<annotator>-<nano>/<ontology>-<date>-<rand>/data;
        response stores[name].status == 'integrated'."""
        del zarr_root_factory, wip_dir_with_manifest, server_argv, capsys

    def test_integrates_landmarks_writes_to_annotator_scoped_path(
        self, zarr_root_factory, wip_dir_with_manifest, server_argv, capsys
    ):
        """Valid landmarks.mrk.json → analogous zarr path, correct attrs."""
        del zarr_root_factory, wip_dir_with_manifest, server_argv, capsys

    def test_integrates_both_seg_and_landmarks_in_single_call(
        self, zarr_root_factory, wip_dir_with_manifest, server_argv, capsys
    ):
        """WIP has both → both written, both in response.annotations list."""
        del zarr_root_factory, wip_dir_with_manifest, server_argv, capsys

    def test_provenance_recorded_on_success(
        self, zarr_root_factory, wip_dir_with_manifest, server_argv, capsys
    ):
        """.meta/provenance.jsonl has new entry with matching annotator_id,
        machine_id, source checksums. Zarr array attrs include integrated_at,
        annotator_id, etc."""
        del zarr_root_factory, wip_dir_with_manifest, server_argv, capsys

    def test_uses_ontology_from_manifest_not_cli(
        self, zarr_root_factory, wip_dir_with_manifest, server_argv, capsys
    ):
        """Manifest expected_ontologies=['inner-ear-structures'] → that ontology
        loaded, recorded in written array attrs."""
        del zarr_root_factory, wip_dir_with_manifest, server_argv, capsys

    def test_checksum_matches_accepted(
        self, zarr_root_factory, wip_dir_with_manifest, server_argv, capsys
    ):
        """--checksums file:sha256:<correct> → integration proceeds."""
        del zarr_root_factory, wip_dir_with_manifest, server_argv, capsys


# ===========================================================================
# _run_integrate_annotations (error paths)
# ===========================================================================


class TestIntegrateAnnotationsErrors:
    """Covers error/rejection paths in _run_integrate_annotations."""

    def test_missing_manifest_writes_error_envelope_and_exits(
        self, zarr_root_factory, tmp_path, server_argv, capsys
    ):
        """WIP has no .voxhub_manifest.json → ServerError code='manifest_missing',
        SystemExit(1)."""
        del zarr_root_factory, tmp_path, server_argv, capsys

    def test_checksum_mismatch_writes_error_envelope_and_exits(
        self, zarr_root_factory, wip_dir_with_manifest, server_argv, capsys
    ):
        """Wrong checksum → ServerError code='checksum_mismatch', SystemExit(1)."""
        del zarr_root_factory, wip_dir_with_manifest, server_argv, capsys

    def test_unknown_ontology_produces_warning_but_continues(
        self, zarr_root_factory, wip_dir_with_manifest, server_argv, capsys
    ):
        """Manifest references nonexistent ontology → warning issue added,
        integration still proceeds with unconstrained fallback."""
        del zarr_root_factory, wip_dir_with_manifest, server_argv, capsys

    def test_segmentation_validation_error_without_force_blocks_write(
        self, zarr_root_factory, wip_dir_with_manifest, server_argv, capsys
    ):
        """Shape mismatch in seg.nrrd → no annotation written, issues populated."""
        del zarr_root_factory, wip_dir_with_manifest, server_argv, capsys

    def test_force_allows_integration_despite_errors(
        self, zarr_root_factory, wip_dir_with_manifest, server_argv, capsys
    ):
        """--force + shape mismatch → annotation IS written."""
        del zarr_root_factory, wip_dir_with_manifest, server_argv, capsys

    def test_parse_error_recorded_in_issues(
        self, zarr_root_factory, wip_dir_with_manifest, server_argv, capsys
    ):
        """Corrupted .seg.nrrd → caught as IssueRecord with severity='error',
        no annotation written, other stores unaffected."""
        del zarr_root_factory, wip_dir_with_manifest, server_argv, capsys

    def test_lock_timeout_surfaces_cleanly(
        self, zarr_root_factory, wip_dir_with_manifest, server_argv, capsys
    ):
        """Simulate held lock in a separate process → integrate blocks until
        timeout, then surfaces an error envelope (not a traceback)."""
        del zarr_root_factory, wip_dir_with_manifest, server_argv, capsys


# ===========================================================================
# _run_integrate_annotations (multi-store)
# ===========================================================================


class TestIntegrateAnnotationsMultiStore:
    """Covers multi-store behavior in _run_integrate_annotations."""

    def test_partial_failure_per_store_isolated(
        self, zarr_root_factory, wip_dir_with_manifest, server_argv, capsys
    ):
        """Store A valid, store B corrupted → A integrated, B failed, independent."""
        del zarr_root_factory, wip_dir_with_manifest, server_argv, capsys

    def test_iteration_order_deterministic(
        self, zarr_root_factory, wip_dir_with_manifest, server_argv, capsys
    ):
        """WIP dirs [c, a, b] → response stores dict ordering matches sorted()."""
        del zarr_root_factory, wip_dir_with_manifest, server_argv, capsys

    def test_skips_hidden_directories(
        self, zarr_root_factory, wip_dir_with_manifest, server_argv, capsys
    ):
        """WIP has .hidden/ → silently skipped."""
        del zarr_root_factory, wip_dir_with_manifest, server_argv, capsys

    def test_skips_directories_without_matching_zarr_store(
        self, zarr_root_factory, wip_dir_with_manifest, server_argv, capsys
    ):
        """WIP has orphan/ but no orphan.zarr → silently skipped."""
        del zarr_root_factory, wip_dir_with_manifest, server_argv, capsys


# ===========================================================================
# _run_integrate_annotations (ontology resolution)
# ===========================================================================


class TestIntegrateAnnotationsOntology:
    """Covers _resolve_ontologies + ontology selection logic."""

    def test_segmentation_ontology_resolution_filters_by_type(
        self, zarr_root_factory, wip_dir_with_manifest, server_argv, capsys
    ):
        """Manifest has both seg and landmarks ontology → each annotation type
        gets the right one."""
        del zarr_root_factory, wip_dir_with_manifest, server_argv, capsys

    def test_first_matching_ontology_used(
        self, zarr_root_factory, wip_dir_with_manifest, server_argv, capsys
    ):
        """Manifest has two seg ontologies → first is used (pin current behavior
        from server/cli.py:446)."""
        del zarr_root_factory, wip_dir_with_manifest, server_argv, capsys

    def test_no_matching_ontology_uses_unconstrained_fallback(
        self, zarr_root_factory, wip_dir_with_manifest, server_argv, capsys
    ):
        """Manifest has only landmarks ontology, WIP has only seg → written with
        ontology='unconstrained', ontology_version=1."""
        del zarr_root_factory, wip_dir_with_manifest, server_argv, capsys


# ===========================================================================
# _run_cleanup
# ===========================================================================


class TestCleanup:
    """Covers voxhub_core.server.cli._run_cleanup."""

    def test_removes_existing_wip_dir(
        self, tmp_path, server_argv, capsys
    ):
        """Existing dir → removed, response status=='ok'."""
        del tmp_path, server_argv, capsys

    def test_noop_when_wip_dir_missing(
        self, tmp_path, server_argv, capsys
    ):
        """Nonexistent dir → response status=='ok', warning logged."""
        del tmp_path, server_argv, capsys

    @pytest.mark.xfail(reason='current implementation has no path safety check')
    def test_refuses_to_remove_non_wip_path(
        self, tmp_path, server_argv, capsys
    ):
        """Security concern: _run_cleanup removes any path given. This test
        documents the concern and becomes actionable if a safety check is
        added (e.g., only remove dt-* prefixed paths or paths under tmpdir)."""
        del tmp_path, server_argv, capsys


# ===========================================================================
# _run_gc
# ===========================================================================


class TestGc:
    """Covers voxhub_core.server.cli._run_gc.

    Isolation note: _run_gc scans the real /tmp. Tests must monkey-patch
    tempfile.gettempdir() to point at a tmp_path owned by the test, otherwise
    they will clobber unrelated dt-* dirs on the developer's machine.
    """

    def test_removes_dirs_older_than_ttl(
        self, tmp_path, server_argv, capsys, monkeypatch
    ):
        """/tmp/dt-pull-xxx with mtime 48h ago, --ttl-hours 24 → removed."""
        del tmp_path, server_argv, capsys, monkeypatch

    def test_keeps_dirs_newer_than_ttl(
        self, tmp_path, server_argv, capsys, monkeypatch
    ):
        """Recent dir → kept."""
        del tmp_path, server_argv, capsys, monkeypatch

    def test_ignores_non_dt_prefix(
        self, tmp_path, server_argv, capsys, monkeypatch
    ):
        """/tmp/foo-bar older than cutoff → ignored."""
        del tmp_path, server_argv, capsys, monkeypatch

    def test_ignores_files_only_dirs(
        self, tmp_path, server_argv, capsys, monkeypatch
    ):
        """/tmp/dt-file (a file) → ignored."""
        del tmp_path, server_argv, capsys, monkeypatch

    def test_count_matches_removed_length(
        self, tmp_path, server_argv, capsys, monkeypatch
    ):
        """response['count'] == len(response['removed'])."""
        del tmp_path, server_argv, capsys, monkeypatch

    def test_default_ttl_24_hours(
        self, tmp_path, server_argv, capsys, monkeypatch
    ):
        """No --ttl-hours → argparse default is 24.0."""
        del tmp_path, server_argv, capsys, monkeypatch


# ===========================================================================
# _run_validate_attributes
# ===========================================================================


class TestValidateAttributes:
    """Covers voxhub_core.server.cli._run_validate_attributes."""

    def test_store_without_dataset_attributes_reports_missing(
        self, zarr_root_factory, server_argv, capsys
    ):
        """No dataset_attributes → results[store]['status']=='missing'."""
        del zarr_root_factory, server_argv, capsys

    def test_store_with_valid_attributes_reports_ok(
        self, zarr_root_factory, server_argv, capsys
    ):
        """Consistent attrs → status=='ok'."""
        del zarr_root_factory, server_argv, capsys

    def test_store_with_mismatched_voxel_size_reports_warning(
        self, zarr_root_factory, server_argv, capsys
    ):
        """Declared voxel_size differs from computed spacing →
        status=='warning', issues list populated with field/declared/actual/message."""
        del zarr_root_factory, server_argv, capsys

    def test_filters_stores_by_name(
        self, zarr_root_factory, server_argv, capsys
    ):
        """--stores a → only a evaluated."""
        del zarr_root_factory, server_argv, capsys

    def test_protocol_version_present(
        self, zarr_root_factory, server_argv, capsys
    ):
        """Response envelope always has protocol_version field."""
        del zarr_root_factory, server_argv, capsys


# ===========================================================================
# _run_healthcheck
# ===========================================================================


class TestHealthcheck:
    """Covers voxhub_core.server.cli._run_healthcheck and _check_* helpers."""

    def test_healthy_all_green(
        self, zarr_root_factory, server_argv, capsys
    ):
        """Well-formed root, all checks pass → status=='healthy', exit 0."""
        del zarr_root_factory, server_argv, capsys

    def test_degraded_when_zarr_root_unwritable(
        self, tmp_path, server_argv, capsys
    ):
        """Read-only root → status=='degraded', zarr_root check fails,
        SystemExit(1)."""
        del tmp_path, server_argv, capsys

    def test_degraded_when_store_corrupted(
        self, zarr_root_factory, server_argv, capsys
    ):
        """One store has probe error → stores check fails."""
        del zarr_root_factory, server_argv, capsys

    def test_provenance_check_ok_when_file_missing(
        self, zarr_root_factory, server_argv, capsys
    ):
        """No .meta/provenance.jsonl → check passes (detail='no provenance file yet')."""
        del zarr_root_factory, server_argv, capsys

    def test_provenance_check_fails_on_malformed_jsonl(
        self, zarr_root_factory, server_argv, capsys
    ):
        """Write JSONL with one bad line → check fails."""
        del zarr_root_factory, server_argv, capsys

    def test_store_and_provenance_checks_skipped_when_zarr_root_fails(
        self, tmp_path, server_argv, capsys
    ):
        """If zarr_root check fails, later checks aren't run (would crash)."""
        del tmp_path, server_argv, capsys

    def test_python_version_check(self):
        """_check_python_version() reports current version, status='ok' on 3.12+."""

    def test_rsync_check_when_rsync_present(self, monkeypatch):
        """shutil.which('rsync') returns path → status='ok'."""
        del monkeypatch

    def test_rsync_check_when_absent(self, monkeypatch):
        """shutil.which('rsync') returns None → status='fail'."""
        del monkeypatch

    def test_packages_check_all_importable(self):
        """All three voxhub_* packages importable → status='ok'."""


# ===========================================================================
# main() entry point
# ===========================================================================


class TestMainEntry:
    """Covers voxhub_core.server.cli.main — argparse wiring and error handling."""

    def test_no_command_prints_help_and_exits_zero(
        self, capsys, monkeypatch
    ):
        """args.command is None → print_help, sys.exit(0)."""
        del capsys, monkeypatch

    def test_unknown_command_argparse_error(
        self, capsys, monkeypatch
    ):
        """Unknown subcommand → argparse exits 2."""
        del capsys, monkeypatch

    def test_unhandled_exception_returns_server_error_envelope(
        self, capsys, monkeypatch
    ):
        """Patch a handler to raise RuntimeError('boom') → stdout has
        ServerError code='internal_error', message='boom', exit 1.
        Covers server/cli.py:945-950."""
        del capsys, monkeypatch

    def test_settings_loaded_on_entry(
        self, capsys, monkeypatch
    ):
        """Spy on load_settings — called before command dispatch."""
        del capsys, monkeypatch

# Test Plan: `voxhub_core.server.cli`

**Target module:** `packages/voxhub-core/src/voxhub_core/server/cli.py` (951 LOC)
**Stub files:** `tests/test_server_cli.py`, `tests/test_server_cli_subprocess.py`
**Priority:** P1 — highest-risk untested surface.

## 1. Scope

This plan covers all seven subcommands exposed by `voxhub-server`:

| Command                 | Handler                         | Lines (approx) |
|-------------------------|---------------------------------|----------------|
| `list-stores`           | `_run_list_stores`              | 97–185         |
| `prepare-pull`          | `_run_prepare_pull`             | 191–274        |
| `integrate-annotations` | `_run_integrate_annotations`    | 326–606        |
| `cleanup`               | `_run_cleanup`                  | 612–629        |
| `gc`                    | `_run_gc`                       | 635–667        |
| `validate-attributes`   | `_run_validate_attributes`      | 673–728        |
| `healthcheck`           | `_run_healthcheck` + `_check_*` | 734–875        |

Plus the entry point `main()` (argparse wiring, unhandled exception handler).

## 2. Layered approach

Per the clarification discussion, tests sit at two layers:

### Layer A — function-level (`test_server_cli.py`)

Calls `_run_X(args)` directly with a hand-built `argparse.Namespace`. Captures
`sys.stdout` via `capsys` and parses the single-line JSON envelope. This is the
**bulk of the coverage** — fast, deterministic, exercises all logic except
argparse parsing and `main()`.

### Layer B — subprocess-level (`test_server_cli_subprocess.py`)

Invokes `voxhub-server <cmd>` via `subprocess.run`. Thin smoke suite that
verifies:

- The `voxhub-server` entry point is installed and importable.
- argparse wiring actually parses the documented flags.
- `main()` exits 0 on success, non-zero on failure.
- stdout is valid JSON; `protocol_version` is always present.
- Unhandled exceptions produce a `ServerError` envelope, not a traceback.

One smoke test per command is enough at this layer.

## 3. Fixtures required

Beyond the existing `create_zarr_store` helper, these tests need:

- `zarr_root_factory(store_count=1, with_annotations=False)` — builds a
  `tmp_path / 'stores'` directory containing N zarr stores.
- `staging_dir_with_manifest(zarr_root, store_names, ontologies=...)` — builds a
  staging directory that looks like the output of `prepare-pull`, including a
  valid `.voxhub_manifest.json` written via `RemoteManifest.write()`.
- `server_argv(**kwargs)` — returns an `argparse.Namespace` with defaults
  filled in, so tests only specify what they want to override.
- `parsed_stdout(capsys)` — helper that reads `capsys.readouterr().out` and
  parses it as JSON, asserting it's a single object (not multiple lines).
- `subprocess_server(*args)` (session-scoped) — runs `voxhub-server` via
  `subprocess.run([sys.executable, '-m', 'voxhub_core.server.cli', ...])` and
  returns parsed JSON.

## 4. Function-level test cases

### 4.1 `_run_list_stores` — `TestListStores`

**Happy path:**
- `test_lists_single_empty_store` — one store, no annotations → response has
  one entry, shape/dtype/origin_lps populated, `error is None`,
  `annotations == []`.
- `test_lists_multiple_stores_sorted` — three stores → response lists all
  three, ordering matches `discover_zarr_stores` (alphabetical by name).
- `test_lists_store_with_annotations` — store has a pre-populated annotation
  under `annotations/alice-xyz/ontology-20260101-ab12/data` → response
  entry's `annotations` list contains one entry with correct path, ontology,
  annotator_id, integrated_at.
- `test_includes_dataset_attributes_when_present` — store has
  `dataset_attributes` in root attrs → response's `dataset_attributes` field
  is populated; if absent → field is `None`.
- `test_protocol_version_present` — response has
  `protocol_version == PROTOCOL_VERSION`.

**Error paths:**
- `test_store_with_probe_error` — store exists but is corrupted (missing
  `raw/full`) → entry appears in response with `error` populated, empty
  shape/dtype, empty annotations. Other stores in the same root still
  succeed.
- `test_store_missing_spatial_metadata` — store has `raw/full` but attrs
  lack `ImagePositionPatient` / `PixelSpacing` → entry has `error: 'Missing
  spatial metadata'`, annotations still populated.
- `test_nonexistent_zarr_root` — the handler is called with a
  ``stores_dir`` that doesn't exist → empty stores list (discover
  returns nothing), not a crash.

**Logging:**
- `test_logs_duration_on_completion` — captures stderr, asserts JSON log
  record `list_stores_completed` with `duration_s` key.

### 4.2 `_run_prepare_pull` — `TestPreparePull`

**Happy path:**
- `test_stages_single_store_to_tempdir` — one store in root → response has
  `staging_dir` pointing at a `dt-pull-*` temp directory, `stores[name]` has
  `raw_checksum` (sha256), `shape`, `spacing_mm`, `origin_lps`,
  `space_directions`, `expected_ontologies == []`, `included_annotations == []`.
- `test_uses_explicit_staging_dir_when_provided` — `args.staging_dir=...` → response
  points at exactly that directory, not a `dt-pull-*` tempdir.
- `test_filters_stores_by_name` — root has 3 stores, `--stores a c` → only
  a and c appear in response.
- `test_records_expected_ontologies` — `--ontologies x y z` →
  `stores[name].expected_ontologies == ['x', 'y', 'z']`.
- `test_copies_existing_annotations_when_requested` — store has an
  annotation at `annotations/alice-xyz/.../data`, called with
  `--include-existing-annotations annotations/alice-xyz/seg-20260101-ab12`
  → staging directory contains that annotation copied over.
- `test_compression_flag_propagates_to_stage` — `--compress` flag reaches
  `stage()` (use a spy / mock on `stage`).

**Error paths:**
- `test_stage_failure_writes_error_envelope_and_exits` — patch `stage` to
  raise → response is `ServerError` with `code='prepare_pull_failed'`,
  `sys.exit(1)` called. Use `pytest.raises(SystemExit)`.
- `test_nonexistent_store_name` — `--stores does-not-exist` → behavior
  depends on current implementation (likely produces empty `stores` dict);
  pin down and document.

**Protocol:**
- `test_protocol_version_present`.
- `test_staging_dir_is_string_not_path_object` — JSON serialization correctness.

### 4.3 `_run_integrate_annotations` — `TestIntegrateAnnotations`

This is the largest and most complex handler. Split the class by concern.

#### `TestIntegrateAnnotations_Happy`

- `test_integrates_segmentation_writes_to_annotator_scoped_path` — valid
  seg.nrrd in staging → annotation written at
  `annotations/<annotator_id>-<nano_id>/<ontology>-<date>-<random>/data`,
  response `stores[name].status == 'integrated'`, `annotations` list has
  one entry.
- `test_integrates_landmarks_writes_to_annotator_scoped_path` — analogous
  for landmarks.
- `test_integrates_both_seg_and_landmarks_in_single_call` — staging has both →
  both written, both appear in response.
- `test_provenance_recorded_on_success` — after integration, assert
  `.meta/provenance.jsonl` has a new entry with matching annotator_id,
  machine_id, source checksums. Assert zarr array attrs include
  `integrated_at`, `annotator_id`, etc.
- `test_uses_ontology_from_manifest_not_cli` — manifest
  `expected_ontologies = ['inner-ear-structures']` → integration loads that
  ontology, records `ontology` field in written array attrs.
- `test_checksum_matches_accepted` — pass `--checksums
  segmentation.seg.nrrd:sha256:<correct>` → integration proceeds.

#### `TestIntegrateAnnotations_Errors`

- `test_missing_manifest_writes_error_envelope_and_exits` — staging has no
  `.voxhub_manifest.json` → `ServerError` with `code='manifest_missing'`,
  `SystemExit(1)`.
- `test_checksum_mismatch_writes_error_envelope_and_exits` — wrong checksum
  → `ServerError` with `code='checksum_mismatch'`, `SystemExit(1)`.
- `test_unknown_ontology_produces_warning_but_continues` — manifest lists
  ontology name that doesn't exist → `issues` in response contains a
  warning, integration still proceeds (falls back to `unconstrained`).
- `test_segmentation_validation_error_without_force_blocks_write` — shape
  mismatch in seg.nrrd → no annotation written, `status` reflects failure,
  `issues` contains the error.
- `test_force_allows_integration_despite_errors` — same as above but with
  `--force` → annotation IS written.
- `test_parse_error_recorded_in_issues` — corrupted seg.nrrd → exception
  caught, issue added with `severity='error'`, no annotation written,
  other stores unaffected.
- `test_lock_timeout_surfaces_cleanly` — simulate held lock (by holding
  it in a separate thread in the test) → function blocks until timeout,
  then raises; verify envelope / log.

#### `TestIntegrateAnnotations_MultiStore`

- `test_partial_failure_per_store_isolated` — staging has 2 stores; store A's
  annotation is valid, store B's is corrupted → A integrated, B's response
  entry has `status='failed'` with issues, A unaffected.
- `test_iteration_order_deterministic` — staging has stores `c`, `a`, `b` →
  response stores dict order matches `sorted(staging_dir.iterdir())`.
- `test_skips_hidden_directories` — staging has `.hidden/` → ignored.
- `test_skips_directories_without_matching_zarr_store` — staging has
  `orphan/seg.nrrd` but no `orphan.zarr` in zarr_root → silently skipped
  (verify current behavior, document).

#### `TestIntegrateAnnotations_Ontology`

- `test_segmentation_ontology_resolution_filters_by_type` — manifest has
  both a segmentation and a landmarks ontology → seg gets segmentation
  ontology, landmarks get landmarks ontology.
- `test_first_matching_ontology_used` — manifest has two segmentation
  ontologies → first is used; verify this matches the current
  `seg_ontologies[0]` behavior in code (line 446).
- `test_no_matching_ontology_uses_unconstrained_fallback` — manifest has
  only landmarks ontology but staging has only seg → seg written with
  `ontology='unconstrained'`, `ontology_version=1`.

### 4.4 `_run_cleanup` — `TestCleanup`

- `test_removes_existing_staging_dir` — dir exists → removed, response
  `status == 'ok'`.
- `test_noop_when_staging_dir_missing` — dir doesn't exist → response still
  `status == 'ok'`, warning logged, no exception.
- `test_refuses_to_remove_non_staging_path` — **security concern**:
  current implementation removes *any* path passed in. Document this as
  a finding. If the worktree owner wants to add a safety check (e.g.,
  only remove paths matching `dt-*` or under `tempfile.gettempdir()`),
  these tests would enforce it. Otherwise xfail with an explanation.

### 4.5 `_run_gc` — `TestGc`

- `test_removes_dirs_older_than_ttl` — create `/tmp/dt-pull-xxx` with
  mtime 48h in the past, `--ttl-hours 24` → removed, listed in
  `response['removed']`.
- `test_keeps_dirs_newer_than_ttl` — recent dir → kept.
- `test_ignores_non_dt_prefix` — `/tmp/foo-bar` → ignored even if old.
- `test_ignores_files_only_dirs` — file at `/tmp/dt-file` → ignored.
- `test_count_matches_removed_length` — invariant check.
- `test_default_ttl_24_hours` — no `--ttl-hours` → uses 24.0.
- **Isolation concern:** these tests must not scan the real `/tmp`.
  Monkey-patch `tempfile.gettempdir()` to return a test-owned `tmp_path`
  so the test doesn't clobber unrelated `dt-*` dirs.

### 4.6 `_run_validate_attributes` — `TestValidateAttributes`

- `test_store_without_dataset_attributes_reports_missing` — store has no
  `dataset_attributes` root attr → `results[store]['status'] == 'missing'`.
- `test_store_with_valid_attributes_reports_ok` — consistent attrs →
  `status == 'ok'`.
- `test_store_with_mismatched_voxel_size_reports_warning` — declared voxel
  size differs from computed spacing → `status == 'warning'`, `issues`
  list populated with fields `field`, `declared`, `actual`, `message`.
- `test_filters_stores_by_name` — `--stores a` → only a evaluated.
- `test_protocol_version_present`.

### 4.7 `_run_healthcheck` — `TestHealthcheck`

- `test_healthy_all_green` — well-formed stores directory, all checks pass →
  `status == 'healthy'`, all checks `status == 'ok'`, exit 0.
- `test_degraded_when_stores_dir_unwritable` — read-only dir →
  `status == 'degraded'`, `stores_dir` check fails, `SystemExit(1)`.
- `test_degraded_when_store_corrupted` — one store has probe error →
  `stores` check fails.
- `test_provenance_check_ok_when_file_missing` — no `.meta/provenance.jsonl`
  → check passes with detail `'no provenance file yet'`.
- `test_provenance_check_fails_on_malformed_jsonl` — write a JSONL with a
  malformed line → check fails.
- `test_store_and_provenance_checks_skipped_when_stores_dir_fails` — if
  stores_dir check fails, store/provenance checks aren't run (they'd crash
  otherwise).
- `test_python_version_check` — asserts reports current Python version.
- `test_rsync_check_when_rsync_present` / `test_rsync_check_when_absent` —
  monkey-patch `shutil.which` to control.
- `test_packages_check_all_importable` — at runtime, all three packages
  import successfully.

### 4.8 `main()` — `TestMainEntry`

- `test_no_command_prints_help_and_exits_zero` — `args.command is None`.
- `test_unknown_command_argparse_error` — argparse exits 2.
- `test_unhandled_exception_returns_server_error_envelope` — patch one
  handler to raise `RuntimeError('boom')` → stdout has `ServerError` with
  `code='internal_error'`, `message='boom'`, exit 1. This covers the
  try/except at lines 945-950.
- `test_settings_loaded_on_entry` — spy on `load_settings` to confirm
  it's called before command dispatch.

## 5. Subprocess-level test cases (smoke)

File: `test_server_cli_subprocess.py`. Use a session-scoped `subprocess_server`
fixture. Mark slow if they add > 1s total.

- `test_voxhub_server_entry_point_installed` — `which voxhub-server`
  returns a path (or `python -m voxhub_core.server.cli` works).
- `test_list_stores_via_subprocess` — build zarr root, run
  `voxhub-server list-stores <root>`, parse stdout, assert
  `protocol_version == 1`, `stores` is a list.
- `test_prepare_pull_via_subprocess` — single store, verify returned JSON
  has `staging_dir` and a valid temp path. Clean up the returned staging_dir.
- `test_integrate_annotations_via_subprocess` — full round trip: stage →
  build annotation → integrate → verify zarr state. This is the one
  end-to-end test that exercises real argparse, real I/O, and the real
  entrypoint.
- `test_cleanup_via_subprocess` — happy path.
- `test_gc_via_subprocess` — happy path.
- `test_validate_attributes_via_subprocess` — happy path.
- `test_healthcheck_via_subprocess_exit_code_on_degraded` — point at
  nonexistent root → exit 1, stdout valid JSON.
- `test_no_command_prints_help_exit_zero` — `voxhub-server` with no args.
- `test_invalid_subcommand_argparse_error` —
  `voxhub-server nonsense-cmd` → exit 2.

## 6. Out of scope for this plan

- **Security boundary tests** (path traversal, annotator isolation) —
  covered by the strategic plan §5. Cross-reference but don't duplicate.
- **Concurrent integrate tests** — see `concurrency-and-provenance.md`.
- **Performance / benchmark tests** — strategic plan §8, deferred.
- **Protocol mismatch tests** between client and server — belongs in
  `voxhub-client`'s test suite.

## 7. Open questions for refinement

1. **Invoking subprocess** — should `test_server_cli_subprocess.py` use
   `[sys.executable, '-m', 'voxhub_core.server.cli', ...]` (portable) or
   resolve `voxhub-server` via `shutil.which` (tests installation)? The
   first is more hermetic; the second actually validates the entry point.
   Recommendation: **both** — a single test for `which voxhub-server`,
   rest use `python -m`.
2. **Spy vs real calls** — for `_run_prepare_pull` tests, is it OK to
   monkey-patch `stage()` to return a canned dict, or should tests go
   end-to-end? Recommendation: **real calls for happy path**, monkey-patch
   only for injecting exceptions.
3. **GC test isolation** — monkey-patching `tempfile.gettempdir()` is
   invasive. Alternative: refactor `_run_gc` to accept `tmp_root` as an
   argument (currently hardcoded to `Path(tempfile.gettempdir())` at line
   641). Recommend the refactor in the worktree.
4. **Cleanup safety check** — the current `_run_cleanup` does
   `shutil.rmtree(staging_dir)` on whatever path is passed. Should this be
   hardened to only delete `dt-*` paths? If yes, test enforces it.

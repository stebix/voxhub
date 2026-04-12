# Test Plan: catalog / staging / audit

**Target modules:**
- `packages/voxhub-core/src/voxhub_core/catalog.py` (315 LOC)
- `packages/voxhub-core/src/voxhub_core/staging.py` (346 LOC) — **end-to-end
  `stage()` flow only; the NRRD writer is already tested in
  `test_staging.py`**
- `packages/voxhub-core/src/voxhub_core/audit.py` (313 LOC)

**Stub files:**
- `tests/test_catalog.py`
- `tests/test_staging_e2e.py`
- `tests/test_audit.py`

**Priority:** P2 — these are domain modules that back the server handlers.
They're exercised indirectly through server tests, but deserve direct unit
coverage for faster failure localization.

## 1. Scope and rationale

These three modules share a common flavor: they walk a zarr root, discover
stores, and produce structured reports. None of them are directly tested
today, even though:

- `catalog.py` is called by `_run_list_stores`, `_run_prepare_pull`,
  `_run_validate_attributes`, `_run_healthcheck`, and the local `catalog`
  CLI command.
- `staging.py::stage()` is called by `_run_prepare_pull` and the local
  `stage` CLI command.
- `audit.py::audit()` is called by the local `audit` CLI command.

When these modules misbehave, the server tests in `test_server_cli.py` will
catch it, but they'll be hard to diagnose. Direct tests give a sharper
error signal.

## 2. Fixtures required

- `stores_dir_factory` — same fixture as `server-cli.md`; shared.
- `create_zarr_store_with_annotations(path, annotator_specs)` — extends
  the existing `create_zarr_store` helper to also pre-populate annotations
  under the annotator-scoped path convention. Signature sketch:
  ```python
  create_zarr_store_with_annotations(
      path,
      annotations=[
          {'annotator_id': 'alice', 'nano_id': 'xyz12345',
           'ontology': 'inner-ear-structures', 'version': 1,
           'kind': 'segmentation', 'label_map': ..., 'segments': [...]},
          {'annotator_id': 'bob', 'nano_id': 'abc67890',
           'ontology': 'inner-ear-landmarks', 'version': 1,
           'kind': 'landmarks', 'points': [...], 'labels': [...]},
      ],
  )
  ```
  This should produce zarr arrays at the canonical paths with the expected
  attributes, so catalog/audit tests don't need to fake the layout.

## 3. `test_catalog.py` — store discovery & probing

### 3.1 `TestDiscoverZarrStores` — discovery

- `test_discovers_single_store_at_root` — one `foo.zarr` directly under
  the root → `discover_zarr_stores(root)` returns one `ZarrEntry` with
  `path.name == 'foo.zarr'`.
- `test_discovers_multiple_stores` — three stores → three entries,
  sorted by name (verify ordering is deterministic).
- `test_discovers_nested_stores` — stores under subdirectories (if
  supported) → verify current recursion behavior and document it.
- `test_ignores_non_zarr_directories` — root contains `foo.zarr`,
  `bar/`, `baz.txt` → only `foo.zarr` discovered.
- `test_empty_root_returns_empty_list` — fresh tmp_path → empty list.
- `test_nonexistent_root_behavior` — path doesn't exist → document
  behavior (probably raises or returns empty). Pin it down.

### 3.2 `TestProbeZarrEntry` — per-store probing

- `test_probes_shape_and_dtype` — valid store → `entry.shape` and
  `entry.dtype` populated from `raw/full`.
- `test_probes_attributes_dict` — valid store → `entry.attributes` has
  the `ImagePositionPatient`, `ImageOrientationPatient`, etc. keys.
- `test_probes_source_and_series_directory` — store with
  `source_directory` and `series_directory` in attrs → both populated
  on the entry.
- `test_corrupted_store_records_error_not_raise` — delete `raw/full` →
  entry has `error` populated, other fields empty. The function must
  not raise.
- `test_store_without_spatial_metadata` — store missing ImagePosition*
  attrs → depending on current behavior, either `error` populated or
  empty `attributes`. Pin down and document.
- `test_dataset_attributes_populated_when_present` — store has root
  `dataset_attributes` attr → `entry.dataset_attributes` dict matches.
- `test_dataset_attributes_none_when_absent` — no dataset attrs →
  `entry.dataset_attributes is None`.

### 3.3 `TestDiscoverAnnotations`

This covers `_discover_annotations` (lines 48–89 of `catalog.py`),
which walks the `annotations/` hierarchy.

- `test_no_annotations_group_returns_empty_list` — store without
  `annotations/` → `entry.annotations == []`.
- `test_single_annotation_discovered` — store with one annotator and
  one instance → `entry.annotations` has one `AnnotationEntry` with
  correct path, ontology, ontology_version, annotator_id, integrated_at.
- `test_multiple_annotators_discovered` — two annotator-scoped
  subgroups, each with one instance → both discovered.
- `test_multiple_instances_per_annotator` — same annotator has two
  instance dirs → both discovered.
- `test_annotation_missing_ontology_attr_graceful` — annotation array
  exists but lacks `ontology` attr → entry appears with
  `ontology == None` or empty (document expected behavior).
- `test_annotation_path_format_matches_convention` — discovered path
  is `annotations/<annotator>-<nano>/<ont>-<date>-<rand>/data`.

### 3.4 `TestCatalogRendering` — rich rendering

These are smoke tests — just verify the functions don't crash on
representative inputs. Visual correctness is not worth asserting here.

- `test_build_tree_on_empty_root` — returns a Tree instance.
- `test_build_tree_with_stores_and_annotations` — returns a Tree
  instance with expected number of children.
- `test_build_summary_table_on_empty_root` — returns a Table instance.
- `test_build_summary_table_with_stores` — returns a Table with N
  rows matching N stores.

## 4. `test_staging_e2e.py` — end-to-end `stage()`

The existing `test_staging.py` covers the low-level NRRD writer
(`_write_nrrd_raw`). This file covers the **public** `stage()` function
and `extract_spatial_metadata()`.

### 4.1 `TestExtractSpatialMetadata`

- `test_canonical_axis_aligned_metadata` — store with identity-like
  orientation → origin/directions/spacing match what
  `_core_helpers.create_zarr_store` writes.
- `test_oblique_orientation` — non-identity `ImageOrientationPatient`
  → `space_directions` matrix reflects the rotation.
- `test_anisotropic_spacing` — `PixelSpacing` differs from
  `computed_slice_spacing_mm` → the returned `spacing_mm` triple
  reflects the mix correctly.
- `test_missing_image_position_patient_raises_keyerror` — no
  `ImagePositionPatient` attr → `KeyError`.
- `test_missing_pixel_spacing_raises_keyerror` — no `PixelSpacing` →
  `KeyError`.
- `test_missing_computed_slice_spacing_raises_keyerror` → `KeyError`.
- `test_returns_numpy_arrays_for_origin_and_directions` — types are
  `np.ndarray`, not Python lists (the function's contract).

### 4.2 `TestStageSingleStore` — happy path

- `test_creates_staging_dir_with_store_subdirectory` —
  `stage(root, staging_dir, store_names=['foo'])` → `staging_dir/foo/` exists.
- `test_writes_raw_nrrd` — `staging_dir/foo/raw.nrrd` exists and can be
  read via `nrrd.read`.
- `test_nrrd_data_matches_zarr_data` — read back both; they match
  (modulo the axis reversal documented in `test_staging.py`).
- `test_manifest_structure` — returned manifest is a dict with
  `store_name` keys; each value is a dict with `zarr_path`,
  `raw_checksum`, `shape`, `origin_lps`, `space_directions`,
  `spacing_mm`.
- `test_raw_checksum_is_sha256_hex` — checksum string is
  `sha256:<64 hex chars>`.
- `test_checksum_matches_actual_file_contents` — compute sha256 of
  `staging_dir/foo/raw.nrrd` directly; compare to manifest value.

### 4.3 `TestStageMultipleStores`

- `test_stages_all_stores_when_names_none` — `store_names=None` → all
  stores in the stores directory are staged.
- `test_stages_subset_when_names_provided` — `store_names=['a', 'c']`
  → only those two appear in manifest and on disk.
- `test_nonexistent_store_name_behavior` — `store_names=['missing']`
  → document current behavior (likely a warning or silent skip; pin
  down).

### 4.4 `TestStageCompressionFlag`

- `test_compressed_nrrd_is_gzipped` — `stage(..., compress=True)` →
  output file is gzip-valid (starts with `\x1f\x8b`).
- `test_compressed_and_uncompressed_data_equivalent` — stage the same
  store twice (compress=True and compress=False), read both, compare
  arrays. Tolerate header key differences.

### 4.5 `TestStageForceAndErrors`

- `test_refuses_to_overwrite_without_force` — pre-populate staging_dir
  with a file that would conflict → `FileExistsError`.
- `test_force_overwrites_existing_staging_contents` — same setup but
  `force=True` → succeeds, old contents gone.
- `test_missing_stores_dir_raises` — `stores_dir` doesn't exist →
  `FileNotFoundError`.
- `test_zarr_store_without_raw_full_raises_or_skips` — store lacks
  `raw/full` → document behavior, pin it down.

## 5. `test_audit.py` — cross-store coherence

### 5.1 `TestProbeStoreAnnotations`

- `test_store_with_segmentation_only` — returns `StoreAnnotationInfo`
  with `has_segmentation=True`, `segment_map` populated.
- `test_store_with_landmarks_only` — returns info with
  `has_landmarks=True`, `landmark_labels` populated.
- `test_store_with_both` — both flags true, both fields populated.
- `test_store_with_neither_annotation_type` — both flags false,
  fields empty.
- `test_corrupted_store_records_error` — store with broken
  `annotations/` → `error` populated, function doesn't raise.

### 5.2 `TestSegmentationCoherence`

Setup: build 3 stores with overlapping but not identical segment sets.

- `test_coherent_stores_no_issues` — all 3 stores have the same
  segment set → zero issues reported.
- `test_missing_segment_flagged` — store A has `cochlea`, stores B/C
  have `cochlea` + `vestibule` → A is missing `vestibule`, flagged
  with severity='warning' and category='missing_segment'.
- `test_extra_segment_flagged` — store A has an extra
  `non_standard_label` not present in the majority set → flagged
  with category='extra_segment'.
- `test_label_value_mismatch_flagged` — stores A and B both have
  `cochlea`, but A has `label_value=1` and B has `label_value=2` →
  flagged with category='label_mismatch'.
- `test_majority_set_used_as_reference` — 3 stores with set X, 1
  store with set Y → reference is X, the one with Y is flagged for
  each diff.
- `test_tie_break_when_no_majority` — 2 stores with X, 2 with Y →
  document current behavior (likely flags all non-reference; pin
  down which is chosen as reference).

### 5.3 `TestLandmarkCoherence`

- `test_coherent_landmark_labels_no_issues` — all stores have the
  same landmark label set.
- `test_missing_landmark_flagged` — store is missing a required
  landmark that others have → flagged.
- `test_extra_landmark_flagged` — store has a landmark not in the
  majority set → flagged.

### 5.4 `TestAuditFiltering`

- `test_ontology_filter_restricts_analysis` — audit with
  `ontology_filter='inner-ear-structures'` → stores without that
  ontology are excluded from comparisons.
- `test_store_names_filter` — `store_names=['a', 'c']` → only those
  compared.
- `test_empty_root_returns_no_issues` → `[]`.
- `test_single_store_returns_no_issues` — one store alone can't have
  coherence issues → `[]`.

### 5.5 `TestAuditRendering`

Similar to the catalog rendering tests — smoke tests only.

- `test_audit_renders_empty_report_without_crash`.
- `test_audit_renders_populated_report_without_crash`.

## 6. Out of scope

- **Performance of `discover_zarr_stores` on large roots** — deferred.
- **File watching / change detection** — not a feature of catalog.py.
- **Export-related staging** — covered by the export test plan (not
  included in this round).

## 7. Open questions for refinement

1. **Nested discovery** — `discover_zarr_stores` currently walks in a
   specific way; document whether it's flat-only or recursive. Tests
   in §3.1 will surface this.
2. **Audit reference-set choice under ties** — the current
   `_check_segmentation_coherence` uses a "majority set" for reference.
   What happens under ties? Need to read the implementation carefully
   when fleshing out §5.2 `test_tie_break_when_no_majority`.
3. **`stage()` CLI args vs function args** — `stage()` takes a Console;
   the test may want to pass a `Console(file=io.StringIO())` to capture
   output without cluttering test logs.
4. **Staging of already-staged stores** — if `staging_dir/foo/` exists from
   a previous stage, what happens on re-stage? Force flag is tested in
   §4.5, but verify the non-force failure mode produces a useful
   message.

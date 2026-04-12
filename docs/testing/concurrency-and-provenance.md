# Test Plan: Concurrency & Provenance

**Target modules:**
- `packages/voxhub-core/src/voxhub_core/server/provenance.py` (144 LOC)
- `packages/voxhub-core/src/voxhub_core/server/locks.py` (28 LOC)
- Concurrent interactions with `_run_integrate_annotations` in `server/cli.py`

**Stub files:**
- `tests/test_server_provenance.py`
- `tests/test_concurrency.py`

**Priority:** P1 (concurrency) / P2 (provenance integrity) — see strategic
plan sections 6 and 7.

## 1. Why these two plans are bundled

Provenance recording and concurrency safety are tightly coupled:

- `record_provenance()` writes to two places: (a) zarr array attrs (inside
  the zarr store, protected by `store_lock`) and (b) the central
  `.meta/provenance.jsonl` file (**not** protected by `store_lock`, because
  it's shared across stores under a single zarr root).
- The JSONL append relies on POSIX `O_APPEND` atomicity for writes under
  `PIPE_BUF` (4 KB on Linux). JSON records are usually well under this, but
  the guarantee is implicit, undocumented, and worth testing.
- Concurrent multi-annotator scenarios are where both systems are exercised
  simultaneously, so test coverage benefits from sharing fixtures.

## 2. Fixtures required

Beyond existing helpers:

- `provenance_jsonl_factory(path, *, entries)` — writes a synthetic
  provenance JSONL file. Used for testing `validate_provenance_jsonl`
  without having to drive a full integration.
- `concurrent_integrate_runner` — launches N subprocesses in parallel via
  `multiprocessing.Process` or `subprocess.Popen`, each running
  `voxhub-server integrate-annotations` against the same zarr root,
  returns their exit codes and parsed JSON outputs.
- `held_lock` — context manager that acquires a `FileLock` in a separate
  process and releases it on exit. Used to test lock-timeout paths.

## 3. `test_server_provenance.py` — record_provenance + validate_provenance_jsonl

### 3.1 `TestRecordProvenance` — happy paths

- `test_writes_zarr_array_attributes` — call `record_provenance()` against
  a pre-populated annotation array → assert the array's attrs dict now
  contains `integrated_at`, `annotator_id`, `machine_id`, `nano_id`,
  `pull_session_id`, `source_nrrd_checksum`, `source_file`, `ontology`,
  `ontology_version`. Check types (string timestamps, etc.).
- `test_appends_single_line_to_jsonl_index` — after the call,
  `.meta/provenance.jsonl` exists and has exactly one line; parsing it
  yields a dict with the expected keys.
- `test_creates_meta_directory_if_missing` — `.meta/` doesn't exist →
  created with parents=True.
- `test_subsequent_calls_append_not_overwrite` — call twice → two lines
  in the JSONL, both parse, both refer to the same annotation_path.
- `test_timestamp_is_iso_utc` — `integrated_at` parses as ISO 8601 with
  explicit UTC offset.
- `test_issues_list_empty_when_none_passed` — `issues=None` → JSONL
  record has `issues: []`, not `issues: null`.
- `test_issues_list_populated_when_warnings_passed` — pass two
  `IssueRecord`s → JSONL line has both serialized.
- `test_nested_annotation_path_traversal` — `annotation_path =
  'annotations/alice-xyz/seg-20260101-ab12/data'` → correctly navigates
  via `node[part]` and updates attrs on the deepest array.
- `test_strips_leading_trailing_slash` — path with leading `/` or
  trailing `/` still resolves correctly (`annotation_path.strip('/')`).

### 3.2 `TestRecordProvenance` — durability

- `test_fsync_called_on_jsonl_write` — monkey-patch `os.fsync` and assert
  it's called with the JSONL file descriptor. Rationale: the strategic
  plan mandates fsync as "cheap insurance". A regression that removes
  the `os.fsync(f.fileno())` call should fail this test.
- `test_jsonl_flushed_before_function_returns` — after
  `record_provenance` returns, reading the JSONL file in a fresh open
  sees the new line (no buffering).

### 3.3 `TestRecordProvenance` — error paths

- `test_missing_zarr_store_raises` — `store_name` points at a
  nonexistent `.zarr` directory → `zarr.open_group` raises; the function
  propagates (no silent failure).
- `test_missing_annotation_path_raises` — `annotation_path` traverses a
  group that doesn't exist → `KeyError` propagates.
- `test_readonly_meta_directory` — `.meta/` exists but is read-only →
  `PermissionError` propagates; **no partial write** (verify JSONL is
  unchanged).

### 3.4 `TestValidateProvenanceJsonl`

- `test_missing_file_returns_empty_list` — file doesn't exist → `[]`
  (not an error per the docstring at line 118).
- `test_empty_file_returns_empty_list` — zero bytes → `[]`.
- `test_valid_file_returns_empty_list` — several valid JSON lines → `[]`.
- `test_malformed_line_reports_line_number` — one bad line in a valid
  file → result has one error string mentioning the line number.
- `test_multiple_malformed_lines_all_reported` — three bad lines → three
  error strings.
- `test_blank_lines_ignored` — valid JSON with blank lines interspersed
  → still `[]`.
- `test_utf8_handling` — annotator name with non-ASCII characters (e.g.
  `müller`, `李`) round-trips through the JSONL without mojibake.

## 4. `test_concurrency.py` — filelock + multi-process integrate

### 4.1 `TestStoreLock` — unit tests for `store_lock`

- `test_returns_filelock_instance` — `store_lock(path)` returns a
  `FileLock`. Trivial, but pins the contract.
- `test_lock_file_is_sibling_of_zarr_dir` — for
  `path = /root/foo.zarr`, lock file is `/root/foo.zarr.lock`.
- `test_lock_is_exclusive_same_process` — acquire in one thread, assert
  second acquisition with short timeout raises `Timeout`.
- `test_lock_released_on_context_exit` — `with store_lock(p): pass` →
  lock file still exists but is unlocked (next acquirer succeeds).
- `test_custom_timeout_propagates` — `store_lock(p, timeout=0.1)` →
  attempting to acquire a held lock raises within ~0.1s.

### 4.2 `TestConcurrentIntegrateSameStore`

All tests in this class use **subprocess / multiprocess**, never threads,
because production concurrency is cross-process only (SSH spawns a fresh
`voxhub-server` process per invocation).

- `test_two_concurrent_writes_to_same_store_serialize` — fixture: zarr
  store, two distinct staging dirs each with a different annotator_id.
  Launch two `integrate-annotations` subprocesses in parallel. Assert:
  - both exit 0
  - both annotations end up in the store (different annotator-scoped
    paths)
  - provenance JSONL has two entries, one per annotator
  - neither run sees a partially-written annotation from the other.
- `test_concurrent_writes_do_not_deadlock` — launch 5 concurrent
  integrates; all complete within N seconds (parameterize N).
- `test_lock_contention_respects_timeout` — patch one subprocess to hold
  the lock for longer than the other's timeout → the second subprocess
  fails cleanly with a timeout error surfaced in its JSON envelope, not
  a crash.
- `test_lock_released_after_crash` — launch a subprocess that crashes
  (via injected SIGKILL or `sys.exit(1)` mid-operation) while holding
  the lock → after it dies, a new subprocess can acquire the lock
  (filelock recovery). Note: depends on filelock library semantics,
  document.

### 4.3 `TestConcurrentIntegrateDifferentStores`

- `test_two_different_stores_fully_parallel` — two zarr stores, two
  integrates in parallel → both complete concurrently (they should not
  serialize, because each acquires its own store_lock).

### 4.4 `TestConcurrentProvenanceAppend`

This is the area where the implicit POSIX atomicity assumption lives.

- `test_two_different_stores_append_single_jsonl_concurrently` — two
  stores under the same stores_dir, each gets an integrate in parallel
  → both provenance entries land in `.meta/provenance.jsonl`, both
  lines parse as valid JSON, no interleaved/truncated lines.
- `test_high_concurrency_jsonl_integrity` — 10 concurrent appends to
  `.meta/provenance.jsonl` (stores differ, so `store_lock` doesn't
  serialize them). Assert:
  - exactly 10 lines in the file
  - every line parses as valid JSON
  - every line has a unique `annotator_id`/`nano_id` combination.
- `test_large_jsonl_record_still_atomic` — craft a record > 4 KB (e.g.
  200 issues) → append is still atomic. This is where the `PIPE_BUF`
  guarantee breaks down; document the expected behavior and, if broken,
  this test becomes the justification for adding an explicit lock
  around the JSONL append.
- `test_jsonl_append_lock_contention_documented` — synthetic stress
  test: 100 concurrent short appends. Run under `pytest.mark.slow`.
  Primarily a regression canary, not a strict correctness test.

### 4.5 `TestAnnotatorIsolation`

Overlaps with strategic plan §5 (security boundaries). Cover the basics
here, defer deeper path-traversal tests to the security plan:

- `test_concurrent_multi_annotator_isolation` — two integrates with
  different `annotator_id` → each writes under its own
  `annotations/<annotator>-<nano>/` path, neither touches the other's
  attrs.
- `test_same_annotator_two_pushes_different_instances` — two integrates
  with identical annotator_id + nano_id but different staging dirs → two
  separate instance directories (because `instance_dir` includes a
  random suffix), both co-exist, both appear in list-stores.

## 5. Out of scope

- **Path traversal / input sanitization** — strategic plan §5.
- **Backup / restore** — strategic plan §9.
- **Network-layer SSH tests** — out of scope for `voxhub-core`; belongs
  in deployment / integration test suite.

## 6. Open questions for refinement

1. **JSONL append lock** — should the implementation gain an explicit
   file lock around the JSONL append, independent of `store_lock`? The
   current code relies on OS-level atomicity, which is implicit. Tests
   in §4.4 will reveal whether this matters in practice. If they fail
   under load, we should add a `meta_lock` helper.
2. **Subprocess launch mechanism** — `multiprocessing.Process` (spawns
   a child that runs a Python callable) vs `subprocess.Popen` (runs
   `voxhub-server` binary). The latter is more faithful to production;
   the former is easier to debug. Recommendation: **`subprocess.Popen`**
   for the multi-store and JSONL tests; `multiprocessing` only where
   it's easier to share state (e.g., the "crash mid-operation" test).
3. **Platform portability** — filelock semantics differ between Linux
   and macOS (both POSIX, but edge cases exist). These tests target
   Linux (the deployment platform) and may need `pytest.mark.linux`
   guards for any test that depends on specific FS semantics.
4. **Flakiness budget** — concurrency tests are inherently timing-
   sensitive. Use generous timeouts, retry on known-flaky tests,
   or mark them `@pytest.mark.flaky(reruns=2)` if pytest-rerunfailures
   is added. Better: eliminate timing dependencies where possible by
   using explicit synchronization primitives (barriers, events) rather
   than `sleep`.

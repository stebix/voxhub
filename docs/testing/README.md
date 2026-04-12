# voxhub-core Testing Plans

Concrete test-case specifications for the under-tested parts of `voxhub-core`.
These plans are intended to be refined and implemented in downstream worktrees.

## Relationship to `docs/testing-plan.md`

The existing top-level `docs/testing-plan.md` is the **strategic** testing
document: CI wiring, coverage thresholds, smoke tests, P0/P1/P2 prioritization.

The plans in this directory are the **tactical** specifications that fulfill
its P1 and P2 sections. They enumerate concrete test cases, fixtures, and
scaffolding required to cover specific untested modules.

| Top-level plan section         | Tactical plan                                                  | Status      |
|--------------------------------|----------------------------------------------------------------|-------------|
| §4 Server Command Integration  | [server-cli.md](server-cli.md)                                 | implemented |
| §6 Concurrent Writer Safety    | [concurrency-and-provenance.md](concurrency-and-provenance.md) | implemented |
| §7 Provenance / Audit          | [concurrency-and-provenance.md](concurrency-and-provenance.md) | implemented |
| (gap — not in strategic plan)  | [catalog-staging-audit.md](catalog-staging-audit.md)           | stubbed     |

## Scope

These plans cover the following currently-untested surfaces in `voxhub-core`:

- **`server/cli.py`** (951 lines, 0 direct tests) — all seven subcommands,
  at two layers: function-level (`_run_X(args)`) and subprocess-level
  (`voxhub-server <cmd>`).
- **`server/provenance.py`** — `record_provenance`, `validate_provenance_jsonl`,
  dual-write (zarr attrs + JSONL), `fsync` durability.
- **`server/locks.py`** + concurrency — per-store `FileLock` behavior,
  multi-process integrate races, JSONL append interleaving.
- **`catalog.py`** — `discover_zarr_stores`, annotation discovery, probe
  error handling.
- **`staging.py`** — end-to-end `stage()` flow (the NRRD writer itself is
  already tested in `test_staging.py`).
- **`audit.py`** — cross-store coherence checks for segmentations and landmarks.

Out of scope (covered elsewhere or deliberately deferred):

- `export.py` serial / parallel export — deferred per user direction.
- `voxhub-schema` tests — already well-covered.
- Security boundary tests (path traversal, annotator isolation) — covered by
  the strategic plan §5, not re-specified here.

## Deliverable layout

```
docs/testing/
├── README.md                         (this file)
├── server-cli.md                     (§server/cli.py plans)
├── concurrency-and-provenance.md     (§locks + provenance plans)
└── catalog-staging-audit.md          (§domain module plans)

packages/voxhub-core/tests/
├── test_server_cli.py                (function-level stubs, all 7 commands)
├── test_server_cli_subprocess.py     (subprocess smoke-test stubs)
├── test_server_provenance.py         (provenance recording stubs)
├── test_concurrency.py               (filelock + race stubs)
├── test_catalog.py                   (discovery stubs)
├── test_staging_e2e.py               (end-to-end stage() stubs)
└── test_audit.py                     (coherence audit stubs)
```

Stub test files are created with `pytest.mark.skip` on each test so the
suite remains green until the worktree owner implements them. Each stub has a
docstring describing what it should verify. The `server-cli.md` and
`concurrency-and-provenance.md` plans are fully implemented — see those plan
files and `test_server_cli*.py` / `test_server_provenance.py` /
`test_concurrency.py` for the delivered coverage.

## Shared fixtures required

The existing `tests/conftest.py` and `tests/_core_helpers.py` provide
ontology fixtures and single-store builders. The new tests require extensions:

### New fixtures (to add to `conftest.py`)

- `zarr_root_factory` — pytest fixture producing a `tmp_path` containing one
  or more zarr stores built via `create_zarr_store`. Parameterized on store
  count and on whether existing annotations are pre-populated.
- `wip_dir_with_manifest` — builds a WIP directory complete with a valid
  `.voxhub_manifest.json` written via `RemoteManifest.write()`, so server
  integration tests don't have to fabricate manifests by hand.
- `server_argv` — helper fixture producing an `argparse.Namespace` shaped like
  what `voxhub-server <cmd>` would parse, to simplify function-level server
  tests.
- `subprocess_server` — session-scoped fixture that locates the
  `voxhub-server` entrypoint (via `importlib.util.find_spec` + `sys.executable`
  or `shutil.which`) and returns a callable that runs it and returns parsed
  JSON stdout. Centralizes subprocess invocation logic.

### New builder helpers (to add to `_core_helpers.py`)

- `create_zarr_root(root_path, store_specs)` — build multiple stores under a
  single parent directory in one call.
- `write_remote_manifest(wip_dir, store_names, ...)` — thin wrapper around
  `RemoteManifest.write()` with sensible defaults.
- `write_provenance_jsonl(path, entries)` — write a synthetic provenance JSONL
  file for audit/validation tests.
- `populate_store_annotation(zarr_path, annotator_id, ontology, kind, ...)` —
  populate an existing zarr store with a pre-made annotation at the canonical
  annotator-scoped path, for tests that need `list-stores` to see prior work.

The stub files reference these fixtures and helpers by name; the worktree
implementation adds them when fleshing out the tests.

## Implementation conventions

- **One module, one test file** — follow the existing pattern.
- **Class-based grouping** — use `class TestX:` to group related cases, matching
  `test_integrate.py` / `test_slicer.py` style.
- **Function-level server tests** construct an `argparse.Namespace` directly and
  call `_run_X(args)`. They capture `sys.stdout` (via `capsys`) and parse the
  JSON envelope to assert structure.
- **Subprocess-level server tests** invoke `voxhub-server` via `subprocess.run`
  and parse `result.stdout` as JSON. Mark these `@pytest.mark.slow` if needed.
- **Concurrent tests** use `multiprocessing` or `subprocess.Popen` (never
  `threading`), because the production concurrency model is cross-process only.
- **No mocking of zarr or the filesystem.** Use `tmp_path` fixtures throughout;
  these tests are integration-flavored by nature.

## How to work with these plans

1. Pick a plan file (e.g., `server-cli.md`).
2. Create a worktree for that scope.
3. Flesh out the corresponding stub file(s) in `packages/voxhub-core/tests/`,
   adding fixtures/helpers as described above.
4. Remove `pytest.mark.skip` as each test becomes real.
5. Run `uv run pytest packages/voxhub-core/tests/test_X.py -v` to verify.
6. Commit the fleshed-out tests + any new fixtures together.

# Internal rename: `zarr_root` → `stores_dir` for perfect consistency

## Context

After the two-PR wire-protocol refactor (`docs/plans/store-basedir-refactor.md`,
commits 7938e3d + f383cd6), the directory-of-zarr-stores concept is called
`stores_dir` on the wire, in the schema, in the server CLI argparse, in the
client `SshTarget`, in user-facing docs, and in the `server.toml`
`[storage].stores_dir` key.

Internal symbols still use the old name:

- Core library function parameters (`stage`, `integrate`, `audit`,
  `record_provenance`).
- Local variables inside those modules.
- Developer-facing `voxhub-core` local CLI (`cli.py` — distinct from
  `server/cli.py`) argparse dests and handlers.
- Test fixtures (`zarr_root_factory`), test helper parameters, test body locals,
  test function names, and invocation-dict keys in the concurrency harness.
- Deploy-script CLI flags (`--zarr-root`) and shell variables (`$ZARR_ROOT`).
- Lingering prose in `docs/testing/*.md`, `packages/voxhub-core/README.md`,
  and `docs/deployment-readiness.md`.

None of these cross the wire. All are safe to rename together with simple
mechanical edits + green test suite as the verification gate.

Survey baseline: ~250 hits of `zarr_root`/`ZARR_ROOT`/`zarr-root` across the
repo outside already-renamed sites.

## What stays

- **`zarr_path`** — the path of an individual `*.zarr` store
  (`zarr_path = stores_dir / f'{store_name}.zarr'`). Meaningfully different
  from `stores_dir`; keep.
- **`discover_zarr_stores`**, `.zarr` suffix, any literal reference to zarr
  library concepts. These describe zarr, not a directory of stores.
- **`docs/plans/store-basedir-refactor.md`** — historical planning doc.
  Don't edit; it describes how we got here.
- **`packages/voxhub-client/src/voxhub_client/cli.py`** — stale duplicate of
  the pre-refactor server CLI. Violates the core/client DAG (see
  `test_architecture.py::test_client_does_not_import_core`, already failing).
  Needs a rewrite, not a rename — tracked separately. 38 `zarr_root` mentions
  inside will die with the file.

## Shape of the change

Split into four PRs, ordered by blast radius. Each PR is green-on-its-own;
later PRs depend on earlier ones landing but not on specific line numbers.

---

### PR 3 — Core library API rename [DONE]

**Scope:** `voxhub-core` library modules + the developer-facing `voxhub-core/cli.py`.

**Critical files:**

- `packages/voxhub-core/src/voxhub_core/staging.py`
  - `stage(zarr_root: str | Path, ...)` → `stage(stores_dir: str | Path, ...)`
  - Rename the `zarr_root = Path(...)` local and update usages (discover call,
    error messages: `'Zarr root directory not found'` →
    `'Stores directory not found'`, `'No .zarr stores found under ...'`
    message text unchanged except variable interpolation).
  - Docstring parameter description.

- `packages/voxhub-core/src/voxhub_core/integrate.py`
  - `integrate(staging_dir, zarr_root, ...)` →
    `integrate(staging_dir, stores_dir, ...)`
  - Local variable + `zarr_path = stores_dir / f'{store_name}.zarr'` update.
  - Docstring.

- `packages/voxhub-core/src/voxhub_core/audit.py`
  - `audit(zarr_root, ...)` → `audit(stores_dir, ...)`.
  - Local + discover call.
  - Docstring.

- `packages/voxhub-core/src/voxhub_core/server/provenance.py`
  - `record_provenance(zarr_root: Path, ...)` →
    `record_provenance(stores_dir: Path, ...)`.
  - Body: `zarr_path = stores_dir / f'{store_name}.zarr'`,
    `meta_dir = stores_dir / '.meta'`.
  - Module docstring (`"...at the zarr root"` → `"...at the stores
    directory root"`).
  - Update the three existing call sites in `server/cli.py`
    (`_run_integrate_annotations`) — they already have a `stores_dir`
    local, so just the keyword name changes.

- `packages/voxhub-core/src/voxhub_core/cli.py` (developer/local CLI, not the
  SSH-invoked server CLI)
  - Argparse dests: `zarr_root` → `stores_dir` on the stage, integrate,
    audit subparsers (positional args).
  - Handler bodies: `args.zarr_root` → `args.stores_dir`.

- `packages/voxhub-core/src/voxhub_core/attributes.py`
  - One docstring line: `"stored in the zarr root group"` → keep wording
    but clarify: `"stored in each zarr store's root group"` (this refers
    to a zarr *group*, not the stores directory — the confusion here is
    exactly what motivated the original rename).

- `packages/voxhub-core/README.md`
  - Two references: `` `zarr_root` `` in the CLI arg list (L112) and
    `` `<zarr_root>/.meta/provenance.jsonl` `` (L121). Replace with
    `stores_dir` / `<stores_dir>` respectively.

**Test ripple within PR 3:**

- `packages/voxhub-core/tests/test_staging.py`,
  `test_integrate.py`, `test_audit.py`, `test_server_provenance.py`,
  `test_staging_e2e.py`: all call the renamed functions with `zarr_root=`
  kwargs or positionally. Mechanical find/replace for the kwarg form. Positional
  callers are unaffected.
- `tests/test_workflow.py`: calls `stage(zarr_root, ...)` positionally; no
  change needed. But see PR 4 for the `zarr_root` fixture rename that would
  flow through.

**Verification:**

- `uv run ruff check packages/`, `uv run pyright`, `uv run pytest packages/`
  all green.
- Grep `packages/voxhub-core/src` for `zarr_root` → should match only
  `discover_zarr_stores` calls (keep) and `zarr_path` (keep).

**Estimated diff:** ~50 lines across 5 source files, ~10 test files touched
via kwarg-rename (mostly single-line edits).

---

### PR 4 — Test infrastructure rename [DONE]

**Scope:** rename every test-only identifier that uses `zarr_root`. No source
changes.

**Critical files:**

- `packages/voxhub-core/tests/conftest.py`
  - Fixture `zarr_root_factory` → `stores_dir_factory`. Update the fixture's
    docstring (`"Build a stores/ directory"` — already accurate wording,
    just confirm).
  - `concurrent_integrate_runner._run()`: invocation-dict key `'zarr_root'`
    → `'stores_dir'`. The fixture currently reads `inv['zarr_root']` to
    write a per-invocation TOML; rename the key and the lookup together.

- `packages/voxhub-core/tests/test_staging_e2e.py`
  - Helper `_stage(zarr_root: Path, ...)` → `_stage(stores_dir: Path, ...)`.
  - Test functions `test_missing_zarr_root_raises`,
    `test_empty_zarr_root_raises` → `test_missing_stores_dir_raises`,
    `test_empty_stores_dir_raises`.

- `packages/voxhub-core/tests/test_server_cli.py`
  - Test function `test_nonexistent_zarr_root` →
    `test_nonexistent_stores_dir`.
  - `test_store_and_provenance_checks_skipped_when_zarr_root_fails` →
    `test_store_and_provenance_checks_skipped_when_stores_dir_fails`.

- `tests/test_workflow.py`
  - Fixture `def zarr_root(tmp_path)` → `def stores_dir(tmp_path)`.
  - Path segment inside the fixture `tmp_path / 'zarr_root'` → `tmp_path /
    'stores'` (cosmetic; avoids a directory named `zarr_root` under the
    fixture's tmp path).
  - All test method parameters `zarr_root` → `stores_dir` (~40 usages).

- Every test file that consumes `zarr_root_factory` as a fixture parameter
  needs to be updated to take `stores_dir_factory`. That's a mechanical
  find-replace across:
  - `test_catalog.py` (~21 tests)
  - `test_audit.py` (~22 tests)
  - `test_server_provenance.py` (~13 tests)
  - `test_server_cli.py` (~40 tests)
  - `test_concurrency.py` (~10 tests — also drops the `'zarr_root'` dict key
    inside each `concurrent_integrate_runner` invocation)
  - `test_server_cli_subprocess.py` (~8 tests)
  - `test_integrate.py`, `test_staging_e2e.py` where applicable

- Local variable renames (cosmetic but worth doing in the same PR for
  consistency): inside test bodies, `zarr_root = zarr_root_factory(...)` →
  `stores_dir = stores_dir_factory(...)`. The variable flows through
  assertions and path constructions across the whole test file.

**Verification:**

- `uv run pytest packages/ tests/` green — this is the authoritative check.
  Tests are the only consumer.
- Grep the `tests/` trees for `zarr_root` → should match zero (or just
  internal variables if we choose to leave some body-local renames out —
  but the proposal here is to do them all).

**Estimated diff:** ~400 lines across ~12 test files. Mostly one-line kwarg
or parameter name changes. Tedious but mechanical and verifiable via the
test suite.

---

### PR 5 — Deploy scripts rename [DONE]

**Scope:** operator-facing installer flags and shell variables.

Per the earlier "no backward compat" direction, breaking operator muscle
memory is acceptable. New flag is clearer.

**Critical files:**

- `scripts/deploy/deploy.sh`
  - CLI flag `--zarr-root` → `--stores-dir` (and usage string, help block,
    argument parser, required-check error message).
  - Shell variable `$ZARR_ROOT` → `$STORES_DIR` throughout (~15 occurrences).
  - Step banner `"Prepare zarr root directory"` → `"Prepare stores
    directory"`.
  - Success message `"Zarr root ready at $STORES_DIR"` → `"Stores
    directory ready at $STORES_DIR"`.
  - Healthcheck invocation `voxhub-server healthcheck $ZARR_ROOT` → just
    `voxhub-server healthcheck` (the server reads stores_dir from its TOML
    config now; passing a positional would fail argparse).
  - Final summary line `"Zarr root:     $STORES_DIR"` → `"Stores dir:
    $STORES_DIR"`.
  - User-facing hint at the end: `voxhub pull $VOXHUB_USER@<server-ip>:$ZARR_ROOT
    ./local_staging` → `voxhub pull $VOXHUB_USER@<server-ip> ./local_staging`
    (target shape change too — this is stale from PR 2).

- `scripts/deploy/healthcheck.sh`
  - Same flag rename (`--zarr-root` → `--stores-dir`).
  - `$ZARR_ROOT` → `$STORES_DIR`.
  - Same server CLI call fix: drop the positional from the healthcheck
    invocation; the server reads from its config.
  - Error message `"Zarr root not found: $STORES_DIR"` → `"Stores directory
    not found: $STORES_DIR"`.

- `docs/deployment-readiness.md`
  - `$ZARR_ROOT/.meta/provenance.jsonl` / `$ZARR_ROOT/*/annotations/` →
    `$STORES_DIR/...`.
  - `--zarr-root` in the go-live checklist (step 5) → `--stores-dir`.
  - "attached volume for `$ZARR_ROOT`" → "attached volume for `$STORES_DIR`".

**Verification:**

- `bash -n scripts/deploy/deploy.sh scripts/deploy/healthcheck.sh` parses.
- Optional: `--dry-run` smoke test on `deploy.sh` with the new flag.
- Grep `scripts/deploy/` for `zarr-root|ZARR_ROOT|zarr_root` → zero hits
  (except inside `voxhub-forced-command.sh`'s comment block which already
  only references the TOML config).

**Estimated diff:** ~60 lines across 3 files.

---

### PR 6 — Documentation prose cleanup [DONE]

**Scope:** lingering narrative mentions of `zarr_root` in testing and general
docs. Not load-bearing; a cleanup pass.

**Critical files:**

- `docs/testing/README.md`
  - `zarr_root_factory` reference → `stores_dir_factory` (matches PR 4
    rename).
  - `create_zarr_root(root_path, store_specs)` — check if this helper still
    exists and rename if so, otherwise drop the stale mention.

- `docs/testing/server-cli.md`
  - `zarr_root_factory(store_count=1, ...)` → `stores_dir_factory(...)`.
  - `staging_dir_with_manifest(zarr_root, store_names, ...)` — the helper
    itself takes `store_names`, not `zarr_root`; this doc line is stale
    and should be corrected.

- `docs/testing/catalog-staging-audit.md`
  - `zarr_root_factory` reference.
  - Two `"stores in the zarr_root are staged"` lines (L108, L171) → "stores
    in the stores directory are staged".
  - `test_missing_zarr_root_raises` reference → `test_missing_stores_dir_raises`
    (matches PR 4 rename).

- `docs/testing/concurrency-and-provenance.md`
  - `"stores under the same zarr_root"` → `"stores under the same
    stores_dir"`.

**Verification:**

- `grep -r 'zarr_root\|zarr-root\|ZARR_ROOT' docs/` → matches only
  `docs/plans/store-basedir-refactor.md` (historical, leave alone) and
  `docs/plans/zarr-root-internal-rename.md` (this document — same
  justification).

**Estimated diff:** ~15 lines across 4 markdown files.

---

## PR sequencing

- **PR 3 (core API)** first. It changes function signatures, so the kwarg-form
  test calls need to update alongside. Bundle those minimal test edits into
  PR 3 — don't try to split the signature change from its callers.
- **PR 4 (test infra)** second. Fixture rename cascades to ~50 test
  functions; self-contained and verifiable by running the test suite.
- **PR 5 (deploy)** and **PR 6 (docs)** can land in either order, and either
  before or after PR 4. They're independent of PRs 3 and 4 content-wise.
  Recommended: PR 5 before PR 6 so PR 6 can reference the already-renamed
  deploy flags in its prose.

If perfect consistency is wanted in a single atomic commit instead of a
split, collapse PRs 3–6. The verification (`pytest`, `pyright`, `ruff`)
works the same either way; the split is only for reviewability.

## Out of scope

- `packages/voxhub-client/src/voxhub_client/cli.py` — stale duplicate,
  needs rewrite. Tracked by the already-failing
  `test_client_does_not_import_core` arch test.
- `docs/plans/store-basedir-refactor.md` — historical planning doc.
- `zarr_path`, `discover_zarr_stores`, `.zarr` suffix, and every other
  reference to zarr-the-library. The `zarr_root` → `stores_dir` rename
  was always specifically about the *directory containing zarr stores*,
  not about zarr stores themselves.

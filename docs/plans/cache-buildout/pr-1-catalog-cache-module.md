# PR 1 — `catalog_cache.py` module

## Scope

Introduce a new module in `voxhub-core` that owns the on-disk cache file
and its lifecycle: reading, fingerprinting, full rebuild, per-store
invalidation, and atomic writes. **No wiring into the server CLI in this
PR** — that happens in PR 2. This PR stands alone behind its own unit tests
so the logic is validated in isolation before any caller changes.

## Critical files

### New: `packages/voxhub-core/src/voxhub_core/server/catalog_cache.py`

Module responsibilities:

- Know the on-disk layout (`<stores_dir>/.meta/catalog.json`,
  `<stores_dir>/.meta/catalog.json.lock`).
- Build the payload for a single store by reusing the existing probe
  (`catalog._probe_zarr`) and the spatial-metadata extraction pulled out of
  `server/cli.py:_run_list_stores` — see "Refactor cue" below.
- Detect staleness via a cheap directory fingerprint.
- Guarantee atomic rewrites and never serve a torn file.

Proposed public surface:

```python
CATALOG_FILENAME = 'catalog.json'
CATALOG_LOCKFILENAME = 'catalog.json.lock'
DEFAULT_TTL_S = 60.0

@attrs.define
class CatalogSnapshot:
    protocol_version: str
    catalog_version: int
    built_at: str            # ISO-8601 UTC
    stores_dir_fingerprint: str
    stores: dict[str, dict]  # store_name -> list-stores payload dict

def read_catalog(
    stores_dir: Path,
    *,
    ttl_s: float = DEFAULT_TTL_S,
) -> CatalogSnapshot:
    """Return the current catalog, rebuilding if missing / stale / fingerprint-mismatched.

    Fast path (warm & fresh): open JSON, parse, compare fingerprint & age,
    return. One file open, one stat-walk.

    Slow path: acquire the writer lock, rebuild, atomic-replace, return.
    """

def rebuild(stores_dir: Path) -> CatalogSnapshot:
    """Full walk + probe of every *.zarr under stores_dir. Bumps catalog_version.
    Atomic write. Takes the writer lock only around the rename."""

def invalidate_store(stores_dir: Path, store_name: str) -> CatalogSnapshot:
    """Re-probe one store and splice it into the catalog. Atomic rewrite.
    If the cache does not exist yet, degrades to a full rebuild."""

def fingerprint(stores_dir: Path) -> str:
    """SHA-1 of sorted (name, mtime_ns, size) tuples for every *.zarr
    directory directly under stores_dir. Cheap stat-walk, no zarr opens."""
```

Internal helpers:

- `_probe_store_payload(zarr_path: Path) -> dict` — mirrors the per-store
  dict built in `server/cli.py:107-171` (see refactor cue). Returns the
  exact shape currently sent on the wire.
- `_load_catalog_file(path: Path) -> CatalogSnapshot | None` — returns
  `None` on missing file, parse failure, or protocol-version mismatch.
- `_atomic_write(path: Path, snapshot: CatalogSnapshot) -> None` — write to
  `<path>.tmp`, `os.replace`.
- `_next_catalog_version(previous: CatalogSnapshot | None) -> int` — starts
  at 1 on first build, `previous.catalog_version + 1` otherwise. Monotonic
  across rebuilds.

Locking model:

- `filelock.FileLock(.meta/catalog.json.lock)` — same pattern as
  `server/locks.py:store_lock`. Timeout 30 s, surface as `CacheLockError`.
- Readers **do not** hold the lock. They open, parse, and validate
  atomically relying on `os.replace`. If parse fails, they retake the lock
  and rebuild.
- `rebuild()` runs the probe walk **outside** the lock and only takes the
  lock for the final `os.replace`. This keeps the lock held for
  milliseconds even on a 100-store rebuild.

### Refactor cue: extract the per-store payload builder

`server/cli.py:107-171` currently builds the per-store dict inline. Move
this into a module-level function in `catalog_cache.py` (or, if we want to
avoid cross-package coupling, into `voxhub_core/catalog.py` and import from
there). The existing `_run_list_stores` in this PR is not yet touched —
but the function it will call in PR 2 should already exist and be unit
tested here.

Signature:

```python
def build_store_entry(
    entry: ZarrEntry,
    zarr_path: Path,
) -> dict[str, Any]:
    """Build the single-store payload dict as currently emitted by list-stores.

    Handles the three branches in _run_list_stores:
      - discovery error → {'error': ..., minimal fields}
      - missing spatial metadata → {'error': 'Missing spatial metadata', ...}
      - ok → full spatial payload including dataset_attributes
    """
```

Note the N+1 opens in today's code — `ZarrEntry` already carries
`entry.shape`, `entry.dtype`, `entry.annotations`, and `entry.attributes`
(see `catalog.py:92-111`). `build_store_entry` should read spatial
metadata out of `entry.attributes` directly and only fall back to
`zarr.open_group(zarr_path, mode='r')` if `entry.attributes` is empty —
collapsing today's double-open into a single probe. This is a correctness-
preserving simplification; note it in the PR description.

## Tests

New file: `packages/voxhub-core/tests/test_catalog_cache.py`. Use the
existing `zarr_root_factory`-style fixtures (see
`packages/voxhub-core/tests/conftest.py`) to build a real stores dir with
2–3 zarr stores.

Minimum coverage:

1. **Cold build**: no cache file → `read_catalog` produces one, file
   appears on disk, `catalog_version == 1`, every store from the fixture
   shows up.
2. **Warm hit within TTL**: call twice in quick succession → second call
   does not rebuild (assert via monkeypatched probe counter or by checking
   `built_at` is unchanged).
3. **TTL expiry with no on-disk changes**: advance clock past TTL → `built_at`
   refreshes but `catalog_version` does **not** bump (fingerprint matches).
4. **TTL expiry with on-disk changes**: touch a store directory (add a
   file) → rebuild triggers, `catalog_version` bumps.
5. **`rebuild()` bumps version monotonically** across repeated calls.
6. **`invalidate_store`** on a specific store updates only that store's
   entry and bumps `catalog_version`. Other stores' payloads are bytewise
   unchanged (no spurious diffs from re-probing).
7. **`invalidate_store` on missing cache** degrades to full rebuild.
8. **Corrupt cache file** (truncated JSON, wrong protocol_version) → next
   read rebuilds and does not raise.
9. **Atomic write**: simulate mid-write crash by asserting `.tmp` files
   never survive a successful write and readers never observe a torn file
   (light-weight: just check post-conditions after each public call).
10. **Concurrent invalidate + read**: spawn two threads, one calling
    `invalidate_store` in a loop, another calling `read_catalog` — assert
    no exceptions and every read returns a well-formed snapshot.
11. **Fingerprint stability**: rebuilding without any on-disk change
    yields an identical `stores_dir_fingerprint`.
12. **Fingerprint sensitivity**: adding a new `*.zarr` directory changes
    the fingerprint; removing one changes it; touching a file inside an
    existing store changes its mtime and changes the fingerprint.
13. **Store-entry parity**: the dict produced by `build_store_entry`
    matches the dict produced today by `_run_list_stores` inline for a
    fixture store. Use a golden comparison. (This is the load-bearing
    test for the payload extraction refactor.)

## Verification gate

- `uv run pytest packages/voxhub-core/tests/test_catalog_cache.py`
- `uv run ruff check packages/voxhub-core`
- `uv run pyright packages/voxhub-core`
- Existing `test_server_cli.py::test_list_stores*` tests still pass
  (nothing is wired yet, so this should be trivially true).

## Non-goals for this PR

- Wiring `_run_list_stores` to use the cache (PR 2).
- Invalidation hooks inside `_run_integrate_annotations` (PR 3).
- Admin CLI (PR 4).
- Client-side anything (PR 5).

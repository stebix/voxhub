# PR 2 — Wire `list-stores` to the catalog cache

## Scope

Replace the inline walk-and-probe in `_run_list_stores` with a single call
to `catalog_cache.read_catalog`. Surface `catalog_version` in the response.
TTL-plus-fingerprint governs staleness on the read path. Write-through
invalidation from `integrate-annotations` lands separately in PR 3 — until
that ships, the TTL is the only correctness mechanism, which is still fine
because every server-side write already goes through the same process and
the TTL is bounded.

Depends on: PR 1 (`catalog_cache.py` must exist).

## Critical files

### `packages/voxhub-core/src/voxhub_core/server/cli.py`

Rewrite `_run_list_stores` (currently lines 97–185):

```python
def _run_list_stores(args: argparse.Namespace) -> None:
    log = get_logger(command='list-stores')
    t0 = time.monotonic()

    stores_dir = Path(args.stores_dir)
    log.info('list_stores_started', stores_dir=str(stores_dir))

    snapshot = catalog_cache.read_catalog(stores_dir)

    duration = time.monotonic() - t0
    log.info(
        'list_stores_completed',
        store_count=len(snapshot.stores),
        catalog_version=snapshot.catalog_version,
        cache_age_s=round(_age_seconds(snapshot.built_at), 3),
        duration_s=round(duration, 3),
    )

    _write_dict({
        'protocol_version': PROTOCOL_VERSION,
        'catalog_version': snapshot.catalog_version,
        'stores': list(snapshot.stores.values()),
    })
```

Remove: the `discover_zarr_stores` call, the per-entry `zarr.open_group`
on line 136, the `extract_spatial_metadata` try-block, the three
per-branch dict assemblies. All of that now lives in
`catalog_cache.build_store_entry`.

Add a tiny `_age_seconds(iso: str) -> float` helper (or put it in
`catalog_cache` — caller's choice) for the log line. Not required, but
gives operators a quick sanity signal.

### Imports to add

```python
from voxhub_core.server import catalog_cache
```

### Imports to remove (if no longer referenced)

- `from voxhub_core.catalog import discover_zarr_stores` — still used by
  `_run_validate_attributes` (line 687) and `_check_stores` (line 795),
  so keep. Do not remove.
- `from voxhub_core.staging import extract_spatial_metadata, stage` —
  `stage` is still used by `_run_prepare_pull`; `extract_spatial_metadata`
  may become unused in this file — remove if so. Ruff will flag it.

### `packages/voxhub-schema`

No changes. `catalog_version` is an additive top-level field on the
`list-stores` response; the existing client code is lenient to extra
fields (it pulls `'stores'` by key, not by schema). Protocol version does
not bump.

## TTL configuration

- Default TTL hard-coded to 60 s in `catalog_cache.DEFAULT_TTL_S` (set in
  PR 1).
- No config-file knob in this PR. If operators ask for one later, add
  `[cache].ttl_seconds` to `server.toml` — simple addition to
  `server/settings.py`. Not needed now.

## Tests

### `packages/voxhub-core/tests/test_server_cli.py`

Existing `list_stores` tests continue to pass unchanged — payload shape is
preserved. Add:

1. **`catalog_version` is emitted** in the JSON response.
2. **Repeated calls within TTL** do not re-walk the filesystem — assert by
   patching `catalog._probe_zarr` with a counter (or by asserting the
   returned `catalog_version` is unchanged across two rapid calls when
   fingerprint is unchanged).
3. **Cache file is created on first call** at `stores_dir/.meta/catalog.json`.
4. **Out-of-band store addition + TTL elapse** triggers a rebuild and a
   bumped `catalog_version`. Easiest shape: `freezegun` or inject a
   monotonic-clock via `catalog_cache`'s public API (see PR 1 — the TTL
   check should be testable without real sleep).
5. **Corrupt cache file** (write garbage to `.meta/catalog.json`, then
   call `list-stores`) → returns a healthy response; cache is rebuilt.

### `packages/voxhub-client/tests/test_ssh.py`

Update fixtures that stub `list-stores` responses to include
`catalog_version` (or confirm the client silently ignores it — tests at
`test_ssh.py:104-148` use a stub runner; a stable payload shape is what
matters). Adjust assertions if any currently match the response dict
exactly.

## Verification gate

- `uv run pytest packages/voxhub-core packages/voxhub-client`
- `uv run ruff check packages/`
- `uv run pyright`
- Manual smoke: spin up a stores dir with 3 fixture zarrs, call
  `voxhub-server list-stores` twice, confirm the second call produces
  identical output and does not probe (watch stderr log lines).

## Risk notes

- **Hot-path error surface changes**: today, a single corrupt zarr in the
  stores dir produces an entry with `'error': ...` and the rest continue.
  `catalog_cache.rebuild` must preserve this per-store error isolation —
  verified by the parity test in PR 1 and by the existing
  `test_list_stores_with_error` coverage.
- **First-ever call on a populated stores dir** pays the full rebuild cost
  plus a cache write. No worse than today; just shifted by microseconds.
- **Stale cache window**: between an out-of-band write and the next TTL
  expiry, `list-stores` returns stale data. Bounded to ≤ TTL. Operators
  who need immediacy use `voxhub-server catalog refresh` (PR 4).

# PR 3 — Write-through invalidation on `integrate-annotations`

## Scope

Wire `catalog_cache.invalidate_store` into the single server-side mutation
path. After this ships, an annotator's push becomes immediately visible on
their (and every other annotator's) next `list-stores` call — no TTL wait,
no stale window for in-band changes.

Depends on: PRs 1 and 2.

## The one and only write path

`_run_integrate_annotations` in
`packages/voxhub-core/src/voxhub_core/server/cli.py:327-607`. Every other
command either reads (`list-stores`, `validate-attributes`, `healthcheck`)
or mutates only the staging dir (`prepare-pull`, `cleanup`, `gc`). There
are no other places that write into `stores_dir/<store>.zarr` server-side.

Confirm this invariant by grepping before implementing:

```
rg -n 'stores_dir.*\.zarr' packages/voxhub-core/src
rg -n 'zarr\.open_group.*mode=.w.' packages/voxhub-core/src
rg -n 'write_segmentation_to_zarr|write_landmarks_to_zarr' packages/voxhub-core/src
```

If anything outside `_run_integrate_annotations` writes into a store, that
call site needs the same hook.

## Critical files

### `packages/voxhub-core/src/voxhub_core/server/cli.py`

Hook point: end of the per-store loop in `_run_integrate_annotations`,
**outside** the `with store_lock(zarr_path):` block but **inside** the
`for store_dir in sorted(staging_dir.iterdir())` loop, right before the
`stores_result[store_name] = ...` assignment (currently around line 589).

Shape:

```python
# … existing annotation write + provenance record happens inside store_lock …

if annotations_written:
    try:
        catalog_cache.invalidate_store(stores_dir, store_name)
    except Exception as exc:
        log.warning(
            'catalog_invalidate_failed',
            store=store_name,
            error=str(exc),
        )
        # Do not fail the integrate — the TTL path will catch up.

stores_result[store_name] = {
    'status': ('integrated' if annotations_written else 'failed'),
    'annotations': annotations_written,
    'issues': [...],
}
```

Rationale for invalidating **outside** `store_lock`:

- `store_lock` is a per-store lock guarding the zarr write. Holding it
  while acquiring the catalog lock risks a lock-order problem if any
  future caller inverts the order.
- The zarr write has already committed by the time we release
  `store_lock` — invalidation only needs to reflect that committed state.
- If the catalog invalidation fails (disk full, lock timeout), we do not
  want to fail the push. The TTL + fingerprint machinery from PR 2 will
  reconcile on the next read.

Rationale for invalidating **per-store** rather than once at the end of
the command:

- A single `integrate-annotations` call can touch N stores. Doing N
  cheap per-store invalidations keeps each lock window tiny.
- If the process crashes mid-command after touching M < N stores, the M
  already-invalidated entries are correct and the remaining N-M will be
  picked up by TTL — best-effort partial progress.

### Optional: early return if nothing was written

If `annotations_written` is empty (all ontologies errored, force off,
etc.), skip the invalidate call. The store wasn't actually modified.
Already handled by the `if annotations_written:` guard above.

## Tests

### `packages/voxhub-core/tests/test_server_cli.py`

Add to the `integrate-annotations` test class:

1. **Successful integrate bumps `catalog_version`**: seed a catalog via
   `list-stores`, run `integrate-annotations`, call `list-stores` again
   — `catalog_version` is strictly greater, and the newly-written
   annotation appears in `stores[<name>].annotations` **without** waiting
   for any TTL.
2. **Integrate that writes nothing leaves `catalog_version` untouched**
   (e.g. validation errors with `force=False`).
3. **Multi-store integrate** bumps `catalog_version` by N (one per store
   touched); every touched store's annotation list is current.
4. **Invalidate failure is non-fatal**: monkey-patch
   `catalog_cache.invalidate_store` to raise; the integrate still
   completes and returns `integrated` status; a `catalog_invalidate_failed`
   log line is emitted.

### `packages/voxhub-core/tests/test_concurrency.py`

The concurrency harness at `test_concurrency.py:233+` already exercises
multi-annotator parallel integrates against a shared stores dir. Add a
step that calls `list-stores` after the parallel pushes settle and
asserts every integrated annotation appears exactly once. The existing
`store_lock` gives us write serialisation per store; the catalog lock
gives us cache-write serialisation across stores. Assert final
`catalog_version == initial_version + total_annotations_integrated`.

## Verification gate

- `uv run pytest packages/voxhub-core` (full suite — concurrency tests
  are the riskiest addition)
- `uv run ruff check packages/`
- `uv run pyright`
- Manual: end-to-end pull-annotate-push cycle with a real client,
  confirm the fresh annotation is visible in the very next
  `list-stores` response.

## Risk notes

- **Lock contention under burst pushes**: if ten annotators push
  simultaneously, all ten integrates serialise on the catalog lock for
  the JSON-rewrite window (milliseconds per store). Acceptable at the
  2–5 concurrent annotator target; revisit if we grow past that.
- **Catalog file growth**: each store's payload is small (< 1 KB for
  typical metadata). 1000 stores × 1 KB = ~1 MB JSON. Still fine for a
  single-file cache. If we exceed that, consider sharding — not now.
- **Hook added in the wrong place** (inside `store_lock` instead of
  outside) would work functionally but invert the lock order. The test
  above only verifies behaviour, not lock ordering — reviewers should
  call out the placement in the PR description.

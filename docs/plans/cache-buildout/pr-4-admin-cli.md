# PR 4 — Admin `voxhub-server catalog` subcommand

## Scope

Give operators an explicit handle on the cache. Needed for two scenarios:

1. **Out-of-band mutation**: the ops team `rsync`-ed a new `*.zarr` store
   directly into `stores_dir`, or hand-edited a store's metadata. Until
   the next TTL elapse, `list-stores` returns stale results. A manual
   refresh eliminates the gap.
2. **Debugging stale reads**: inspect the cache file's age, fingerprint,
   and store count without having to `cat` the JSON by hand.

Depends on: PRs 1 and 2. Independent of PR 3 (works with or without the
write-through hook).

## Command surface

```
voxhub-server catalog refresh            # full rebuild
voxhub-server catalog refresh --store N  # single store re-probe
voxhub-server catalog show               # pretty-print current snapshot
voxhub-server catalog stats              # age, fingerprint match, counts
```

All four emit a single JSON object to stdout, same convention as every
other `voxhub-server` subcommand. Operators typically invoke them via
SSH — the ops helper scripts can wrap with `jq` for humans.

## Critical files

### `packages/voxhub-core/src/voxhub_core/server/cli.py`

Add a `catalog` subparser with nested actions. The pattern in `main()`
already uses `subparsers` cleanly; extend with a second level:

```python
cat = subparsers.add_parser('catalog')
cat_sub = cat.add_subparsers(dest='catalog_action')

cat_refresh = cat_sub.add_parser('refresh')
cat_refresh.add_argument('--store', default=None)
cat_refresh.set_defaults(func=_run_catalog_refresh)

cat_show = cat_sub.add_parser('show')
cat_show.set_defaults(func=_run_catalog_show)

cat_stats = cat_sub.add_parser('stats')
cat_stats.set_defaults(func=_run_catalog_stats)
```

New handlers in the same file:

```python
def _run_catalog_refresh(args: argparse.Namespace) -> None:
    log = get_logger(command='catalog-refresh')
    stores_dir = Path(args.stores_dir)
    if args.store:
        snapshot = catalog_cache.invalidate_store(stores_dir, args.store)
        log.info('catalog_store_refreshed', store=args.store,
                 catalog_version=snapshot.catalog_version)
    else:
        snapshot = catalog_cache.rebuild(stores_dir)
        log.info('catalog_rebuilt',
                 store_count=len(snapshot.stores),
                 catalog_version=snapshot.catalog_version)
    _write_dict({
        'protocol_version': PROTOCOL_VERSION,
        'catalog_version': snapshot.catalog_version,
        'store_count': len(snapshot.stores),
        'built_at': snapshot.built_at,
    })

def _run_catalog_show(args: argparse.Namespace) -> None:
    snapshot = catalog_cache.read_catalog(Path(args.stores_dir))
    _write_dict({
        'protocol_version': PROTOCOL_VERSION,
        'catalog_version': snapshot.catalog_version,
        'built_at': snapshot.built_at,
        'stores_dir_fingerprint': snapshot.stores_dir_fingerprint,
        'stores': list(snapshot.stores.values()),
    })

def _run_catalog_stats(args: argparse.Namespace) -> None:
    stores_dir = Path(args.stores_dir)
    # Read without triggering a rebuild — call a separate peek helper.
    stats = catalog_cache.peek_stats(stores_dir)
    _write_dict({
        'protocol_version': PROTOCOL_VERSION,
        **stats,  # catalog_version, built_at, age_s, fingerprint_match,
                  # store_count, cache_file_size_bytes
    })
```

### `packages/voxhub-core/src/voxhub_core/server/catalog_cache.py`

Add one more public helper:

```python
def peek_stats(stores_dir: Path) -> dict[str, Any]:
    """Read cache metadata WITHOUT triggering a rebuild.

    Returns age, fingerprint match (comparing cached fingerprint to the
    live one), store count, and cache file size. Safe to call when the
    cache is stale or even corrupt (surfaces status='corrupt' in that
    case).
    """
```

This is a diagnostic tool; it must not mutate state. If the cache file
is missing it returns `status='missing'`; if corrupt, `status='corrupt'`;
otherwise `status='ok'` with the detail fields.

### Missing-subcommand UX

`voxhub-server catalog` with no action → print help for `catalog`, exit 0.
Mirrors the top-level behaviour (`server/cli.py:941-943`).

## Documentation

Add a section to `packages/voxhub-core/README.md` (ops section) covering:

- When to run `catalog refresh` (after manual `rsync`, manual store
  deletions, or any out-of-band edit to a `*.zarr` directory).
- What `catalog stats` output means.
- Note that the TTL-based refresh on read is automatic — manual refresh
  is only required for immediacy.

Short paragraph plus a two-line code block. Do not over-document — the
command's own `--help` text should carry the weight.

## Tests

### `packages/voxhub-core/tests/test_server_cli.py`

New test class `TestCatalogCommand`:

1. `catalog refresh` with no existing cache builds one and returns
   `store_count == fixture count`.
2. `catalog refresh` on an existing cache bumps `catalog_version`.
3. `catalog refresh --store NAME` only re-probes that store (assert via
   a probe counter; other store entries' `catalog_version`-level fields
   unchanged).
4. `catalog refresh --store NONEXISTENT` returns a structured error with
   `code='store_not_found'` — decide whether `invalidate_store` raises
   or degrades; PR 1 should already have taken a position on this.
5. `catalog show` returns the full snapshot; fields match what
   `list-stores` would return.
6. `catalog stats` reports `fingerprint_match=True` on a fresh build,
   `False` after touching a store directory.
7. `catalog stats` on a missing cache returns `status='missing'` and
   exit 0 (diagnostic, not an error).
8. `catalog stats` on a corrupt cache returns `status='corrupt'` and
   exit 0.
9. `catalog` with no subaction prints help and exits 0.

## Verification gate

- `uv run pytest packages/voxhub-core`
- `uv run ruff check packages/`
- `uv run pyright`
- Manual smoke: on a populated stores dir, run each of the four command
  forms; eyeball output.

## Risk notes

- **`catalog refresh` during a concurrent push**: catalog lock serialises
  them. The refresh sees the committed state of the store at the moment
  its writer-lock `os.replace` lands. No torn state.
- **Operators invoking `catalog show` to diff against `list-stores`**:
  both now produce the same payload; the only intentional difference is
  that `show` also emits `built_at` and `stores_dir_fingerprint`.

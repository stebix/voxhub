# PR 5 — Client-side catalog cache and `--if-version` short-circuit

## Scope

Make the client side cheap-or-free under repeated `list-stores` calls. A
client keeps its last response on disk, tagged with the server's
`catalog_version`. On subsequent calls it tells the server "only send me
new data if version > N". When the catalog hasn't changed, the server
replies with a zero-payload acknowledgement and the client reuses its
cached snapshot.

This is a transport-layer optimisation on top of PR 2 (warm server reads
already take ~1 ms; this PR reduces the bytes over the wire and the JSON
parse cost on repeated filter passes).

Depends on: PR 2 (server emits `catalog_version`). Ideally PR 3 too, so
in-band changes invalidate immediately; otherwise the client experiences
stale reads bounded by the server TTL.

Touches both `voxhub-core` (server response shape) and `voxhub-client`
(cache + flag).

## Protocol addition

Server `list-stores` response grows one optional input flag:

- `voxhub-server list-stores --if-version N`

Behaviour:

- If the current `catalog_version == N`, server replies:
  ```json
  {"protocol_version": "...", "catalog_version": N, "unchanged": true}
  ```
- Otherwise, current full response (with new `catalog_version`).

No `protocol_version` bump — `unchanged` is additive, and clients that
don't send `--if-version` never see `unchanged: true`.

## Critical files

### `packages/voxhub-core/src/voxhub_core/server/cli.py`

Extend the `list-stores` subparser:

```python
ls = subparsers.add_parser('list-stores')
ls.add_argument('--if-version', type=int, default=None)
ls.set_defaults(func=_run_list_stores)
```

Update `_run_list_stores`:

```python
snapshot = catalog_cache.read_catalog(stores_dir)

if args.if_version is not None and args.if_version == snapshot.catalog_version:
    log.info('list_stores_unchanged', catalog_version=snapshot.catalog_version)
    _write_dict({
        'protocol_version': PROTOCOL_VERSION,
        'catalog_version': snapshot.catalog_version,
        'unchanged': True,
    })
    return

# … existing full-response emission …
```

Note the early return uses the exact same `read_catalog` call — we still
need to know the current version before we can short-circuit.

### `packages/voxhub-client/src/voxhub_client/`

Add a new module, `catalog_cache.py`, separate from the server one
(different concerns, different lifetime, and core/client never import
each other per the architecture rule).

Layout:

- Cache file: `~/.cache/voxhub/<server_key>/catalog.json`
- `server_key` = sanitised `user@host[:port]` from the active `SshTarget`,
  so multi-server users get isolated caches.
- Payload: `{ 'catalog_version': N, 'fetched_at': ..., 'stores': [...] }`.

Public API:

```python
@attrs.define
class ClientCatalogCache:
    cache_root: Path

    def read(self, server_key: str) -> dict | None:
        """Return {'catalog_version', 'stores'} or None if no cache / unreadable."""

    def write(self, server_key: str, catalog_version: int, stores: list[dict]) -> None:
        """Atomic write via tmp + replace."""

    def clear(self, server_key: str | None = None) -> None:
        """Delete one server's cache, or all caches."""
```

### `packages/voxhub-client/src/voxhub_client/ssh.py`

Existing `run()` signature stays. The caller (a new helper) orchestrates
the cache dance:

```python
# Somewhere in voxhub_client — probably a new list_stores_cached() helper
# rather than stuffing it inside SshRunner.run().

def list_stores_cached(
    runner: SshRunner,
    cache: ClientCatalogCache,
    server_key: str,
    *,
    force: bool = False,
) -> dict:
    cached = None if force else cache.read(server_key)
    args = ['list-stores']
    if cached is not None:
        args += ['--if-version', str(cached['catalog_version'])]

    resp = runner.run(*args)

    if resp.get('unchanged') and cached is not None:
        return {
            'protocol_version': resp['protocol_version'],
            'catalog_version': resp['catalog_version'],
            'stores': cached['stores'],
        }

    cache.write(server_key, resp['catalog_version'], resp['stores'])
    return resp
```

This helper is what the annotator CLI (introduced in
`packages/voxhub-client/src/voxhub_client/cli.py`, see recent
`feat(client): add server config module and annotator-facing CLI`)
should call. Find the existing `list-stores` call site in the client
CLI and route it through `list_stores_cached`.

### `packages/voxhub-client/src/voxhub_client/cli.py`

Add a `--no-cache` flag to the `list-stores` client subcommand (or
equivalent) that sets `force=True`. Useful for debugging. Mirrors
server-side `catalog refresh` intent.

## Cache-poisoning and correctness boundary

The server's `catalog_version` is the single source of truth. Client
cache entries are always validated against it on every call — stale
client caches can never serve wrong data, only unnecessarily re-fetched
data. Worst case after a client cache corruption: one full re-fetch.

**Do not** let the client cache serve data without a round-trip to the
server. Every `list-stores` call hits the server; the optimisation is
only in the response payload size. This keeps the design simple and
impossible to go wrong in the "server catalog rebuilt but client cached
version stuck at 0" failure mode.

## Tests

### `packages/voxhub-client/tests/test_catalog_cache.py` (new)

1. `read` on missing cache returns `None`.
2. `write` then `read` round-trips the payload.
3. Corrupt cache file → `read` returns `None` (do not raise).
4. Atomic write: `.tmp` file never survives a successful write.
5. Multi-server isolation: writing under `server_key='a'` does not
   affect `server_key='b'`.
6. `clear()` with and without `server_key`.

### `packages/voxhub-client/tests/test_ssh.py`

Extend the stub runner used by `TestSshRunner` (see
`test_ssh.py:104-148`):

1. Client sends `--if-version N` when cache exists; omits the flag when
   cache is empty.
2. Server returns `unchanged: true` → client returns cached stores
   without a re-fetch (i.e. a second call under the same fixture
   produces identical content).
3. Server returns a full payload with a different `catalog_version` →
   client updates its cache.
4. `--no-cache` / `force=True` skips the `--if-version` header.

### `packages/voxhub-core/tests/test_server_cli.py`

1. `list-stores --if-version N` with matching `catalog_version` returns
   `{"unchanged": true, "catalog_version": N}` and no `stores` key.
2. `list-stores --if-version N` with mismatching N returns the full
   payload (assert `stores` is present).
3. `list-stores --if-version` accepts int only; bad value → argparse
   error (standard behaviour, worth one sanity test).

## Verification gate

- `uv run pytest packages/voxhub-core packages/voxhub-client`
- `uv run ruff check packages/`
- `uv run pyright`
- Manual: run `voxhub-client` `list-stores` twice against a live server,
  confirm the second call returns in ≤ SSH handshake time and does not
  re-download the payload (check stderr / debug logs).

## Risk notes

- **Home dir on annotator machines**: `~/.cache/voxhub/` must survive
  across CLI invocations. On Linux this is standard XDG behaviour. On
  macOS, `~/.cache` is non-standard — use `platformdirs` if the client
  is expected to run on non-Linux annotator laptops. Given the target
  user base is Linux-heavy, default to `~/.cache/voxhub/` and leave
  `platformdirs` migration for later if needed.
- **Cache growing unbounded**: worst case, one JSON file per server ever
  contacted. Single-digit kilobytes each. Ignore.
- **Clock skew**: `fetched_at` is informational only; not used for
  validity decisions. No risk.
- **Architecture rule**: `voxhub-client` must not import `voxhub-core`.
  The new client-side `catalog_cache.py` is fully self-contained —
  verify in `test_architecture.py::test_client_does_not_import_core`.

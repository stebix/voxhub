# Catalog cache buildout: make `list-stores` cheap under repeated calls

## Context

`list-stores` is the entry point every annotator hits before every pull.
In practice clients will invoke it many times in a row while narrowing down
which store(s) to pull — filtering by ontology, annotator, shape, etc. The
current implementation (`packages/voxhub-core/src/voxhub_core/server/cli.py:97`
→ `catalog.discover_zarr_stores`) does a full filesystem walk, opens every
`*.zarr` group **twice** (once in `_probe_zarr`, once more in
`_run_list_stores` to extract spatial metadata), and walks the full
`annotations/<annotator>/<instance>/<array>` tree of each store to read attrs.

Per call cost for 100 stores × 10 annotations each:

- ~200 `zarr.open_group` calls (N+1: probe + re-open)
- ~1000 annotation attrs reads
- ~1500+ syscalls server-side
- 1 SSH handshake per invocation

That's linear in `(stores × annotations)`, repeated on every client filter
action. Fine for a handful of stores, painful at target scale
(see `memory/project_deployment_target.md` — Hetzner CX22/CX32, 2–5
concurrent annotators, heterogeneous store catalog).

## Goal

Serve warm `list-stores` calls from an on-disk cache. Keep the cache correct
under the three situations that can mutate store state:

1. **In-band writes** — the server's own `integrate-annotations` path.
   Hook into it; invalidate the touched store atomically.
2. **Out-of-band writes** — operators `rsync`-ing new zarr stores in, or
   fixing metadata by hand. Covered by a cheap fingerprint check on read
   plus a TTL, and by an explicit admin refresh command.
3. **Initial / corrupt state** — no cache, wrong protocol version, or parse
   failure → cold rebuild, same code path as today.

Non-goal: avoid introducing a long-running daemon. The server is
SSH-invoked process-per-call and will stay that way; the cache must
therefore live on disk.

## Key design decisions

- **On-disk cache at `stores_dir/.meta/catalog.json`** alongside the existing
  `.meta/provenance.jsonl`. One JSON file, one `filelock` guard.
- **Atomic rewrites via `tmp + os.replace`** — readers never see a torn file.
- **Three invalidation levers**: write-through hook, TTL + fingerprint on
  read, manual admin CLI. (All three land in separate PRs.)
- **`catalog_version`** is a monotonic counter bumped on every mutation;
  surfaced to the client so the client can short-circuit repeated requests.
- **Cache payload == current `list-stores` payload**, so protocol stays
  stable and we don't touch `voxhub-schema`.

## PR sequencing

Five PRs, ordered by blast radius. Each PR is green on its own; later PRs
depend on earlier ones landing but not on specific line numbers. The first
four all live in `voxhub-core`; the last one is client-only.

| # | File | Scope |
|---|---|---|
| 1 | [pr-1-catalog-cache-module.md](pr-1-catalog-cache-module.md) | Pure `catalog_cache.py` module + unit tests. No wiring. |
| 2 | [pr-2-list-stores-integration.md](pr-2-list-stores-integration.md) | Swap `_run_list_stores` to read from cache; TTL + fingerprint. |
| 3 | [pr-3-write-through-invalidation.md](pr-3-write-through-invalidation.md) | Hook `_run_integrate_annotations` to invalidate on write. |
| 4 | [pr-4-admin-cli.md](pr-4-admin-cli.md) | `voxhub-server catalog refresh\|show\|stats` subcommand. |
| 5 | [pr-5-client-shortcircuit.md](pr-5-client-shortcircuit.md) | Client-side `catalog_version` cache + `--if-version` short-circuit. |

Shipping PRs 1–3 already delivers the bulk of the win (warm reads, in-band
correctness). PR 4 is an ops-quality-of-life addition. PR 5 is a transport
optimisation that turns "warm read" into "zero bytes over the wire" for
unchanged catalogs.

## Invariants the cache must preserve

- The payload returned to the client is byte-for-byte equivalent to today's
  `_run_list_stores` output (modulo the new `catalog_version` field, which
  is additive).
- Readers never see a partially-written cache file.
- Any error reading/parsing the cache falls through to a cold rebuild.
- An operator-forced refresh always takes precedence over the TTL path.
- `catalog_version` is monotonic for the lifetime of the stores directory
  (persist across rebuilds — do not reset to 0 on full rebuild).

## Out of scope

- Replacing process-per-call with a long-running daemon.
- Distributed / multi-host caches.
- Server-side observability for cache hit rate (could land later as a
  `catalog stats` extension).
- Changing the protocol to paginate or stream stores.

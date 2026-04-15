# Catalog cache: follow-up plan

Items deferred from the post-PR-5 review. The `read_catalog` 30-second-stall
bug has already been fixed on this branch; what follows is everything else.

Sorted by impact. Each tier is independently shippable; tiers 1 & 2 are
worth their own PRs, tier 3 can ride along whenever someone next touches
the relevant file.

---

## Tier 1 — correctness (ship next)

### 1.1 Client `list_stores_cached` KeyError on rogue `unchanged: true`

**Where:** `packages/voxhub-client/src/voxhub_client/catalog_cache.py:269-280`

**Bug:** `if response.get('unchanged') and cached is not None` — when the
server sends `unchanged: true` but `cached is None`, the guard fails,
falls through to
`cache.write(..., int(response['catalog_version']), list(response['stores']))`,
KeyErrors on the missing `stores` field. This shouldn't happen if both
sides honour the protocol, but a buggy/old server or a wiped-mid-call
client cache will trip it with a confusing traceback.

**Fix shape:** Branch the response shape explicitly. If `unchanged is True`
and we have no cached payload, transparently retry once with `force=True`:

```python
if response.get('unchanged'):
    if cached is not None:
        return {
            'protocol_version': response.get('protocol_version'),
            'catalog_version': response['catalog_version'],
            'stores': cached['stores'],
        }
    # Server claimed unchanged but we have nothing to splice — refetch.
    return list_stores_cached(runner, cache, server_key, force=True)
```

**Tests:** `test_client_catalog_cache.py` — add
`test_unchanged_response_without_cache_refetches`. Stub runner returns
`unchanged: true` on the first call, full payload on the second; assert
two `runner.run` calls and a populated cache afterwards.

**Risk:** None — the recursion is bounded at one (`force=True` skips the
`--if-version` flag, server cannot return `unchanged`).

---

## Tier 2 — design smells (next quiet PR)

### 2.1 Promote private cache helpers used by the CLI

**Where:** `packages/voxhub-core/src/voxhub_core/server/cli.py:735-737`
calls
`catalog_cache._load_catalog_file(catalog_cache._catalog_paths(stores_dir)[1])`
to implement the `--store NAME` typo guard.

**Fix shape:** Add a public
`try_load(stores_dir: Path) -> CatalogSnapshot | None` to `catalog_cache`
that hides the path tuple. Replace the private call. Keep
`_load_catalog_file` and `_catalog_paths` private — they're internal
layout knowledge.

**Tests:** No new behaviour; existing `TestCatalogCommand` cases cover
the typo guard. Drop a sanity test that `try_load` returns `None` on a
missing/corrupt cache.

---

### 2.2 De-duplicate `_age_seconds`

**Where:** `cli.py:107` vs `catalog_cache.py:163`. Same logic, different
signatures (one uses `time.time()` directly, the other takes `now_func`).

**Fix shape:** Drop the `cli.py` copy. Import
`from voxhub_core.server.catalog_cache import _age_seconds` (or promote
to public — it's pure and harmless). The cli always passes `time.time` so
the `now_func` arg is fine to default.

**Risk:** None. The cli call sites already round to 3 decimals;
behaviour identical.

---

### 2.3 Delete the dead-code fallback in `build_store_entry`

**Where:** `catalog_cache.py:282-287` —
`if not attrs_map: import zarr; root = zarr.open_group(...)`.

**Why dead:** `voxhub_core/catalog.py:102` unconditionally sets
`entry.attributes = dict(arr.attrs)` on the success branch, and the
`entry.error` branch returns earlier. The fallback can only fire if a
future `_probe_zarr` change forgets to populate `attributes` — at which
point we'd rather see a KeyError than silently double the I/O.

**Fix shape:** Delete the fallback block. Drop the inline `import zarr`.
Remove the "defensive; legacy probe results" sentence from the docstring.

**Tests:** Existing parity test
(`test_build_store_entry_matches_inline_payload`) still covers the happy
path. The two existing edge-case tests still cover error and
missing-spatial branches.

**Risk:** Negligible. If the invariant ever breaks, the loud KeyError is
better than a silent re-open.

---

### 2.4 Make client `_sanitise_key` injective

**Where:** `voxhub-client/src/voxhub_client/catalog_cache.py:83-89`.

**Bug:** `[^A-Za-z0-9._-] → '_'` collapses many distinct SSH targets onto
the same cache directory. `user@host:2222` and `user@host_2222` both
become `user_host_2222`. Hetzner-only deploys won't trip this;
multi-host annotators with weird hostnames could.

**Fix shape:** Append a short hash suffix derived from the raw input:

```python
def _sanitise_key(raw: str) -> str:
    cleaned = _SAFE_KEY_RE.sub('_', raw)
    if not cleaned:
        raise ValueError(f'server_key sanitises to empty string: {raw!r}')
    digest = hashlib.blake2b(raw.encode(), digest_size=4).hexdigest()
    return f'{cleaned}-{digest}'
```

**Tests:** `test_client_catalog_cache.py` — add
`test_sanitise_key_distinguishes_collision_pairs`. Two raws that
previously collided must now produce different keys; the same raw must
produce the same key across calls.

**Migration note:** Existing client caches under the old key will be
orphaned (worst case: one wasted refetch per server, then the new key is
used). Acceptable; document in PR description.

---

## Tier 3 — polish (nice-to-have, low ROI)

### 3.1 Swap SHA-1 for `blake2b(digest_size=16)` in `fingerprint`

`catalog_cache.py:140` — semantic change only ("this is a non-crypto
fingerprint"). Slightly faster too. One-line edit; bumps
`CACHE_SCHEMA_VERSION` to 2 because old caches hold SHA-1 fingerprints
that won't match. Old caches degrade to a single rebuild. Worth the bump
only if we touch the schema for another reason — otherwise leave alone.

### 3.2 Promote `peek_stats` return to an attrs class

`catalog_cache.py:453-497` — replace the discriminated dict with
`PeekResult` variants (`PeekMissing`, `PeekCorrupt`, `PeekOk`). Pyright
catches missing-key bugs at the call site. Cost: ~30 lines, modest
serialisation glue in `_run_catalog_stats`. Defer until a second caller
appears — single consumer doesn't justify the type.

### 3.3 Skip the redundant fingerprint walk in `invalidate_store`

`catalog_cache.py:399` re-walks the dir on every per-store invalidation.
Under burst pushes (10 stores × 5 annotators) that's 50 stat-walks
back-to-back. At target scale (≤100 top-level stores) the walk is
sub-millisecond; revisit only if `catalog_invalidate_failed` warnings
start appearing under load.

---

## Suggested sequencing

| PR | Tier | Scope | Why now |
|---|---|---|---|
| A | 1.1 | Client `unchanged` retry | Crash hazard, one-line fix |
| B | 2.1 + 2.2 + 2.3 | Cache module hygiene | Cohesive cleanup, no behaviour change |
| C | 2.4 | Sanitiser injectivity | Standalone, has migration story |
| — | 3.x | — | Park until another reason to touch the file |

PR A is small enough to bundle into the next unrelated client change if
you don't want a dedicated PR for it. PRs B and C are independent and
can ship in either order.

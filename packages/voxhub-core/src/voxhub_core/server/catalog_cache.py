"""On-disk cache for ``list-stores`` payloads.

The server is invoked once per SSH call, so a long-running in-memory
cache isn't an option. Instead we persist the cached payload to
``<stores_dir>/.meta/catalog.json`` and serve repeat calls from there,
guarded by:

- a cheap top-level directory fingerprint (detects added/removed stores),
- a TTL that bounds how long we trust the cache before re-checking the
  fingerprint, and
- explicit per-store invalidation hooked into the write paths (PR 3)
  plus an admin refresh command (PR 4).

The read path is strictly read-only: ``read_catalog`` never acquires the
writer lock and never rewrites the file. When the TTL expires it
re-checks the fingerprint and either returns the existing snapshot
unchanged (fingerprint matches) or triggers a full ``rebuild``
(fingerprint differs). This keeps reads non-blocking even while a
concurrent ``invalidate_store`` holds the writer lock.

Deep edits inside an existing store are intentionally NOT caught by the
fingerprint — the flat ``stores_dir/<name>.zarr`` layout only checks the
top-level directory's ``(name, mtime_ns, size)`` tuple. That's cheap and
covers add/remove; interior writes rely on the write-through hook or the
admin refresh.

Concurrent writers are serialised behind a single ``filelock`` on the
cache file. Readers open and parse without the lock; ``os.replace`` gives
them atomicity.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import attrs
from filelock import FileLock, Timeout

if TYPE_CHECKING:
    from collections.abc import Callable

from voxhub_core.catalog import ZarrEntry, _probe_zarr, discover_zarr_stores
from voxhub_core.staging import extract_spatial_metadata
from voxhub_schema import PROTOCOL_VERSION

CACHE_SCHEMA_VERSION: int = 1
CATALOG_DIRNAME: str = '.meta'
CATALOG_FILENAME: str = 'catalog.json'
CATALOG_LOCKFILENAME: str = 'catalog.json.lock'
DEFAULT_TTL_S: float = 60.0
DEFAULT_LOCK_TIMEOUT_S: float = 30.0


class CacheLockError(RuntimeError):
    """Raised when the catalog cache lock cannot be acquired in time."""


@attrs.define
class CatalogSnapshot:
    """Serialised representation of the on-disk catalog cache.

    Parameters
    ----------
    cache_schema_version
        Version of *this* file's layout. Bumped independently of the wire
        protocol. A mismatch forces a cold rebuild.
    protocol_version
        The wire ``PROTOCOL_VERSION`` at build time. Recorded for
        diagnostics; not used for cache-validity decisions.
    catalog_version
        Monotonic counter bumped on every mutation (rebuild or
        ``invalidate_store``). Clients use this to short-circuit
        repeated requests in PR 5.
    built_at
        ISO-8601 UTC timestamp of the last actual build (``rebuild`` or
        ``invalidate_store``). The read path never updates this field.
    stores_dir_fingerprint
        Hex SHA-1 over sorted ``(name, mtime_ns, size)`` for every
        top-level ``*.zarr`` directory directly under ``stores_dir``.
    stores
        Mapping ``store_name -> list-stores payload dict``. The payload
        shape matches today's ``_run_list_stores`` output.
    """

    cache_schema_version: int
    protocol_version: int
    catalog_version: int
    built_at: str
    stores_dir_fingerprint: str
    stores: dict[str, dict[str, Any]]

    def to_json_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-ready dict."""
        return attrs.asdict(self)

    @classmethod
    def from_json_dict(cls, data: dict[str, Any]) -> CatalogSnapshot:
        """Reconstruct from a parsed JSON dict; raises on malformed input."""
        return cls(
            cache_schema_version=int(data['cache_schema_version']),
            protocol_version=int(data['protocol_version']),
            catalog_version=int(data['catalog_version']),
            built_at=str(data['built_at']),
            stores_dir_fingerprint=str(data['stores_dir_fingerprint']),
            stores=dict(data['stores']),
        )


def _catalog_paths(stores_dir: Path) -> tuple[Path, Path, Path]:
    """Return ``(meta_dir, catalog_path, lock_path)`` for a stores dir."""
    meta_dir = stores_dir / CATALOG_DIRNAME
    return (
        meta_dir,
        meta_dir / CATALOG_FILENAME,
        meta_dir / CATALOG_LOCKFILENAME,
    )


def _catalog_lock(
    stores_dir: Path,
    *,
    timeout: float = DEFAULT_LOCK_TIMEOUT_S,
) -> FileLock:
    """Return a ``FileLock`` on the catalog, creating ``.meta/`` if needed."""
    meta_dir, _, lock_path = _catalog_paths(stores_dir)
    meta_dir.mkdir(parents=True, exist_ok=True)
    return FileLock(str(lock_path), timeout=timeout)


def fingerprint(stores_dir: Path) -> str:
    """Fingerprint every top-level ``*.zarr`` directory under *stores_dir*.

    Returns the hex SHA-1 over sorted ``(name, mtime_ns, size)`` tuples.
    Cheap stat-walk, no zarr opens.

    Only sensitive to add / remove / rename of top-level stores. Interior
    edits (writing into ``raw/full/c/0/…`` or editing ``.zattrs``) do not
    change the result by design; callers must invalidate explicitly for
    those. See module docstring.
    """
    h = hashlib.sha1()
    if not stores_dir.is_dir():
        return h.hexdigest()

    entries: list[tuple[str, int, int]] = []
    for child in stores_dir.iterdir():
        if not child.name.endswith('.zarr'):
            continue
        if not child.is_dir():
            continue
        st = child.stat()
        entries.append((child.name, st.st_mtime_ns, st.st_size))

    for name, mtime_ns, size in sorted(entries):
        h.update(f'{name}\x00{mtime_ns}\x00{size}\n'.encode())
    return h.hexdigest()


def _now_iso(now_func: Callable[[], float]) -> str:
    """Render the current time (from *now_func*) as an ISO-8601 UTC string."""
    return datetime.fromtimestamp(now_func(), tz=UTC).isoformat()


def age_seconds(
    built_at: str,
    *,
    now_func: Callable[[], float] = time.time,
) -> float:
    """Return the age of a ``built_at`` timestamp in seconds.

    An unparseable timestamp is treated as infinitely old so the caller
    triggers a rebuild (or, in diagnostic call sites, surfaces the
    badness via an ``inf`` log line).
    """
    try:
        dt = datetime.fromisoformat(built_at)
    except ValueError:
        return float('inf')
    return max(0.0, now_func() - dt.timestamp())


def _load_catalog_file(catalog_path: Path) -> CatalogSnapshot | None:
    """Read and parse the catalog file, or return ``None`` on any problem.

    Missing file, unreadable bytes, malformed JSON, shape mismatch, or a
    ``cache_schema_version`` mismatch all degrade to ``None`` so callers
    fall through to a cold rebuild.
    """
    try:
        raw = catalog_path.read_bytes()
    except (FileNotFoundError, OSError):
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        snap = CatalogSnapshot.from_json_dict(data)
    except (KeyError, TypeError, ValueError):
        return None
    if snap.cache_schema_version != CACHE_SCHEMA_VERSION:
        return None
    return snap


def try_load(stores_dir: Path) -> CatalogSnapshot | None:
    """Return the current on-disk snapshot, or ``None`` if absent/corrupt.

    Read-only; no lock is acquired and no rewrite is performed. Intended
    for callers that want to consult the cache without triggering a
    rebuild (e.g. the admin ``catalog refresh --store NAME`` typo
    guard). Use ``read_catalog`` for the serving path.
    """
    _, catalog_path, _ = _catalog_paths(stores_dir)
    return _load_catalog_file(catalog_path)


def _atomic_write(catalog_path: Path, snapshot: CatalogSnapshot) -> None:
    """Write *snapshot* to *catalog_path* via ``tmp + os.replace``.

    Readers never observe a torn file: either the old complete file or
    the new complete file. The tmp file is removed on any error path.
    """
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(snapshot.to_json_dict(), ensure_ascii=False)
    fd, tmp_name = tempfile.mkstemp(
        prefix='.' + catalog_path.name + '.',
        suffix='.tmp',
        dir=str(catalog_path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, catalog_path)
    except Exception:
        if tmp_path.exists():
            with contextlib.suppress(OSError):
                tmp_path.unlink()
        raise


def _next_catalog_version(previous: CatalogSnapshot | None) -> int:
    """Return the next ``catalog_version``. Starts at 1, else ``prev + 1``.

    A corrupt or missing on-disk catalog resets the counter to 1. The
    client short-circuit in PR 5 treats a lower server version as a
    forced refetch, so regression is safe — just a missed optimisation.
    """
    if previous is None:
        return 1
    return previous.catalog_version + 1


def build_store_entry(entry: ZarrEntry, zarr_path: Path) -> dict[str, Any]:
    """Build the single-store payload dict as emitted by ``list-stores``.

    Three branches mirror ``server/cli._run_list_stores``:

    * ``entry.error`` set → minimal error payload.
    * spatial metadata missing → ``'error': 'Missing spatial metadata'``.
    * OK → full spatial payload + ``dataset_attributes``.

    Reads spatial metadata straight off ``entry.attributes`` (already
    populated by ``_probe_zarr``) and ``entry.dataset_attributes_raw``,
    avoiding the re-open done by today's inline implementation.
    """
    store_name = zarr_path.name.removesuffix('.zarr')
    annotations = [
        {
            'path': a.path,
            'ontology': a.ontology,
            'ontology_version': a.ontology_version,
            'annotator_id': a.annotator_id,
            'integrated_at': a.integrated_at,
        }
        for a in entry.annotations
    ]

    if entry.error:
        return {
            'name': store_name,
            'shape': [],
            'dtype': '',
            'origin_lps': [],
            'spacing_mm': [],
            'space_directions': [],
            'annotations': [],
            'error': entry.error,
            'dataset_attributes': None,
        }

    attrs_map = entry.attributes
    try:
        origin, space_directions, spacing_mm = extract_spatial_metadata(attrs_map)
    except KeyError:
        return {
            'name': store_name,
            'shape': list(entry.shape or []),
            'dtype': entry.dtype or '',
            'origin_lps': [],
            'spacing_mm': [],
            'space_directions': [],
            'annotations': annotations,
            'error': 'Missing spatial metadata',
            'dataset_attributes': None,
        }

    return {
        'name': store_name,
        'shape': list(entry.shape or []),
        'dtype': entry.dtype or '',
        'origin_lps': origin.tolist(),
        'spacing_mm': spacing_mm,
        'space_directions': space_directions.tolist(),
        'annotations': annotations,
        'error': None,
        'dataset_attributes': entry.dataset_attributes_raw,
    }


def _build_snapshot(
    stores_dir: Path,
    previous: CatalogSnapshot | None,
    *,
    now_func: Callable[[], float],
) -> CatalogSnapshot:
    """Walk *stores_dir*, probe every store, and assemble a fresh snapshot."""
    entries = discover_zarr_stores(stores_dir)
    stores_payload: dict[str, dict[str, Any]] = {}
    for entry in entries:
        key = entry.path.name.removesuffix('.zarr')
        stores_payload[key] = build_store_entry(entry, entry.path)
    return CatalogSnapshot(
        cache_schema_version=CACHE_SCHEMA_VERSION,
        protocol_version=PROTOCOL_VERSION,
        catalog_version=_next_catalog_version(previous),
        built_at=_now_iso(now_func),
        stores_dir_fingerprint=fingerprint(stores_dir),
        stores=stores_payload,
    )


def rebuild(
    stores_dir: Path,
    *,
    now_func: Callable[[], float] = time.time,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT_S,
) -> CatalogSnapshot:
    """Full walk + probe of every ``*.zarr`` under *stores_dir*.

    Bumps ``catalog_version`` and atomically rewrites the cache. Holds
    the writer lock for the whole walk — simpler and race-free; at
    target scale (≤100 stores on a Hetzner VPS) the walk finishes in
    tens of milliseconds.
    """
    _, catalog_path, _ = _catalog_paths(stores_dir)
    try:
        with _catalog_lock(stores_dir, timeout=lock_timeout):
            previous = _load_catalog_file(catalog_path)
            snapshot = _build_snapshot(stores_dir, previous, now_func=now_func)
            _atomic_write(catalog_path, snapshot)
            return snapshot
    except Timeout as exc:
        msg = f'Could not acquire catalog lock within {lock_timeout:g}s'
        raise CacheLockError(msg) from exc


def invalidate_store(
    stores_dir: Path,
    store_name: str,
    *,
    now_func: Callable[[], float] = time.time,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT_S,
) -> CatalogSnapshot:
    """Re-probe a single store and splice the result into the catalog.

    Bumps ``catalog_version`` and atomically rewrites the cache. If no
    cache exists yet, degrades to a full rebuild. If *store_name* has
    been removed from disk, its entry is dropped from the snapshot.
    """
    _, catalog_path, _ = _catalog_paths(stores_dir)
    try:
        with _catalog_lock(stores_dir, timeout=lock_timeout):
            previous = _load_catalog_file(catalog_path)
            if previous is None:
                snapshot = _build_snapshot(stores_dir, None, now_func=now_func)
                _atomic_write(catalog_path, snapshot)
                return snapshot

            new_stores = dict(previous.stores)
            zarr_path = stores_dir / f'{store_name}.zarr'
            if zarr_path.is_dir():
                entry = _probe_zarr(zarr_path)
                new_stores[store_name] = build_store_entry(entry, zarr_path)
            else:
                new_stores.pop(store_name, None)

            snapshot = CatalogSnapshot(
                cache_schema_version=CACHE_SCHEMA_VERSION,
                protocol_version=PROTOCOL_VERSION,
                catalog_version=previous.catalog_version + 1,
                built_at=_now_iso(now_func),
                stores_dir_fingerprint=fingerprint(stores_dir),
                stores=new_stores,
            )
            _atomic_write(catalog_path, snapshot)
            return snapshot
    except Timeout as exc:
        msg = f'Could not acquire catalog lock within {lock_timeout:g}s'
        raise CacheLockError(msg) from exc


def read_catalog(
    stores_dir: Path,
    *,
    ttl_s: float = DEFAULT_TTL_S,
    now_func: Callable[[], float] = time.time,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT_S,
) -> CatalogSnapshot:
    """Return the current catalog, rebuilding only when stale and changed.

    Fast path (warm & fresh): one file read, one JSON parse. No lock.

    Slow paths:

    - Missing / unparseable / schema-mismatched cache → cold rebuild.
    - TTL expired & fingerprint unchanged → return existing snapshot
      as-is. No write, no lock, ``built_at`` stays put.
    - TTL expired & fingerprint changed → rebuild (bumps version).

    ``lock_timeout`` is forwarded to ``rebuild`` for the rebuild branches;
    the read path itself never acquires the writer lock.
    """
    _, catalog_path, _ = _catalog_paths(stores_dir)

    existing = _load_catalog_file(catalog_path)
    if existing is None:
        return rebuild(stores_dir, now_func=now_func, lock_timeout=lock_timeout)

    if age_seconds(existing.built_at, now_func=now_func) < ttl_s:
        return existing

    if fingerprint(stores_dir) == existing.stores_dir_fingerprint:
        return existing

    return rebuild(stores_dir, now_func=now_func, lock_timeout=lock_timeout)


def peek_stats(
    stores_dir: Path,
    *,
    now_func: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Return cache-file diagnostics without triggering a rebuild.

    Read-only; no lock is acquired and no bytes are written. Safe to call
    against a missing, stale, or corrupt cache — the ``status`` field
    discriminates:

    - ``'missing'``: no cache file at ``<stores_dir>/.meta/catalog.json``.
    - ``'corrupt'``: file present but unreadable / unparseable / schema
      mismatch. The ``cache_file_size_bytes`` field is still populated so
      operators can gauge whether the file is empty vs. truncated.
    - ``'ok'``: full detail dict including ``catalog_version``,
      ``built_at``, ``age_s``, ``fingerprint_match``, ``store_count``, and
      ``cache_file_size_bytes``.

    ``age_s`` measures time since the last actual build (``rebuild`` or
    ``invalidate_store``); the read path never refreshes ``built_at``.

    ``fingerprint_match`` compares the cached fingerprint against a live
    ``fingerprint(stores_dir)`` walk. A mismatch means the TTL-triggered
    refresh path would choose a full rebuild on the next read.
    """
    _, catalog_path, _ = _catalog_paths(stores_dir)

    try:
        size = catalog_path.stat().st_size
    except FileNotFoundError:
        return {'status': 'missing'}
    except OSError as exc:
        return {'status': 'corrupt', 'detail': str(exc)}

    snap = _load_catalog_file(catalog_path)
    if snap is None:
        return {'status': 'corrupt', 'cache_file_size_bytes': size}

    return {
        'status': 'ok',
        'catalog_version': snap.catalog_version,
        'built_at': snap.built_at,
        'age_s': round(age_seconds(snap.built_at, now_func=now_func), 3),
        'fingerprint_match': fingerprint(stores_dir) == snap.stores_dir_fingerprint,
        'store_count': len(snap.stores),
        'cache_file_size_bytes': size,
    }

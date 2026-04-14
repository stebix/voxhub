"""Unit tests for ``voxhub_core.server.catalog_cache``.

Scope is strictly the cache module itself. No server-CLI wiring is
exercised here — that lands in PR 2.
"""

from __future__ import annotations

import json
import threading
import time
from typing import TYPE_CHECKING, Any

import pytest
import zarr
from _core_helpers import create_zarr_store, populate_store_annotation

from voxhub_core.attributes import DATASET_ATTRIBUTES_KEY
from voxhub_core.catalog import discover_zarr_stores
from voxhub_core.server import catalog_cache as cc
from voxhub_core.staging import extract_spatial_metadata

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def clock() -> dict[str, float]:
    """Mutable clock container; tests advance ``clock['now']`` directly."""
    return {'now': 1_700_000_000.0}


@pytest.fixture
def now_func(clock: dict[str, float]):
    """A ``Callable[[], float]`` reading from the *clock* fixture."""
    return lambda: clock['now']


@pytest.fixture
def stores(stores_dir_factory) -> Path:
    """Build a stores dir with three zarr stores — one with annotations."""
    root = stores_dir_factory(
        store_names=('alpha', 'beta', 'gamma'),
        with_annotations=False,
    )
    populate_store_annotation(root / 'alpha.zarr')
    return root


# ---------------------------------------------------------------------------
# Cold build / warm hit / TTL
# ---------------------------------------------------------------------------


def test_cold_build_creates_cache_file(stores: Path, now_func) -> None:
    snap = cc.read_catalog(stores, now_func=now_func)

    assert snap.catalog_version == 1
    assert snap.cache_schema_version == cc.CACHE_SCHEMA_VERSION
    assert set(snap.stores.keys()) == {'alpha', 'beta', 'gamma'}

    _, catalog_path, _ = cc._catalog_paths(stores)
    assert catalog_path.is_file()

    # File is valid JSON and round-trips to the same snapshot.
    data = json.loads(catalog_path.read_text())
    assert data['catalog_version'] == 1
    assert set(data['stores'].keys()) == {'alpha', 'beta', 'gamma'}


def test_warm_hit_within_ttl_does_not_rebuild(
    stores: Path,
    clock: dict[str, float],
    now_func,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snap1 = cc.read_catalog(stores, now_func=now_func)
    built_at_1 = snap1.built_at

    # Any rebuild would call _build_snapshot; poison it to detect rebuilds.
    def _boom(*_a: object, **_kw: object) -> None:
        raise AssertionError('warm hit must not rebuild')

    monkeypatch.setattr(cc, '_build_snapshot', _boom)

    # Advance slightly, still within the default TTL.
    clock['now'] += 1.0
    snap2 = cc.read_catalog(stores, now_func=now_func)

    assert snap2.catalog_version == snap1.catalog_version
    assert snap2.built_at == built_at_1


def test_ttl_expiry_without_changes_refreshes_built_at_only(
    stores: Path,
    clock: dict[str, float],
    now_func,
) -> None:
    snap1 = cc.read_catalog(stores, ttl_s=10.0, now_func=now_func)

    clock['now'] += 1000.0
    snap2 = cc.read_catalog(stores, ttl_s=10.0, now_func=now_func)

    assert snap2.catalog_version == snap1.catalog_version
    assert snap2.built_at != snap1.built_at
    assert snap2.stores_dir_fingerprint == snap1.stores_dir_fingerprint


def test_ttl_expiry_with_changes_triggers_rebuild(
    stores: Path,
    stores_dir_factory,
    clock: dict[str, float],
    now_func,
) -> None:
    snap1 = cc.read_catalog(stores, ttl_s=10.0, now_func=now_func)

    # Add a new store at the top level — changes the fingerprint.
    create_zarr_store(stores / 'delta.zarr')

    clock['now'] += 1000.0
    snap2 = cc.read_catalog(stores, ttl_s=10.0, now_func=now_func)

    assert snap2.catalog_version == snap1.catalog_version + 1
    assert 'delta' in snap2.stores


# ---------------------------------------------------------------------------
# rebuild / invalidate_store
# ---------------------------------------------------------------------------


def test_rebuild_bumps_version_monotonically(stores: Path, now_func) -> None:
    v = cc.rebuild(stores, now_func=now_func).catalog_version
    for _ in range(3):
        snap = cc.rebuild(stores, now_func=now_func)
        assert snap.catalog_version == v + 1
        v = snap.catalog_version


def test_invalidate_store_only_changes_target(
    stores: Path, now_func, clock: dict[str, float]
) -> None:
    before = cc.read_catalog(stores, now_func=now_func)
    beta_before = before.stores['beta']
    gamma_before = before.stores['gamma']

    # Mutate the alpha store on disk (touch annotation metadata) — not
    # detectable by the top-level fingerprint, so we must invalidate
    # explicitly.
    populate_store_annotation(
        stores / 'alpha.zarr',
        nano_id='freshnew',
        short_random='cd34',
        integrated_at='2026-02-02T00:00:00+00:00',
    )

    clock['now'] += 0.5
    after = cc.invalidate_store(stores, 'alpha', now_func=now_func)

    assert after.catalog_version == before.catalog_version + 1
    assert after.stores['beta'] == beta_before
    assert after.stores['gamma'] == gamma_before
    assert after.stores['alpha'] != before.stores['alpha']
    assert len(after.stores['alpha']['annotations']) > len(
        before.stores['alpha']['annotations']
    )


def test_invalidate_store_on_missing_cache_falls_back_to_full_rebuild(
    stores: Path, now_func
) -> None:
    _, catalog_path, _ = cc._catalog_paths(stores)
    assert not catalog_path.exists()

    snap = cc.invalidate_store(stores, 'alpha', now_func=now_func)

    assert snap.catalog_version == 1
    assert set(snap.stores.keys()) == {'alpha', 'beta', 'gamma'}
    assert catalog_path.is_file()


def test_invalidate_store_for_removed_store_drops_entry(
    stores: Path, now_func
) -> None:
    cc.read_catalog(stores, now_func=now_func)

    import shutil

    shutil.rmtree(stores / 'beta.zarr')

    after = cc.invalidate_store(stores, 'beta', now_func=now_func)
    assert 'beta' not in after.stores
    assert set(after.stores.keys()) == {'alpha', 'gamma'}


# ---------------------------------------------------------------------------
# Corruption / error paths
# ---------------------------------------------------------------------------


def test_corrupt_cache_triggers_rebuild(stores: Path, now_func) -> None:
    _, catalog_path, _ = cc._catalog_paths(stores)
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_path.write_text('{this is not valid json')

    snap = cc.read_catalog(stores, now_func=now_func)
    # Counter resets to 1 (we can't read the previous value) — documented
    # as acceptable: PR 5's client short-circuit treats a lower server
    # version as a forced refetch, so a regression is safe.
    assert snap.catalog_version == 1
    assert set(snap.stores.keys()) == {'alpha', 'beta', 'gamma'}


def test_wrong_schema_version_triggers_rebuild(stores: Path, now_func) -> None:
    snap1 = cc.read_catalog(stores, now_func=now_func)
    _, catalog_path, _ = cc._catalog_paths(stores)

    # Rewrite with a schema version that doesn't match CACHE_SCHEMA_VERSION.
    data = json.loads(catalog_path.read_text())
    data['cache_schema_version'] = cc.CACHE_SCHEMA_VERSION + 999
    catalog_path.write_text(json.dumps(data))

    snap2 = cc.read_catalog(stores, now_func=now_func)
    # Previous snapshot was unreadable under the new schema — counter
    # restarts at 1. Same regression tradeoff as corrupt-cache.
    assert snap2.catalog_version == 1
    assert set(snap2.stores.keys()) == set(snap1.stores.keys())


def test_atomic_write_leaves_no_tmp_files(stores: Path, now_func) -> None:
    cc.read_catalog(stores, now_func=now_func)
    meta_dir, _, _ = cc._catalog_paths(stores)
    tmp_siblings = [p for p in meta_dir.iterdir() if p.name.endswith('.tmp')]
    assert tmp_siblings == []


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrent_invalidate_and_read(stores: Path) -> None:
    """Hammer ``invalidate_store`` from one thread while another reads.

    Every read must return a well-formed snapshot; no exceptions escape.
    """
    cc.read_catalog(stores)

    stop = threading.Event()
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            while not stop.is_set():
                cc.invalidate_store(stores, 'alpha')
        except BaseException as exc:
            errors.append(exc)

    def reader() -> None:
        try:
            while not stop.is_set():
                snap = cc.read_catalog(stores, ttl_s=0.0)
                assert 'alpha' in snap.stores
                assert snap.catalog_version >= 1
        except BaseException as exc:
            errors.append(exc)

    w = threading.Thread(target=writer)
    r = threading.Thread(target=reader)
    w.start()
    r.start()
    time.sleep(0.5)
    stop.set()
    w.join(timeout=10)
    r.join(timeout=10)

    assert not errors, f'unexpected error(s): {errors}'


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------


def test_fingerprint_stable_without_changes(stores: Path) -> None:
    fp1 = cc.fingerprint(stores)
    fp2 = cc.fingerprint(stores)
    assert fp1 == fp2


def test_fingerprint_sensitive_to_add_and_remove(stores: Path) -> None:
    fp_base = cc.fingerprint(stores)

    create_zarr_store(stores / 'delta.zarr')
    fp_after_add = cc.fingerprint(stores)
    assert fp_after_add != fp_base

    import shutil

    shutil.rmtree(stores / 'delta.zarr')
    fp_after_remove = cc.fingerprint(stores)
    assert fp_after_remove == fp_base


def test_fingerprint_ignores_deep_interior_edits(stores: Path) -> None:
    """Flat-dir scope: interior file edits are explicitly not detected.

    Interior correctness is the responsibility of the write-through hook
    (PR 3) and the admin refresh (PR 4), not the fingerprint.
    """
    fp_before = cc.fingerprint(stores)

    # Edit a deep file inside an existing store. On Linux this does not
    # update the top-level .zarr directory's mtime.
    deep = stores / 'alpha.zarr' / 'zarr.json'
    assert deep.is_file()
    original = deep.read_bytes()
    deep.write_bytes(original + b' ')
    try:
        fp_after = cc.fingerprint(stores)
        assert fp_after == fp_before
    finally:
        deep.write_bytes(original)


# ---------------------------------------------------------------------------
# build_store_entry parity with _run_list_stores
# ---------------------------------------------------------------------------


def _inline_payload(zarr_path: Path) -> dict[str, Any]:
    """Reproduce today's inline ``_run_list_stores`` payload for a store.

    Mirrors ``server/cli.py`` so the parity test has a ground truth that
    doesn't depend on the server CLI being importable.
    """
    from voxhub_core.catalog import _probe_zarr

    entry = _probe_zarr(zarr_path)
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

    root = zarr.open_group(zarr_path, mode='r')
    arr = root['raw']['full']
    a = dict(arr.attrs)

    try:
        origin, space_directions, spacing_mm = extract_spatial_metadata(a)
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

    da_raw = dict(root.attrs).get(DATASET_ATTRIBUTES_KEY)
    return {
        'name': store_name,
        'shape': list(arr.shape),
        'dtype': str(arr.dtype),
        'origin_lps': origin.tolist(),
        'spacing_mm': spacing_mm,
        'space_directions': space_directions.tolist(),
        'annotations': annotations,
        'error': None,
        'dataset_attributes': da_raw,
    }


def test_build_store_entry_matches_inline_payload(stores: Path) -> None:
    entries = {
        e.path.name.removesuffix('.zarr'): e for e in discover_zarr_stores(stores)
    }
    for name, entry in entries.items():
        zarr_path = entry.path
        expected = _inline_payload(zarr_path)
        actual = cc.build_store_entry(entry, zarr_path)
        assert actual == expected, f'parity mismatch for store {name!r}'


def test_build_store_entry_handles_missing_spatial_metadata(tmp_path: Path) -> None:
    """A probed entry whose attributes lack the spatial keys hits the
    ``Missing spatial metadata`` branch rather than raising."""
    from voxhub_core.catalog import ZarrEntry

    # Synthesise an entry shaped like a real probe result but with attrs
    # that pass the ``if not attrs_map`` guard yet miss the spatial keys
    # that ``extract_spatial_metadata`` requires.
    store_path = tmp_path / 'sparse.zarr'
    store_path.mkdir()
    entry = ZarrEntry(
        path=store_path,
        shape=(4, 4, 4),
        dtype='float32',
        attributes={'unrelated_key': 'present-but-not-spatial'},
    )
    payload = cc.build_store_entry(entry, store_path)
    assert payload['error'] == 'Missing spatial metadata'
    assert payload['origin_lps'] == []
    assert payload['shape'] == [4, 4, 4]


def test_build_store_entry_handles_discovery_error(
    stores_dir_factory, tmp_path: Path
) -> None:
    """A probe-level error produces the minimal error payload."""
    root = stores_dir_factory(store_names=('broken',), corrupt=('broken',))
    from voxhub_core.catalog import _probe_zarr

    zarr_path = root / 'broken.zarr'
    entry = _probe_zarr(zarr_path)
    assert entry.error is not None

    payload = cc.build_store_entry(entry, zarr_path)
    assert payload['error'] == entry.error
    assert payload['shape'] == []
    assert payload['annotations'] == []


def test_build_store_entry_preserves_dataset_attributes(
    stores_dir_factory,
) -> None:
    """Raw ``dataset_attributes`` round-trip without reopening the zarr."""
    da = {
        'modality': 'MSCT',
        'resolution': {'voxel_size': [0.5, 0.5, 0.5], 'unit': 'mm'},
        'origin': 'synthetic',
        'tags': {'domain': 'inner_ear'},
    }
    root = stores_dir_factory(
        store_names=('with_attrs',),
        dataset_attributes={'with_attrs': da},
    )
    from voxhub_core.catalog import _probe_zarr

    zarr_path = root / 'with_attrs.zarr'
    entry = _probe_zarr(zarr_path)
    payload = cc.build_store_entry(entry, zarr_path)
    assert payload['dataset_attributes'] == da

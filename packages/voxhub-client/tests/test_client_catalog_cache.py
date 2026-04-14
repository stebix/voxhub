"""Tests for the client-side catalog cache.

Plan: docs/plans/cache-buildout/pr-5-client-shortcircuit.md
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from voxhub_client.catalog_cache import (
    CACHE_FILENAME,
    ClientCatalogCache,
    list_stores_cached,
    server_key_for,
)
from voxhub_client.ssh import SshTarget

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


# ===================================================================
# server_key_for
# ===================================================================


class TestServerKeyFor:
    def test_user_host_only(self) -> None:
        target = SshTarget(user='alice', host='annotate.lab.edu')
        key = server_key_for(target)
        # No filesystem-reserved characters in the result.
        assert '@' not in key
        assert ':' not in key
        assert '/' not in key
        # Components are still recognisable.
        assert 'alice' in key
        assert 'annotate.lab.edu' in key

    def test_includes_port_when_set(self) -> None:
        target = SshTarget(user='alice', host='host', port=2222)
        key = server_key_for(target)
        assert '2222' in key

    def test_distinct_targets_produce_distinct_keys(self) -> None:
        a = server_key_for(SshTarget(user='alice', host='host'))
        b = server_key_for(SshTarget(user='bob', host='host'))
        c = server_key_for(SshTarget(user='alice', host='other-host'))
        d = server_key_for(SshTarget(user='alice', host='host', port=2222))
        assert len({a, b, c, d}) == 4


# ===================================================================
# ClientCatalogCache: read / write / clear
# ===================================================================


@pytest.fixture
def cache(tmp_path: Path) -> ClientCatalogCache:
    return ClientCatalogCache(cache_root=tmp_path / 'cache')


class TestRead:
    def test_missing_returns_none(self, cache: ClientCatalogCache) -> None:
        assert cache.read('voxhub_at_host') is None

    def test_corrupt_returns_none(self, cache: ClientCatalogCache) -> None:
        # Lay down a cache file by hand with non-JSON contents.
        path = cache.cache_root / 'voxhub_at_host' / CACHE_FILENAME
        path.parent.mkdir(parents=True)
        path.write_text('{not json at all')
        assert cache.read('voxhub_at_host') is None

    def test_wrong_top_level_type_returns_none(self, cache: ClientCatalogCache) -> None:
        path = cache.cache_root / 'voxhub_at_host' / CACHE_FILENAME
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps([1, 2, 3]))
        assert cache.read('voxhub_at_host') is None

    def test_missing_required_field_returns_none(self, cache: ClientCatalogCache) -> None:
        path = cache.cache_root / 'voxhub_at_host' / CACHE_FILENAME
        path.parent.mkdir(parents=True)
        # Missing ``stores``.
        path.write_text(json.dumps({'catalog_version': 7}))
        assert cache.read('voxhub_at_host') is None


class TestWriteRead:
    def test_round_trips_payload(self, cache: ClientCatalogCache) -> None:
        stores = [
            {'name': 'alpha', 'shape': [10, 10, 10], 'annotations': []},
            {'name': 'bravo', 'shape': [4, 4, 4], 'annotations': []},
        ]
        cache.write('voxhub_at_host', catalog_version=42, stores=stores)
        read = cache.read('voxhub_at_host')
        assert read == {'catalog_version': 42, 'stores': stores}

    def test_overwrite_replaces_payload(self, cache: ClientCatalogCache) -> None:
        cache.write('voxhub_at_host', 1, [{'name': 'alpha'}])
        cache.write('voxhub_at_host', 2, [{'name': 'bravo'}])
        read = cache.read('voxhub_at_host')
        assert read is not None
        assert read['catalog_version'] == 2
        assert read['stores'] == [{'name': 'bravo'}]

    def test_no_tmp_file_left_after_successful_write(
        self, cache: ClientCatalogCache
    ) -> None:
        cache.write('voxhub_at_host', 1, [{'name': 'alpha'}])
        server_dir = cache.cache_root / 'voxhub_at_host'
        leftovers = [p for p in server_dir.iterdir() if p.suffix == '.tmp']
        assert leftovers == []
        # The actual cache file is exactly the one we expect.
        assert (server_dir / CACHE_FILENAME).is_file()

    def test_creates_per_server_directory(self, cache: ClientCatalogCache) -> None:
        assert not cache.cache_root.exists()
        cache.write('voxhub_at_host', 1, [])
        assert (cache.cache_root / 'voxhub_at_host' / CACHE_FILENAME).is_file()


class TestMultiServerIsolation:
    def test_writing_one_does_not_affect_other(self, cache: ClientCatalogCache) -> None:
        cache.write('voxhub_at_a', 1, [{'name': 'alpha'}])
        cache.write('voxhub_at_b', 99, [{'name': 'zulu'}])

        a = cache.read('voxhub_at_a')
        b = cache.read('voxhub_at_b')
        assert a is not None
        assert b is not None
        assert a['catalog_version'] == 1
        assert b['catalog_version'] == 99
        assert a['stores'] == [{'name': 'alpha'}]
        assert b['stores'] == [{'name': 'zulu'}]


class TestClear:
    def test_clear_single_server_only_drops_that_one(
        self, cache: ClientCatalogCache
    ) -> None:
        cache.write('voxhub_at_a', 1, [])
        cache.write('voxhub_at_b', 1, [])

        cache.clear('voxhub_at_a')

        assert cache.read('voxhub_at_a') is None
        assert cache.read('voxhub_at_b') is not None

    def test_clear_all_drops_everything(self, cache: ClientCatalogCache) -> None:
        cache.write('voxhub_at_a', 1, [])
        cache.write('voxhub_at_b', 1, [])

        cache.clear()

        assert cache.read('voxhub_at_a') is None
        assert cache.read('voxhub_at_b') is None
        assert not cache.cache_root.exists()

    def test_clear_missing_server_is_noop(self, cache: ClientCatalogCache) -> None:
        # Should not raise.
        cache.clear('does-not-exist')

    def test_clear_all_when_empty_is_noop(self, cache: ClientCatalogCache) -> None:
        cache.clear()


class TestSanitisation:
    def test_path_traversal_keys_are_neutralised(self, cache: ClientCatalogCache) -> None:
        """A malicious / careless server_key must not escape the cache root."""
        cache.write('../escape', 1, [{'name': 'alpha'}])
        # The on-disk directory must live under cache_root, not above it.
        contents = list(cache.cache_root.iterdir())
        assert len(contents) == 1
        assert contents[0].is_dir()
        # The directory name has no path separators.
        assert '/' not in contents[0].name
        # And the round-trip works under the same key.
        read = cache.read('../escape')
        assert read is not None
        assert read['catalog_version'] == 1


# ===================================================================
# list_stores_cached
# ===================================================================


class _StubRunner:
    """Minimal stand-in for SshRunner that records sent args."""

    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args: str, timeout: float | None = 60) -> dict:
        del timeout
        self.calls.append(args)
        if not self._responses:
            msg = f'StubRunner has no more queued responses (got {args!r})'
            raise AssertionError(msg)
        return self._responses.pop(0)


@pytest.fixture
def make_runner() -> Callable[[list[dict]], _StubRunner]:
    def _build(responses: list[dict]) -> _StubRunner:
        return _StubRunner(responses)

    return _build


class TestListStoresCached:
    def test_first_call_omits_if_version_and_writes_cache(
        self,
        cache: ClientCatalogCache,
        make_runner: Callable[[list[dict]], _StubRunner],
    ) -> None:
        runner = make_runner(
            [
                {
                    'protocol_version': 1,
                    'catalog_version': 1,
                    'stores': [{'name': 'alpha'}],
                }
            ]
        )

        result = list_stores_cached(runner, cache, 'voxhub_at_host')  # type: ignore[arg-type]

        # No --if-version flag on a cold cache.
        assert runner.calls == [('list-stores',)]
        assert result['catalog_version'] == 1
        assert result['stores'] == [{'name': 'alpha'}]

        # Cache populated.
        cached = cache.read('voxhub_at_host')
        assert cached is not None
        assert cached['catalog_version'] == 1
        assert cached['stores'] == [{'name': 'alpha'}]

    def test_second_call_sends_if_version_and_short_circuits(
        self,
        cache: ClientCatalogCache,
        make_runner: Callable[[list[dict]], _StubRunner],
    ) -> None:
        runner = make_runner(
            [
                {
                    'protocol_version': 1,
                    'catalog_version': 5,
                    'stores': [{'name': 'alpha'}, {'name': 'bravo'}],
                },
                {
                    'protocol_version': 1,
                    'catalog_version': 5,
                    'unchanged': True,
                },
            ]
        )

        first = list_stores_cached(runner, cache, 'voxhub_at_host')  # type: ignore[arg-type]
        second = list_stores_cached(runner, cache, 'voxhub_at_host')  # type: ignore[arg-type]

        # First call: no --if-version. Second call: --if-version 5.
        assert runner.calls[0] == ('list-stores',)
        assert runner.calls[1] == ('list-stores', '--if-version', '5')

        # Short-circuit response splices the cached stores back in.
        assert second['catalog_version'] == 5
        assert second['stores'] == first['stores']

    def test_full_payload_with_new_version_updates_cache(
        self,
        cache: ClientCatalogCache,
        make_runner: Callable[[list[dict]], _StubRunner],
    ) -> None:
        runner = make_runner(
            [
                {
                    'protocol_version': 1,
                    'catalog_version': 1,
                    'stores': [{'name': 'alpha'}],
                },
                {
                    'protocol_version': 1,
                    'catalog_version': 2,
                    'stores': [{'name': 'alpha'}, {'name': 'bravo'}],
                },
            ]
        )

        list_stores_cached(runner, cache, 'voxhub_at_host')  # type: ignore[arg-type]
        list_stores_cached(runner, cache, 'voxhub_at_host')  # type: ignore[arg-type]

        cached = cache.read('voxhub_at_host')
        assert cached is not None
        assert cached['catalog_version'] == 2
        assert cached['stores'] == [{'name': 'alpha'}, {'name': 'bravo'}]

    def test_force_skips_if_version_even_when_cache_warm(
        self,
        cache: ClientCatalogCache,
        make_runner: Callable[[list[dict]], _StubRunner],
    ) -> None:
        # Pre-populate the cache.
        cache.write('voxhub_at_host', 7, [{'name': 'alpha'}])

        runner = make_runner(
            [
                {
                    'protocol_version': 1,
                    'catalog_version': 8,
                    'stores': [{'name': 'alpha'}, {'name': 'bravo'}],
                }
            ]
        )

        result = list_stores_cached(
            runner,
            cache,
            'voxhub_at_host',  # type: ignore[arg-type]
            force=True,
        )

        # No --if-version flag, even though the cache is warm.
        assert runner.calls == [('list-stores',)]
        assert result['catalog_version'] == 8
        assert result['stores'] == [{'name': 'alpha'}, {'name': 'bravo'}]

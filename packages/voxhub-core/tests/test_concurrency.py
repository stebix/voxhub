"""Concurrency tests for voxhub-core.

Covers voxhub_core.server.locks.store_lock and multi-process race scenarios
in _run_integrate_annotations.  All concurrency here is cross-process
(multiprocessing or subprocess.Popen), never threading — production runs
each voxhub-server invocation as a fresh SSH-spawned process.

Plan: docs/testing/concurrency-and-provenance.md §4
"""

import json
import multiprocessing as mp
import os
import signal
import time
from pathlib import Path
from typing import Any

import pytest
from _core_helpers import (
    build_staging_dir_entries,
)
from filelock import FileLock, Timeout

from voxhub_core.server import cli as server_cli
from voxhub_core.server.locks import store_lock
from voxhub_core.server.provenance import record_provenance
from voxhub_schema import IssueRecord

pytestmark = pytest.mark.slow


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _build_staging(
    staging_parent: Path,
    staging_name: str,
    store_names: list[str],
) -> Path:
    """Build an isolated staging dir under ``staging_parent``.

    Each invocation produces a distinct directory so that ``staging_dir.name``
    (used as ``pull_session_id`` in provenance) differs per concurrent
    integrate.  Ontology declaration is now passed via the CLI at
    ``integrate-annotations`` invocation time, not written into the
    staging dir.
    """
    staging = staging_parent / staging_name
    staging.mkdir(parents=True, exist_ok=True)
    for store in store_names:
        build_staging_dir_entries(staging / store, include_seg=True)
    return staging


def _parse_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding='utf-8').splitlines()
        if line.strip()
    ]


# Module-level worker used by multiprocessing (fork) so it does not need
# to be pickled.
def _record_provenance_worker(kwargs: dict[str, Any]) -> None:
    """Child entry point: call record_provenance with the given kwargs."""
    # Rebuild IssueRecord instances if present (they are plain dicts after
    # fork-inheritance too, but for future-proofing with spawn).
    issues_raw = kwargs.pop('_issues_raw', None)
    if issues_raw is not None:
        kwargs['issues'] = [
            IssueRecord(severity=i['severity'], message=i['message']) for i in issues_raw
        ]
    record_provenance(**kwargs)


def _run_workers_parallel(
    targets: list[dict[str, Any]],
    *,
    timeout: float = 60.0,
) -> list[int | None]:
    """Spawn N fork-children running ``_record_provenance_worker`` in parallel.

    Returns each child's exit code (``None`` if it never exited).
    """
    ctx = mp.get_context('fork')
    procs = [
        ctx.Process(target=_record_provenance_worker, args=(kwargs,))
        for kwargs in targets
    ]
    for p in procs:
        p.start()
    exit_codes: list[int | None] = []
    for p in procs:
        p.join(timeout=timeout)
        if p.is_alive():
            p.terminate()
            p.join(timeout=5.0)
        exit_codes.append(p.exitcode)
    return exit_codes


# Worker that acquires the lock then SIGKILLs itself mid-hold, simulating
# an ungraceful server crash.  The OS must release fcntl.flock on death.
def _acquire_and_suicide(zarr_path_str: str, acquired: Any) -> None:
    from voxhub_core.server.locks import store_lock as _lock

    with _lock(Path(zarr_path_str)):
        acquired.set()
        # Die while still holding the lock.
        os.kill(os.getpid(), signal.SIGKILL)


# ===========================================================================
# store_lock unit tests
# ===========================================================================


class TestStoreLock:
    """Covers voxhub_core.server.locks.store_lock."""

    def test_returns_filelock_instance(self, tmp_path):
        lock = store_lock(tmp_path / 'foo.zarr')
        assert isinstance(lock, FileLock)

    def test_lock_file_is_sibling_of_zarr_dir(self, tmp_path):
        zarr_path = tmp_path / 'foo.zarr'
        lock = store_lock(zarr_path)
        assert Path(str(lock.lock_file)) == tmp_path / 'foo.zarr.lock'

    def test_lock_is_exclusive_cross_process(self, tmp_path, held_lock):
        """Cross-process exclusivity: while a child holds the lock, this
        process cannot acquire it within a short timeout.

        (filelock is re-entrant within the same process when using the same
        Lock object, so the meaningful exclusivity contract is cross-process.)
        """
        zarr_path = tmp_path / 'foo.zarr'
        zarr_path.mkdir()

        with held_lock(zarr_path):
            lock = store_lock(zarr_path, timeout=0.2)
            with pytest.raises(Timeout):
                lock.acquire()

    def test_lock_released_on_context_exit(self, tmp_path):
        zarr_path = tmp_path / 'foo.zarr'
        zarr_path.mkdir()

        with store_lock(zarr_path):
            pass

        # Fresh acquirer succeeds immediately (whether the lock file is
        # removed on release or kept is a filelock-version detail; the
        # contract we care about is released-ness).
        with store_lock(zarr_path, timeout=1.0):
            pass

    def test_custom_timeout_propagates(self, tmp_path, held_lock):
        zarr_path = tmp_path / 'foo.zarr'
        zarr_path.mkdir()

        with held_lock(zarr_path):
            lock = store_lock(zarr_path, timeout=0.1)
            t0 = time.monotonic()
            with pytest.raises(Timeout):
                lock.acquire()
            # Generous upper bound — should fail well before 2s.
            assert time.monotonic() - t0 < 2.0


# ===========================================================================
# Concurrent integrate: same store
# ===========================================================================


class TestConcurrentIntegrateSameStore:
    """Two or more integrate-annotations invocations against the same zarr store.

    Must be serialized by store_lock; both must succeed under independent
    annotator IDs without data loss or partial writes visible to either run.
    """

    def test_two_concurrent_writes_to_same_store_serialize(
        self,
        stores_dir_factory,
        concurrent_integrate_runner,
        tmp_path,
    ):
        stores_dir = stores_dir_factory(('alpha',))
        staging_a = _build_staging(tmp_path / 'stagings', 'vxhb-staging-a', ['alpha'])
        staging_b = _build_staging(tmp_path / 'stagings', 'vxhb-staging-b', ['alpha'])

        results = concurrent_integrate_runner(
            [
                {
                    'stores_dir': stores_dir,
                    'staging_dir': staging_a,
                    'annotator_id': 'alice',
                    'nano_id': 'aaaa1111',
                },
                {
                    'stores_dir': stores_dir,
                    'staging_dir': staging_b,
                    'annotator_id': 'bob',
                    'nano_id': 'bbbb2222',
                },
            ]
        )

        assert all(r['returncode'] == 0 for r in results), [r['stderr'] for r in results]

        annotator_dirs = {
            p.name
            for p in (stores_dir / 'alpha.zarr' / 'annotations').iterdir()
            if p.is_dir()
        }
        assert 'alice-aaaa1111' in annotator_dirs
        assert 'bob-bbbb2222' in annotator_dirs

        records = _parse_jsonl(stores_dir / '.meta' / 'provenance.jsonl')
        assert len(records) == 2
        assert {r['annotator_id'] for r in records} == {'alice', 'bob'}

    def test_concurrent_writes_do_not_deadlock(
        self,
        stores_dir_factory,
        concurrent_integrate_runner,
        tmp_path,
    ):
        stores_dir = stores_dir_factory(('alpha',))
        invocations = []
        for i in range(5):
            staging = _build_staging(
                tmp_path / 'stagings', f'vxhb-staging-{i}', ['alpha']
            )
            invocations.append(
                {
                    'stores_dir': stores_dir,
                    'staging_dir': staging,
                    'annotator_id': f'user{i}',
                    'nano_id': f'abcd{i:04d}',
                }
            )

        t0 = time.monotonic()
        results = concurrent_integrate_runner(invocations, timeout=120.0)
        elapsed = time.monotonic() - t0

        assert all(r['returncode'] == 0 for r in results), [r['stderr'] for r in results]
        assert elapsed < 60.0, f'5 concurrent integrates took {elapsed:.1f}s'

        records = _parse_jsonl(stores_dir / '.meta' / 'provenance.jsonl')
        assert len(records) == 5

    def test_lock_contention_respects_timeout(
        self,
        stores_dir_factory,
        server_argv,
        held_lock,
        tmp_path,
        monkeypatch,
    ):
        """Hold the lock in another process, invoke the handler in-process
        with a short lock timeout → ``filelock.Timeout`` propagates cleanly.

        Running in-process (rather than via subprocess) lets us monkey-patch
        the lock timeout without needing an env-var knob in the CLI.  It
        still exercises the real ``_run_integrate_annotations`` code path.
        """
        stores_dir = stores_dir_factory(('alpha',))
        staging = _build_staging(
            tmp_path / 'stagings', 'vxhb-staging-contended', ['alpha']
        )

        def _short_lock(path: Path, *, timeout: float = 60.0) -> FileLock:
            del timeout
            return store_lock(path, timeout=0.3)

        monkeypatch.setattr(server_cli, 'store_lock', _short_lock)

        with held_lock(stores_dir / 'alpha.zarr'), pytest.raises(Timeout):
            server_cli._run_integrate_annotations(
                server_argv(
                    stores_dir=stores_dir,
                    staging_dir=str(staging),
                    annotator_id='alice',
                    nano_id='ccccdddd',
                    expected_ontology=['inner-ear-structures'],
                )
            )

    def test_lock_released_after_crash(
        self,
        tmp_path,
    ):
        """A process that SIGKILLs itself mid-hold must leave the lock
        available for the next acquirer (POSIX flock is released on death)."""
        zarr_path = tmp_path / 'foo.zarr'
        zarr_path.mkdir()

        ctx = mp.get_context('fork')
        acquired = ctx.Event()
        proc = ctx.Process(target=_acquire_and_suicide, args=(str(zarr_path), acquired))
        proc.start()
        try:
            assert acquired.wait(timeout=10.0)
            proc.join(timeout=5.0)
            assert not proc.is_alive()
            assert proc.exitcode is not None
            assert proc.exitcode < 0  # SIGKILL
        finally:
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=2.0)

        # New acquirer must succeed quickly.
        with store_lock(zarr_path, timeout=2.0):
            pass


# ===========================================================================
# Concurrent integrate: different stores
# ===========================================================================


class TestConcurrentIntegrateDifferentStores:
    """Integrates targeting distinct stores should not serialize."""

    def test_two_different_stores_fully_parallel(
        self,
        stores_dir_factory,
        concurrent_integrate_runner,
        tmp_path,
    ):
        stores_dir = stores_dir_factory(('alpha', 'beta'))
        staging_alpha = _build_staging(
            tmp_path / 'stagings', 'vxhb-staging-alpha', ['alpha']
        )
        staging_beta = _build_staging(
            tmp_path / 'stagings', 'vxhb-staging-beta', ['beta']
        )

        results = concurrent_integrate_runner(
            [
                {
                    'stores_dir': stores_dir,
                    'staging_dir': staging_alpha,
                    'annotator_id': 'alice',
                    'nano_id': 'aaaa1111',
                },
                {
                    'stores_dir': stores_dir,
                    'staging_dir': staging_beta,
                    'annotator_id': 'bob',
                    'nano_id': 'bbbb2222',
                },
            ]
        )

        assert all(r['returncode'] == 0 for r in results), [r['stderr'] for r in results]
        records = _parse_jsonl(stores_dir / '.meta' / 'provenance.jsonl')
        assert {r['store'] for r in records} == {'alpha', 'beta'}

    def test_parallel_pushes_catalog_version_accounts_for_all(
        self,
        stores_dir_factory,
        concurrent_integrate_runner,
        server_argv,
        parsed_stdout,
        tmp_path,
    ):
        """Five parallel integrates on distinct stores must each invalidate
        the catalog exactly once; the post-settle list-stores must show
        every new annotation and ``catalog_version`` must equal the
        initial version plus the number of integrated stores.
        """
        store_names = [f'store{i:02d}' for i in range(5)]
        stores_dir = stores_dir_factory(tuple(store_names))

        # Seed the catalog so ``initial_version`` is stable.
        server_cli._run_list_stores(server_argv(stores_dir=stores_dir))
        initial_version = parsed_stdout()['catalog_version']

        invocations = []
        for i, name in enumerate(store_names):
            staging = _build_staging(
                tmp_path / 'stagings', f'vxhb-staging-{name}', [name]
            )
            invocations.append(
                {
                    'stores_dir': stores_dir,
                    'staging_dir': staging,
                    'annotator_id': f'user{i}',
                    'nano_id': f'abcd{i:04d}',
                }
            )

        results = concurrent_integrate_runner(invocations, timeout=120.0)
        assert all(r['returncode'] == 0 for r in results), [r['stderr'] for r in results]

        server_cli._run_list_stores(server_argv(stores_dir=stores_dir))
        payload = parsed_stdout()

        assert payload['catalog_version'] == initial_version + len(store_names)

        by_name = {s['name']: s for s in payload['stores']}
        assert set(by_name) == set(store_names)
        # Every integrated annotation must appear exactly once.
        for i, name in enumerate(store_names):
            annotations = by_name[name]['annotations']
            assert len(annotations) == 1, (name, annotations)
            assert annotations[0]['annotator_id'] == f'user{i}'


# ===========================================================================
# Concurrent provenance append
# ===========================================================================


class TestConcurrentProvenanceAppend:
    """The central .meta/provenance.jsonl is shared across stores.

    These tests probe the implicit POSIX O_APPEND atomicity guarantee
    (safe for writes under PIPE_BUF, typically 4 KB on Linux). If the
    high-concurrency test fails or the large-record test shows torn
    writes, the worktree should add an explicit meta_lock around the
    JSONL append.
    """

    def test_two_different_stores_append_single_jsonl_concurrently(
        self,
        stores_dir_factory,
        concurrent_integrate_runner,
        tmp_path,
    ):
        stores_dir = stores_dir_factory(('alpha', 'beta'))
        staging_a = _build_staging(tmp_path / 'stagings', 'vxhb-staging-a', ['alpha'])
        staging_b = _build_staging(tmp_path / 'stagings', 'vxhb-staging-b', ['beta'])

        results = concurrent_integrate_runner(
            [
                {
                    'stores_dir': stores_dir,
                    'staging_dir': staging_a,
                    'annotator_id': 'alice',
                    'nano_id': 'aaaa1111',
                },
                {
                    'stores_dir': stores_dir,
                    'staging_dir': staging_b,
                    'annotator_id': 'bob',
                    'nano_id': 'bbbb2222',
                },
            ]
        )

        assert all(r['returncode'] == 0 for r in results)

        records = _parse_jsonl(stores_dir / '.meta' / 'provenance.jsonl')
        assert len(records) == 2
        for rec in records:
            assert isinstance(rec, dict)
            assert rec['event'] == 'push'

    def test_high_concurrency_jsonl_integrity(
        self,
        stores_dir_factory,
    ):
        """10 concurrent appends directly via record_provenance (distinct
        stores, so store_lock does not serialize them).  Small records fit
        under PIPE_BUF — the POSIX O_APPEND guarantee should hold."""
        store_names = [f'store{i:02d}' for i in range(10)]
        stores_dir = stores_dir_factory(tuple(store_names), with_annotations=True)

        ann_path = 'annotations/alice-xyz45678/inner-ear-structures-20260101-ab12/data'
        targets = [
            {
                'stores_dir': stores_dir,
                'store_name': name,
                'annotation_path': ann_path,
                'annotator_id': f'annot{i:02d}',
                'machine_id': 'machine-x',
                'nano_id': f'abcd{i:04d}',
                'pull_session_id': f'session-{i:02d}',
                'ontology': 'inner-ear-structures',
                'ontology_version': 1,
                'source_nrrd_checksum': 'sha256:' + '0' * 64,
                'source_file': f'seg-{i}.nrrd',
            }
            for i, name in enumerate(store_names)
        ]
        exit_codes = _run_workers_parallel(targets)
        assert all(code == 0 for code in exit_codes), exit_codes

        records = _parse_jsonl(stores_dir / '.meta' / 'provenance.jsonl')
        assert len(records) == 10
        # Each record's (annotator_id, pull_session_id) pair is unique by
        # construction; the record must parse and preserve that identity.
        keys = {(r['annotator_id'], r['pull_session_id']) for r in records}
        assert len(keys) == 10

    def test_large_jsonl_record_still_atomic(
        self,
        stores_dir_factory,
    ):
        """Craft records well beyond PIPE_BUF (200 issues @ ~120 bytes each
        ≈ 24 KB per line) and append 4 concurrently.

        The current implementation has no explicit lock around the JSONL
        append; if the POSIX O_APPEND guarantee breaks above PIPE_BUF,
        lines may interleave.  This test documents the current behavior.
        A failure motivates adding an explicit ``meta_lock``.
        """
        store_names = [f'large{i:02d}' for i in range(4)]
        stores_dir = stores_dir_factory(tuple(store_names), with_annotations=True)

        ann_path = 'annotations/alice-xyz45678/inner-ear-structures-20260101-ab12/data'

        def _big_issues(tag: str) -> list[dict[str, str]]:
            return [
                {
                    'severity': 'warning',
                    'message': f'[{tag}] ' + ('x' * 120),
                }
                for _ in range(200)
            ]

        targets = [
            {
                'stores_dir': stores_dir,
                'store_name': name,
                'annotation_path': ann_path,
                'annotator_id': f'big{i}',
                'machine_id': 'machine-x',
                'nano_id': f'big{i:05d}',
                'pull_session_id': f'session-big-{i}',
                'ontology': 'inner-ear-structures',
                'ontology_version': 1,
                'source_nrrd_checksum': 'sha256:' + '0' * 64,
                'source_file': f'big-{i}.nrrd',
                '_issues_raw': _big_issues(str(i)),
            }
            for i, name in enumerate(store_names)
        ]
        exit_codes = _run_workers_parallel(targets, timeout=120.0)
        assert all(code == 0 for code in exit_codes), exit_codes

        jsonl_path = stores_dir / '.meta' / 'provenance.jsonl'
        raw_lines = [
            ln for ln in jsonl_path.read_text(encoding='utf-8').splitlines() if ln.strip()
        ]
        assert len(raw_lines) == 4, (
            f'expected 4 lines in provenance.jsonl, got {len(raw_lines)} — '
            f'likely torn writes from concurrent large appends; consider '
            f'adding an explicit meta_lock around the JSONL append.'
        )
        for i, line in enumerate(raw_lines, start=1):
            try:
                json.loads(line)
            except json.JSONDecodeError as exc:
                pytest.fail(f'line {i} malformed: {exc!r}')

    def test_jsonl_append_lock_contention_stress(
        self,
        stores_dir_factory,
    ):
        """Regression canary: 100 short concurrent appends.  Not a strict
        correctness test — small records stay under PIPE_BUF and should
        be safe.  Runs as a sanity probe under ``pytest.mark.slow``."""
        store_names = [f'stress{i:03d}' for i in range(100)]
        stores_dir = stores_dir_factory(tuple(store_names), with_annotations=True)

        ann_path = 'annotations/alice-xyz45678/inner-ear-structures-20260101-ab12/data'
        targets = [
            {
                'stores_dir': stores_dir,
                'store_name': name,
                'annotation_path': ann_path,
                'annotator_id': f'stress{i:03d}',
                'machine_id': 'machine-x',
                'nano_id': f's{i:07d}',
                'pull_session_id': f'stress-{i:03d}',
                'ontology': 'inner-ear-structures',
                'ontology_version': 1,
                'source_nrrd_checksum': 'sha256:' + '0' * 64,
                'source_file': f'stress-{i}.nrrd',
            }
            for i, name in enumerate(store_names)
        ]
        exit_codes = _run_workers_parallel(targets, timeout=180.0)
        assert all(code == 0 for code in exit_codes), exit_codes

        records = _parse_jsonl(stores_dir / '.meta' / 'provenance.jsonl')
        assert len(records) == 100


# ===========================================================================
# Annotator isolation
# ===========================================================================


class TestAnnotatorIsolation:
    """Basic multi-annotator concurrency.  Deeper path-traversal and
    security-boundary tests live in the strategic plan §5, not here.
    """

    def test_concurrent_multi_annotator_isolation(
        self,
        stores_dir_factory,
        concurrent_integrate_runner,
        tmp_path,
    ):
        stores_dir = stores_dir_factory(('alpha',))
        staging_a = _build_staging(tmp_path / 'stagings', 'vxhb-staging-a', ['alpha'])
        staging_b = _build_staging(tmp_path / 'stagings', 'vxhb-staging-b', ['alpha'])

        results = concurrent_integrate_runner(
            [
                {
                    'stores_dir': stores_dir,
                    'staging_dir': staging_a,
                    'annotator_id': 'alice',
                    'nano_id': 'aaaa1111',
                },
                {
                    'stores_dir': stores_dir,
                    'staging_dir': staging_b,
                    'annotator_id': 'bob',
                    'nano_id': 'bbbb2222',
                },
            ]
        )
        assert all(r['returncode'] == 0 for r in results)

        ann_root = stores_dir / 'alpha.zarr' / 'annotations'
        alice_dir = ann_root / 'alice-aaaa1111'
        bob_dir = ann_root / 'bob-bbbb2222'
        alice_instances = [p for p in alice_dir.iterdir() if p.is_dir()]
        bob_instances = [p for p in bob_dir.iterdir() if p.is_dir()]
        assert len(alice_instances) == 1
        assert len(bob_instances) == 1

        # JSONL records confirm annotator isolation (each annotator only
        # appears in their own record, never attributed to the other).
        records = _parse_jsonl(stores_dir / '.meta' / 'provenance.jsonl')
        alice_records = [r for r in records if r['annotator_id'] == 'alice']
        bob_records = [r for r in records if r['annotator_id'] == 'bob']
        assert len(alice_records) == 1
        assert len(bob_records) == 1
        assert alice_records[0]['annotation_path'].startswith(
            'annotations/alice-aaaa1111/'
        )
        assert bob_records[0]['annotation_path'].startswith('annotations/bob-bbbb2222/')

    def test_same_annotator_two_pushes_different_instances(
        self,
        stores_dir_factory,
        concurrent_integrate_runner,
        tmp_path,
    ):
        """Same annotator_id + nano_id but distinct staging payloads →
        each push creates a fresh instance directory (because
        ``instance_dir`` embeds a fresh random suffix per run), so both
        coexist under ``annotations/<annotator>-<nano>/``."""
        stores_dir = stores_dir_factory(('alpha',))
        staging_a = _build_staging(
            tmp_path / 'stagings', 'vxhb-staging-same-a', ['alpha']
        )
        staging_b = _build_staging(
            tmp_path / 'stagings', 'vxhb-staging-same-b', ['alpha']
        )

        results = concurrent_integrate_runner(
            [
                {
                    'stores_dir': stores_dir,
                    'staging_dir': staging_a,
                    'annotator_id': 'alice',
                    'nano_id': 'samesame',
                },
                {
                    'stores_dir': stores_dir,
                    'staging_dir': staging_b,
                    'annotator_id': 'alice',
                    'nano_id': 'samesame',
                },
            ]
        )
        assert all(r['returncode'] == 0 for r in results)

        ann_root = stores_dir / 'alpha.zarr' / 'annotations'
        annotator_dirs = [p for p in ann_root.iterdir() if p.is_dir()]
        assert len(annotator_dirs) == 1
        assert annotator_dirs[0].name == 'alice-samesame'

        instance_dirs = [p for p in annotator_dirs[0].iterdir() if p.is_dir()]
        assert len(instance_dirs) == 2

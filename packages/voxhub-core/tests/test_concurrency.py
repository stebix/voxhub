"""Concurrency tests for voxhub-core.

Covers voxhub_core.server.locks.store_lock and multi-process race scenarios
in _run_integrate_annotations.  All concurrency here is cross-process
(multiprocessing or subprocess.Popen), never threading — production runs
each voxhub-server invocation as a fresh SSH-spawned process.

Plan: docs/testing/concurrency-and-provenance.md §4

Each test is currently skipped.  Remove the skip marker as tests are
implemented in a downstream worktree.

Fixtures expected:
    - zarr_root_factory
    - wip_dir_with_manifest
    - concurrent_integrate_runner — launches N subprocesses in parallel
      running voxhub-server integrate-annotations against the same zarr
      root, returns exit codes and parsed JSON outputs
    - held_lock — context manager that acquires a FileLock in a separate
      process and releases it on exit
"""

import pytest

pytestmark = [
    pytest.mark.skip(
        reason='stub — see docs/testing/concurrency-and-provenance.md §4'
    ),
    pytest.mark.slow,
]


# ===========================================================================
# store_lock unit tests
# ===========================================================================


class TestStoreLock:
    """Covers voxhub_core.server.locks.store_lock."""

    def test_returns_filelock_instance(self, tmp_path):
        """store_lock(path) returns a filelock.FileLock instance. Trivial
        but pins the contract."""
        del tmp_path

    def test_lock_file_is_sibling_of_zarr_dir(self, tmp_path):
        """path=/root/foo.zarr → lock file is /root/foo.zarr.lock."""
        del tmp_path

    def test_lock_is_exclusive_same_process(self, tmp_path):
        """Acquire in one thread → second acquisition with short timeout
        raises filelock.Timeout."""
        del tmp_path

    def test_lock_released_on_context_exit(self, tmp_path):
        """with store_lock(p): pass → lock file still exists, but is
        unlocked (next acquirer succeeds)."""
        del tmp_path

    def test_custom_timeout_propagates(self, tmp_path):
        """store_lock(p, timeout=0.1) → acquiring a held lock raises
        within ~0.1s (generous window, e.g. < 1s)."""
        del tmp_path


# ===========================================================================
# Concurrent integrate: same store
# ===========================================================================


class TestConcurrentIntegrateSameStore:
    """Two or more integrate-annotations invocations against the same zarr store.

    Must be serialized by store_lock; both must succeed under independent
    annotator IDs without data loss or partial writes visible to either
    run.
    """

    def test_two_concurrent_writes_to_same_store_serialize(
        self,
        zarr_root_factory,
        wip_dir_with_manifest,
        concurrent_integrate_runner,
    ):
        """Fixture: one zarr store, two WIP dirs with different annotator_id.
        Run two integrate subprocesses in parallel → both exit 0, both
        annotations end up in the store under distinct annotator-scoped
        paths, provenance JSONL has two entries, neither run sees the
        other's partial write."""
        del zarr_root_factory, wip_dir_with_manifest, concurrent_integrate_runner

    def test_concurrent_writes_do_not_deadlock(
        self,
        zarr_root_factory,
        wip_dir_with_manifest,
        concurrent_integrate_runner,
    ):
        """Launch 5 concurrent integrates against the same store → all
        complete within N seconds (parameterize N, target < 30s)."""
        del zarr_root_factory, wip_dir_with_manifest, concurrent_integrate_runner

    def test_lock_contention_respects_timeout(
        self,
        zarr_root_factory,
        wip_dir_with_manifest,
        concurrent_integrate_runner,
        held_lock,
    ):
        """Hold the lock in a separate process for longer than the
        configured timeout → integrate subprocess fails cleanly with a
        timeout error surfaced in its JSON envelope, not a Python
        traceback."""
        del (
            zarr_root_factory,
            wip_dir_with_manifest,
            concurrent_integrate_runner,
            held_lock,
        )

    def test_lock_released_after_crash(
        self,
        zarr_root_factory,
        wip_dir_with_manifest,
        concurrent_integrate_runner,
    ):
        """Launch a subprocess that crashes mid-operation (SIGKILL or
        sys.exit(1)) while holding the lock → a new subprocess can
        acquire the lock afterward. Pins filelock recovery semantics."""
        del zarr_root_factory, wip_dir_with_manifest, concurrent_integrate_runner


# ===========================================================================
# Concurrent integrate: different stores
# ===========================================================================


class TestConcurrentIntegrateDifferentStores:
    """Integrates targeting distinct stores should not serialize."""

    def test_two_different_stores_fully_parallel(
        self,
        zarr_root_factory,
        wip_dir_with_manifest,
        concurrent_integrate_runner,
    ):
        """Two stores, two integrates in parallel → both complete
        concurrently (duration roughly = max(duration_a, duration_b),
        not sum). Each acquires its own store_lock."""
        del zarr_root_factory, wip_dir_with_manifest, concurrent_integrate_runner


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
        zarr_root_factory,
        wip_dir_with_manifest,
        concurrent_integrate_runner,
    ):
        """Two stores under the same zarr_root, two parallel integrates →
        both provenance entries land in .meta/provenance.jsonl, both
        lines parse as valid JSON, no interleaved or truncated lines."""
        del zarr_root_factory, wip_dir_with_manifest, concurrent_integrate_runner

    def test_high_concurrency_jsonl_integrity(
        self,
        zarr_root_factory,
        wip_dir_with_manifest,
        concurrent_integrate_runner,
    ):
        """10 concurrent appends to .meta/provenance.jsonl (distinct stores,
        so store_lock does not serialize them). Assert exactly 10 lines in
        the file, every line parses as valid JSON, every line has a unique
        annotator_id/nano_id combination."""
        del zarr_root_factory, wip_dir_with_manifest, concurrent_integrate_runner

    def test_large_jsonl_record_still_atomic(
        self, zarr_root_factory, wip_dir_with_manifest
    ):
        """Craft a record > 4 KB (e.g. 200 issues attached) → verify that
        concurrent appends with records of this size remain atomic. If
        this test fails, it is the justification for adding an explicit
        lock around the JSONL append in provenance.py."""
        del zarr_root_factory, wip_dir_with_manifest

    def test_jsonl_append_lock_contention_stress(
        self,
        zarr_root_factory,
        wip_dir_with_manifest,
        concurrent_integrate_runner,
    ):
        """100 concurrent short appends. Regression canary, not a strict
        correctness test. Run under pytest.mark.slow (already applied at
        module level)."""
        del zarr_root_factory, wip_dir_with_manifest, concurrent_integrate_runner


# ===========================================================================
# Annotator isolation
# ===========================================================================


class TestAnnotatorIsolation:
    """Basic multi-annotator concurrency. Deeper path-traversal and
    security-boundary tests live in the strategic plan §5, not here.
    """

    def test_concurrent_multi_annotator_isolation(
        self,
        zarr_root_factory,
        wip_dir_with_manifest,
        concurrent_integrate_runner,
    ):
        """Two integrates with different annotator_id → each writes under
        its own annotations/<annotator>-<nano>/ path, neither touches
        the other's attrs."""
        del zarr_root_factory, wip_dir_with_manifest, concurrent_integrate_runner

    def test_same_annotator_two_pushes_different_instances(
        self,
        zarr_root_factory,
        wip_dir_with_manifest,
        concurrent_integrate_runner,
    ):
        """Two integrates with identical annotator_id + nano_id but
        different WIP contents → two separate instance directories
        (instance_dir includes a random suffix), both co-exist."""
        del zarr_root_factory, wip_dir_with_manifest, concurrent_integrate_runner

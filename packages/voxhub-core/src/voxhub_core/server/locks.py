"""Filelocks for concurrent push safety.

Uses ``filelock`` to serialize writes to individual zarr stores
(:func:`store_lock`) and to the shared provenance JSONL index
(:func:`provenance_lock`).  Store lock files live alongside the ``.zarr``
directories; the provenance lock lives in ``<stores_dir>/.meta``.
"""

from pathlib import Path

from filelock import FileLock


def store_lock(zarr_path: Path, *, timeout: float = 60) -> FileLock:
    """Create a filelock for a zarr store.

    Parameters
    ----------
    zarr_path : Path
        Path to the ``.zarr`` directory.
    timeout : float
        Lock acquisition timeout in seconds.

    Returns
    -------
    FileLock
        A ``FileLock`` instance.  Use as a context manager.
    """
    lock_path = zarr_path.with_suffix('.zarr.lock')
    return FileLock(str(lock_path), timeout=timeout)


def provenance_lock(stores_dir: Path, *, timeout: float = 10) -> FileLock:
    """Create a filelock guarding appends to the shared provenance JSONL.

    The central ``<stores_dir>/.meta/provenance.jsonl`` index is appended to
    across *every* zarr store, so the per-store :func:`store_lock` does not
    serialize these writes.  POSIX ``O_APPEND`` atomicity is only guaranteed
    for writes under ``PIPE_BUF`` (and not at all on some network
    filesystems), so a record can tear when concurrent appends interleave.
    This lock serializes the append instead.

    The caller must ensure ``<stores_dir>/.meta`` exists before acquiring the
    lock (the lock file is created inside it), mirroring :func:`store_lock`'s
    reliance on the ``.zarr`` directory already existing.

    Parameters
    ----------
    stores_dir : Path
        Directory containing the zarr stores.
    timeout : float
        Lock acquisition timeout in seconds.  Short by default: the guarded
        critical section is a single line append plus ``fsync``.

    Returns
    -------
    FileLock
        A ``FileLock`` instance.  Use as a context manager.
    """
    lock_path = stores_dir / '.meta' / 'provenance.jsonl.lock'
    return FileLock(str(lock_path), timeout=timeout)

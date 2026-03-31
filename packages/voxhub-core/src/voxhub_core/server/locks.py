"""Per-store filelock for concurrent push safety.

Uses ``filelock`` to serialize writes to individual zarr stores.
Lock files live alongside the ``.zarr`` directories.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from filelock import FileLock

if TYPE_CHECKING:
    from pathlib import Path


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

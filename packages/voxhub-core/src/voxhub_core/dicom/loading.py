"""DICOM slice loading and volume actualization.

Reads DICOM files, applies the modality LUT, computes geometry,
and stacks slices into 3D volumes.  Supports parallel loading via
a thread pool.
"""

from __future__ import annotations

import os
import queue
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, Any

import numpy as np
import pydicom
from tqdm.auto import tqdm

from .geometry import compute_slice_geometry
from .types import ActualizedDicomTree, DicomTree, DicomVolume

if TYPE_CHECKING:
    from pathlib import Path

    from numpy.typing import NDArray


def _dataset_to_dict(ds: pydicom.Dataset) -> dict[str, Any]:
    """Convert a pydicom Dataset to a plain dict, excluding pixel data."""
    result: dict[str, Any] = {}
    for elem in ds:
        if elem.tag.group == 0x7FE0:
            continue
        key = elem.keyword or str(elem.tag)
        if elem.VR == 'SQ':
            result[key] = [_dataset_to_dict(item) for item in elem.value]
        elif isinstance(elem.value, pydicom.valuerep.PersonName):
            result[key] = str(elem.value)
        elif isinstance(elem.value, pydicom.multival.MultiValue):
            result[key] = [
                str(v) if isinstance(v, pydicom.uid.UID) else v for v in elem.value
            ]
        elif isinstance(elem.value, pydicom.uid.UID):
            result[key] = str(elem.value)
        else:
            result[key] = elem.value
    return result


def _collect_leaves(
    tree: DicomTree,
    prefix: tuple[str, ...] = (),
) -> list[tuple[tuple[str, ...], list[Path]]]:
    """Collect all leaf entries from a DicomTree with their key paths."""
    leaves: list[tuple[tuple[str, ...], list[Path]]] = []
    for key, value in tree.items():
        key_path = (*prefix, key)
        if isinstance(value, list):
            leaves.append((key_path, value))
        else:
            leaves.extend(_collect_leaves(value, key_path))
    return leaves


def _reassemble_tree(
    results: dict[tuple[str, ...], DicomVolume],
) -> ActualizedDicomTree:
    """Reassemble flat results into a nested ActualizedDicomTree."""
    root: ActualizedDicomTree = {}
    for key_path, volume in results.items():
        node = root
        for key in key_path[:-1]:
            if key not in node:
                node[key] = {}
            node = node[key]  # type: ignore[assignment]
        node[key_path[-1]] = volume
    return root


def load_dicom_directory(
    paths: list[Path],
    *,
    verbose: bool = False,
) -> DicomVolume:
    """Load DICOM slices into a 3D volume with the modality LUT applied.

    Parameters
    ----------
    paths : list[Path]
        Sorted ``.dcm`` file paths.
    verbose : bool
        Print progress information.

    Returns
    -------
    DicomVolume
        Volume array of shape ``(slices, rows, cols)`` with metadata.
    """
    datasets: list[pydicom.Dataset] = [pydicom.dcmread(p) for p in paths]

    sort_indices, geom_metadata = compute_slice_geometry(datasets)
    datasets = [datasets[i] for i in sort_indices]

    slices: list[NDArray[Any]] = [
        pydicom.pixels.apply_modality_lut(ds.pixel_array, ds) for ds in datasets
    ]
    volume = np.stack(slices, axis=0)
    metadata = _dataset_to_dict(datasets[0])
    metadata.update(geom_metadata)

    if verbose:
        print(
            f'Loaded volume: shape={volume.shape}, '
            f'dtype={volume.dtype}, '
            f'range=[{volume.min():.1f}, {volume.max():.1f}]'
        )

    return DicomVolume(volume=volume, metadata=metadata)


def actualize(
    tree: DicomTree,
    *,
    max_workers: int | None = None,
) -> ActualizedDicomTree:
    """Load all DICOM directories in a tree into DicomVolume objects.

    Leaf directories are loaded in parallel using a thread pool.

    Parameters
    ----------
    tree : DicomTree
        Parsed DICOM tree from :func:`parse_dicom_tree`.
    max_workers : int | None
        Maximum number of threads.  ``None`` uses a default based on
        CPU count.

    Returns
    -------
    ActualizedDicomTree
        Nested dict with ``DicomVolume`` leaves.
    """
    leaves = _collect_leaves(tree)

    if max_workers is None:
        effective_workers = min(32, (os.cpu_count() or 1) + 4)
    else:
        effective_workers = max_workers
    effective_workers = min(effective_workers, len(leaves))

    positions: queue.SimpleQueue[int] = queue.SimpleQueue()
    for i in range(1, effective_workers + 1):
        positions.put(i)

    def _load_with_progress(
        key_path: tuple[str, ...],
        paths: list[Path],
    ) -> DicomVolume:
        pos = positions.get()
        try:
            bar = tqdm(
                total=len(paths),
                desc=f'  {key_path[-1]}',
                unit='slice',
                position=pos,
                leave=False,
            )

            datasets: list[pydicom.Dataset] = []
            slices: list[NDArray[Any]] = []

            for p in paths:
                dset = pydicom.dcmread(p)
                slc = pydicom.pixels.apply_modality_lut(dset.pixel_array, dset)
                datasets.append(dset)
                slices.append(slc)
                bar.update(1)
            assert datasets

            bar.set_postfix_str('Computing geometry...')
            sort_indices, geom_metadata = compute_slice_geometry(datasets)
            datasets = [datasets[i] for i in sort_indices]
            slices = [slices[i] for i in sort_indices]

            bar.set_postfix_str('Stacking volume...')
            volume = np.stack(slices, axis=0)
            metadata = _dataset_to_dict(datasets[0])
            metadata.update(geom_metadata)
            bar.close()
            return DicomVolume(volume=volume, metadata=metadata)
        finally:
            positions.put(pos)

    results: dict[tuple[str, ...], DicomVolume] = {}
    with ThreadPoolExecutor(max_workers=effective_workers) as executor:
        future_to_key = {
            executor.submit(_load_with_progress, key_path, paths): key_path
            for key_path, paths in leaves
        }
        with tqdm(
            total=len(leaves),
            desc='Directories',
            unit='dir',
            position=0,
        ) as dir_bar:
            for future in as_completed(future_to_key):
                key_path = future_to_key[future]
                results[key_path] = future.result()
                dir_bar.update(1)

    return _reassemble_tree(results)

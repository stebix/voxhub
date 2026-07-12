"""Zarr v3 export pipeline for DICOM volumes.

Provides serial and parallel export of DICOM volumes to zarr stores
with human-readable names (e.g. ``gallivanting-groundhog.zarr``).
"""

import json
import os
import random
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import attrs
import numpy as np
import pydicom
import zarr
from tqdm.auto import tqdm

from voxhub_core.attributes import dataset_attributes_to_dict
from voxhub_core.dicom.geometry import compute_slice_geometry
from voxhub_core.dicom.loading import _collect_leaves, _dataset_to_dict
from voxhub_core.dicom.types import (
    ActualizedDicomTree,
    DicomTree,
    DicomVolume,
)
from voxhub_core.naming import generate_unique_names
from voxhub_schema import DatasetAttributes


@attrs.define
class ExportTask:
    """Lightweight task sent from main process to worker."""

    key: str
    dicom_paths: list[Path]
    output_path: Path
    name: str
    source_directory: str
    series_directory: str | None = None


@attrs.define
class ExportResult:
    """Lightweight result returned from worker to main process."""

    key: str
    name: str
    shape: tuple[int, ...]
    error: str | None = None


def _sort_key(k: str) -> tuple[int, int, str]:
    """Sort key: integer strings numerically, others lexicographically."""
    try:
        return (0, int(k), k)
    except ValueError:
        return (1, 0, k)


def _sanitize_for_json(obj: object) -> object:
    """Recursively convert non-JSON-serializable types."""
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_for_json(v) for v in obj]
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, bytes):
        try:
            return obj.decode('utf-8')
        except UnicodeDecodeError:
            return obj.hex()
    if isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    return str(obj)


def export_zarr(
    volume: DicomVolume,
    path: str | Path,
    *,
    force_write: bool = False,
    dataset_attributes: DatasetAttributes | None = None,
) -> None:
    """Export a DicomVolume to a zarr v3 store.

    Creates ``<path>/raw/full`` with volume data and metadata attrs.

    Parameters
    ----------
    volume : DicomVolume
        The volume to export.
    path : str | Path
        Output ``.zarr`` directory.
    force_write : bool
        Overwrite if the path already exists.
    dataset_attributes : DatasetAttributes | None
        Optional dataset attributes to write to root group attrs.

    Raises
    ------
    FileExistsError
        If *path* exists and *force_write* is False.
    """
    path = Path(path)
    if path.exists() and not force_write:
        msg = f"'{path}' already exists. Pass force_write=True to overwrite."
        raise FileExistsError(msg)
    root = zarr.open_group(path, mode='w', zarr_format=3)
    raw = root.create_group('raw')
    arr = raw.create_array('full', data=volume.volume)
    arr.update_attributes(_sanitize_for_json(volume.metadata))  # type: ignore[arg-type]

    if dataset_attributes is not None:
        root.update_attributes(
            {'dataset_attributes': dataset_attributes_to_dict(dataset_attributes)}
        )


def flatten_to_volumes(
    tree: ActualizedDicomTree,
) -> dict[str, DicomVolume]:
    """Flatten an ActualizedDicomTree to ``{directory_id: DicomVolume}``.

    Accepts two layouts:

    - **Nested** (2-level): each parent must contain exactly one series.
    - **Flat** (1-level): ``{directory_name: DicomVolume}``.

    Parameters
    ----------
    tree : ActualizedDicomTree
        Loaded DICOM tree.

    Returns
    -------
    dict[str, DicomVolume]
        Flat mapping of directory ids to volumes.

    Raises
    ------
    ValueError
        If the tree has mixed layout.
    """
    if not tree:
        return {}

    flat_entries = {k: v for k, v in tree.items() if isinstance(v, DicomVolume)}
    nested_entries = {k: v for k, v in tree.items() if not isinstance(v, DicomVolume)}

    if flat_entries and nested_entries:
        msg = (
            'Mixed tree layout: some top-level entries are DicomVolumes '
            'and others are nested dicts.'
        )
        raise ValueError(msg)

    result: dict[str, DicomVolume] = {}

    if flat_entries:
        for key, volume in flat_entries.items():
            augmented = {**volume.metadata, 'source_directory': key}
            result[key] = DicomVolume(volume=volume.volume, metadata=augmented)
        return result

    for parent_key, subtree in nested_entries.items():
        assert isinstance(subtree, dict)
        if len(subtree) != 1:
            msg = f"Expected exactly 1 series under '{parent_key}', got {len(subtree)}"
            raise ValueError(msg)
        series_name, volume = next(iter(subtree.items()))
        if not isinstance(volume, DicomVolume):
            msg = f"Expected DicomVolume under '{parent_key}/{series_name}'"
            raise ValueError(msg)
        augmented = {
            **volume.metadata,
            'source_directory': parent_key,
            'series_directory': series_name,
        }
        result[parent_key] = DicomVolume(volume=volume.volume, metadata=augmented)
    return result


def _check_conflicts(output_dir: Path, names: list[str]) -> None:
    """Raise if any target store path or the manifest already exists.

    Parameters
    ----------
    output_dir : Path
        Parent directory for the ``<name>.zarr`` stores and ``manifest.json``.
    names : list[str]
        Generated store names (without the ``.zarr`` suffix).

    Raises
    ------
    FileExistsError
        If any ``<name>.zarr`` path or ``manifest.json`` already exists.
    """
    conflicts = [
        str(output_dir / f'{name}.zarr')
        for name in names
        if (output_dir / f'{name}.zarr').exists()
    ]
    manifest_path = output_dir / 'manifest.json'
    if manifest_path.exists():
        conflicts.append(str(manifest_path))
    if conflicts:
        msg = (
            'Refusing to overwrite existing paths:\n'
            + '\n'.join(f'  - {c}' for c in conflicts)
            + '\nPass force_write=True to overwrite.'
        )
        raise FileExistsError(msg)


def _write_manifest(output_dir: Path, mapping: dict[str, str]) -> None:
    """Write ``mapping`` to ``<output_dir>/manifest.json`` as indented JSON."""
    manifest_path = output_dir / 'manifest.json'
    manifest_path.write_text(json.dumps(mapping, indent=2))


def export_zarr_collection(
    volumes: dict[str, DicomVolume],
    output_dir: str | Path,
    *,
    seed: int | None = None,
    force_write: bool = False,
) -> dict[str, str]:
    """Export a collection of DicomVolumes to individual zarr stores.

    Parameters
    ----------
    volumes : dict[str, DicomVolume]
        Flat mapping of directory ids to volumes.
    output_dir : str | Path
        Parent directory for all outputs.
    seed : int | None
        RNG seed for reproducible name generation.
    force_write : bool
        Overwrite existing output paths.

    Returns
    -------
    dict[str, str]
        Mapping of ``{directory_id: generated_name}``.
    """
    output_dir = Path(output_dir)

    rng = random.Random(seed)
    sorted_keys = sorted(volumes, key=_sort_key)
    names = generate_unique_names(len(sorted_keys), rng=rng)
    mapping: dict[str, str] = dict(zip(sorted_keys, names, strict=False))

    if not force_write:
        _check_conflicts(output_dir, names)

    output_dir.mkdir(parents=True, exist_ok=True)

    for key in sorted_keys:
        export_zarr(
            volumes[key],
            output_dir / f'{mapping[key]}.zarr',
            force_write=force_write,
        )

    _write_manifest(output_dir, mapping)
    return mapping


# -- Parallel export ---------------------------------------------------------


def _worker_fn(task: ExportTask) -> ExportResult:
    """Worker: load DICOMs, apply LUT, write zarr.

    Returns an :class:`ExportResult` in all cases; failures are reported via
    its ``error`` field rather than raised, so the parent can aggregate them.
    """
    try:
        datasets = [pydicom.dcmread(p) for p in task.dicom_paths]
        assert datasets

        sort_indices, geom_metadata = compute_slice_geometry(datasets)
        datasets = [datasets[i] for i in sort_indices]

        slices = [
            pydicom.pixels.apply_modality_lut(ds.pixel_array, ds) for ds in datasets
        ]
        volume = np.stack(slices, axis=0)
        metadata = _dataset_to_dict(datasets[0])
        metadata.update(geom_metadata)
        metadata['source_directory'] = task.source_directory
        if task.series_directory is not None:
            metadata['series_directory'] = task.series_directory

        export_zarr(
            DicomVolume(volume=volume, metadata=metadata),
            task.output_path,
            force_write=True,
        )
        return ExportResult(key=task.key, name=task.name, shape=volume.shape)
    except Exception as exc:
        return ExportResult(key=task.key, name=task.name, shape=(), error=str(exc))


def _build_tasks_from_tree(
    tree: DicomTree,
    output_dir: Path,
    key_to_name: dict[str, str],
) -> list[ExportTask]:
    """Build ExportTask list from a parsed DicomTree."""
    leaves = _collect_leaves(tree)
    is_nested = any(len(kp) == 2 for kp, _ in leaves)

    tasks: list[ExportTask] = []
    for key_path, paths in leaves:
        if is_nested:
            key = key_path[0]
            series_dir = key_path[1] if len(key_path) > 1 else None
        else:
            key = key_path[0]
            series_dir = None

        if key not in key_to_name:
            continue

        tasks.append(
            ExportTask(
                key=key,
                dicom_paths=paths,
                output_path=output_dir / f'{key_to_name[key]}.zarr',
                name=key_to_name[key],
                source_directory=key,
                series_directory=series_dir,
            )
        )
    return tasks


def export_zarr_collection_parallel(
    tree: DicomTree,
    output_dir: str | Path,
    *,
    seed: int | None = None,
    force_write: bool = False,
    max_workers: int | None = None,
    verbose: bool = False,
) -> dict[str, str]:
    """Load + export DICOM tree to zarr in parallel processes.

    Parameters
    ----------
    tree : DicomTree
        Parsed (non-actualized) DICOM tree.
    output_dir : str | Path
        Parent directory for zarr stores.
    seed : int | None
        RNG seed for reproducible name generation.
    force_write : bool
        Overwrite existing output paths.
    max_workers : int | None
        Max parallel worker processes.
    verbose : bool
        Print per-volume shape info.

    Returns
    -------
    dict[str, str]
        Mapping of ``{directory_id: generated_name}``.
    """
    output_dir = Path(output_dir)

    leaves = _collect_leaves(tree)
    if not leaves:
        return {}

    top_keys = list(dict.fromkeys(kp[0] for kp, _ in leaves))
    sorted_keys = sorted(top_keys, key=_sort_key)

    rng = random.Random(seed)
    names = generate_unique_names(len(sorted_keys), rng=rng)
    key_to_name: dict[str, str] = dict(zip(sorted_keys, names, strict=False))

    if not force_write:
        _check_conflicts(output_dir, names)

    output_dir.mkdir(parents=True, exist_ok=True)

    tasks = _build_tasks_from_tree(tree, output_dir, key_to_name)

    if max_workers is None:
        effective_workers = min(32, os.cpu_count() or 1)
    else:
        effective_workers = max_workers
    effective_workers = min(effective_workers, len(tasks))

    results: list[ExportResult] = []
    errors: list[ExportResult] = []

    # Advance a single per-store progress bar as each future completes.  We do
    # not plumb per-slice progress across processes — that requires sharing a
    # queue with the workers, which is not picklable and previously deadlocked.
    with ProcessPoolExecutor(max_workers=effective_workers) as executor:
        futures = [executor.submit(_worker_fn, task) for task in tasks]
        with tqdm(total=len(tasks), desc='Directories', unit='dir') as dir_bar:
            for future in as_completed(futures):
                result = future.result()
                if result.error:
                    errors.append(result)
                else:
                    results.append(result)
                dir_bar.update(1)

    if errors:
        error_msgs = '\n'.join(f'  {e.key}: {e.error}' for e in errors)
        msg = f'Failed to export {len(errors)} volume(s):\n{error_msgs}'
        raise RuntimeError(msg)

    mapping: dict[str, str] = {key: key_to_name[key] for key in sorted_keys}

    if verbose:
        for r in sorted(results, key=lambda r: _sort_key(r.key)):
            print(f'  {r.key}: shape={r.shape}')

    _write_manifest(output_dir, mapping)
    return mapping

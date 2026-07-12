"""Tests for the DICOM-to-zarr export pipeline.

Focuses on the parallel export path: a regression guard for the historical
``multiprocessing.Queue`` deadlock (every parallel run happens in a child
process under a hard deadline) and its equivalence to the serial path.
"""

import json
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import numpy as np
import pytest
import zarr
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

from voxhub_core.dicom import actualize, parse_dicom_tree
from voxhub_core.export import export_zarr_collection, flatten_to_volumes

# A regression re-introducing cross-process Queue plumbing would hang the
# parallel export forever, so every parallel run in this module executes in a
# child process bounded by this wall-clock deadline.
_DEADLINE_S = 60.0

_ROWS = 4
_COLUMNS = 5


def _write_slice(path: Path, z_index: int, series_uid: str) -> None:
    """Write one minimal, readable CT ``.dcm`` slice at ``path``."""
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = CTImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = FileDataset(str(path), {}, file_meta=meta, preamble=b'\0' * 128)
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SeriesInstanceUID = series_uid
    ds.StudyInstanceUID = series_uid
    ds.Modality = 'CT'
    ds.Rows = _ROWS
    ds.Columns = _COLUMNS
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = 'MONOCHROME2'
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.RescaleSlope = 1
    ds.RescaleIntercept = 0
    ds.ImageOrientationPatient = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
    ds.ImagePositionPatient = [0.0, 0.0, float(z_index)]
    ds.PixelSpacing = [0.5, 0.5]

    rng = np.random.default_rng(z_index)
    pixels = rng.integers(0, 1000, size=(_ROWS, _COLUMNS)).astype(np.uint16)
    ds.PixelData = pixels.tobytes()
    ds.save_as(str(path), enforce_file_format=True)


def _build_dicom_collection(
    root: Path,
    *,
    n_series: int = 3,
    n_slices: int = 3,
) -> Path:
    """Build a flat DICOM tree: ``root/series<N>/<slice>.dcm``.

    Each series is a contiguous, spatially-sorted stack so both the serial and
    parallel export paths accept it and produce identical volumes.
    """
    for s in range(n_series):
        series_dir = root / f'series{s}'
        series_dir.mkdir(parents=True)
        series_uid = generate_uid()
        for z in range(n_slices):
            _write_slice(series_dir / f'{z}.dcm', z, series_uid)
    return root


# Runs ``export_zarr_collection_parallel`` in a fresh interpreter so a deadlock
# cannot wedge the pytest process and the whole tree can be killed by group.
_CHILD_PROGRAM = textwrap.dedent(
    """
    import sys
    from pathlib import Path

    from voxhub_core.dicom import parse_dicom_tree
    from voxhub_core.export import export_zarr_collection_parallel

    dicom_dir, out_dir, seed, max_workers = sys.argv[1:5]
    tree = parse_dicom_tree(Path(dicom_dir))
    export_zarr_collection_parallel(
        tree,
        Path(out_dir),
        seed=int(seed),
        max_workers=int(max_workers),
    )
    """
)


def _run_parallel_guarded(
    dicom_dir: Path,
    out_dir: Path,
    *,
    seed: int,
    max_workers: int,
) -> None:
    """Run the parallel export in a subprocess bounded by ``_DEADLINE_S``.

    Fails the test if the export does not finish in time (the historical
    deadlock signature) or the child exits non-zero.  ``start_new_session``
    makes the child a process-group leader so a deadlocked run — including any
    leaked ``ProcessPoolExecutor`` workers — is killed as a group.
    """
    with subprocess.Popen(
        [
            sys.executable,
            '-c',
            _CHILD_PROGRAM,
            str(dicom_dir),
            str(out_dir),
            str(seed),
            str(max_workers),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    ) as proc:
        try:
            out, _ = proc.communicate(timeout=_DEADLINE_S)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=5.0)
            pytest.fail(
                f'export_zarr_collection_parallel did not finish within '
                f'{_DEADLINE_S:.0f}s — parallel export deadlocked'
            )

    assert proc.returncode == 0, f'parallel export child failed:\n{out}'


def _read_full_array(store_path: Path) -> np.ndarray:
    """Read the ``raw/full`` array from a zarr store as a numpy array."""
    root = zarr.open_group(store_path, mode='r')
    return root['raw']['full'][:]


@pytest.mark.slow
def test_parallel_export_completes_without_deadlock(tmp_path: Path) -> None:
    """The default parallel path finishes and writes every store + manifest."""
    dicom_dir = _build_dicom_collection(tmp_path / 'dicom')
    out_dir = tmp_path / 'parallel'

    _run_parallel_guarded(dicom_dir, out_dir, seed=7, max_workers=2)

    manifest = json.loads((out_dir / 'manifest.json').read_text())
    assert len(manifest) == 3
    for name in manifest.values():
        assert (out_dir / f'{name}.zarr').exists()


@pytest.mark.slow
def test_parallel_matches_serial_output(tmp_path: Path) -> None:
    """Parallel and serial paths yield identical manifests and volumes."""
    dicom_dir = _build_dicom_collection(tmp_path / 'dicom')
    seed = 13

    # Serial reference path (mirrors ``voxhub export --no-parallel``).
    serial_dir = tmp_path / 'serial'
    tree = parse_dicom_tree(dicom_dir)
    volumes = flatten_to_volumes(actualize(tree))
    export_zarr_collection(volumes, serial_dir, seed=seed)

    # Parallel path, under the deadlock guard.
    parallel_dir = tmp_path / 'parallel'
    _run_parallel_guarded(dicom_dir, parallel_dir, seed=seed, max_workers=2)

    # Byte-identical manifest.
    serial_manifest = (serial_dir / 'manifest.json').read_text()
    parallel_manifest = (parallel_dir / 'manifest.json').read_text()
    assert serial_manifest == parallel_manifest

    # Identical raw volume for every store.
    mapping = json.loads(serial_manifest)
    assert mapping
    for name in mapping.values():
        serial_arr = _read_full_array(serial_dir / f'{name}.zarr')
        parallel_arr = _read_full_array(parallel_dir / f'{name}.zarr')
        np.testing.assert_array_equal(serial_arr, parallel_arr)

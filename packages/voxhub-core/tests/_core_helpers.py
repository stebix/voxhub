"""Test helpers for voxhub-core — builder functions for test data."""

import json
from pathlib import Path

import nrrd
import numpy as np
import zarr

# Canonical small volume geometry used across tests.
SHAPE: tuple[int, int, int] = (10, 12, 14)
ORIGIN_LPS: list[float] = [-5.0, -6.0, -7.0]
SPACING_MM: list[float] = [0.5, 0.5, 0.5]
# NB: order is [slice(z), col(y), row(x)] — matches what
# extract_spatial_metadata computes from DICOM attrs.
SPACE_DIRECTIONS: list[list[float]] = [
    [0.0, 0.0, 0.5],
    [0.0, 0.5, 0.0],
    [0.5, 0.0, 0.0],
]


def create_zarr_store(
    store_path: Path,
    *,
    shape: tuple[int, ...] = SHAPE,
    dtype: str = 'float32',
) -> Path:
    """Create a minimal zarr v3 store with ``raw/full`` and spatial attrs."""
    root = zarr.open_group(store_path, mode='w')
    raw = root.create_group('raw')
    data = np.random.default_rng(42).standard_normal(shape).astype(dtype)
    arr = raw.create_array('full', data=data)

    arr.update_attributes(
        {
            'ImagePositionPatient': ORIGIN_LPS,
            'ImageOrientationPatient': [1.0, 0.0, 0.0, 0.0, 1.0, 0.0],
            'PixelSpacing': [SPACING_MM[0], SPACING_MM[1]],
            'computed_slice_spacing_mm': SPACING_MM[2],
        }
    )
    return store_path


def write_seg_nrrd(
    path: Path,
    label_map: np.ndarray,
    segments: list[dict[str, object]],
    *,
    origin: list[float] | None = None,
    space_directions: list[list[float]] | None = None,
) -> Path:
    """Write a ``.seg.nrrd`` with Slicer segment headers."""
    origin = origin or ORIGIN_LPS
    space_directions = space_directions or SPACE_DIRECTIONS

    header: dict[str, object] = {
        'space': 'left-posterior-superior',
        'space origin': origin,
        'space directions': space_directions,
        'kinds': ['domain', 'domain', 'domain'],
    }
    for i, seg in enumerate(segments):
        header[f'Segment{i}_ID'] = seg.get('id', f'seg_{i}')
        header[f'Segment{i}_Name'] = seg['name']
        header[f'Segment{i}_LabelValue'] = str(seg['label_value'])
        header[f'Segment{i}_Color'] = seg.get('color', '0.5 0.5 0.5')

    nrrd.write(str(path), label_map, header)
    return path


def write_mrk_json(
    path: Path,
    points: list[list[float]],
    labels: list[str],
    coordinate_system: str = 'LPS',
) -> Path:
    """Write a ``.mrk.json`` in Slicer Markups format."""
    control_points = [
        {'id': str(i), 'label': label, 'position': pt}
        for i, (label, pt) in enumerate(zip(labels, points, strict=False))
    ]
    markup = {
        'markups': [
            {
                'type': 'Fiducial',
                'coordinateSystem': coordinate_system,
                'coordinateUnits': 'mm',
                'controlPoints': control_points,
            }
        ],
    }
    path.write_text(json.dumps(markup, indent=2))
    return path


def build_wip_dir(
    root: Path,
    store_name: str,
    *,
    seg_label_map: np.ndarray | None = None,
    seg_segments: list[dict[str, object]] | None = None,
    lmk_points: list[list[float]] | None = None,
    lmk_labels: list[str] | None = None,
    lmk_coordinate_system: str = 'LPS',
) -> Path:
    """Build a WIP directory mirroring what ``stage()`` would produce."""
    store_dir = root / store_name
    store_dir.mkdir(parents=True, exist_ok=True)

    if seg_label_map is not None:
        write_seg_nrrd(
            store_dir / 'segmentation.seg.nrrd',
            seg_label_map,
            seg_segments or [],
        )

    if lmk_points is not None:
        write_mrk_json(
            store_dir / 'landmarks.mrk.json',
            lmk_points,
            lmk_labels or [],
            lmk_coordinate_system,
        )

    return root

"""Helpers for end-to-end workflow tests.

Creates minimal zarr stores and annotation files that exercise
the full pipeline without DICOM input or SSH transport.
"""

import json
from pathlib import Path

import nrrd
import numpy as np
import zarr

# Canonical geometry matching what extract_spatial_metadata produces
# from the DICOM attrs below.  Axis order: [slice(z), col(y), row(x)].
SHAPE: tuple[int, int, int] = (10, 12, 14)
ORIGIN_LPS: list[float] = [-5.0, -6.0, -7.0]
SPACING_MM: list[float] = [0.5, 0.5, 0.5]
SPACE_DIRECTIONS: list[list[float]] = [
    [0.0, 0.0, 0.5],
    [0.0, 0.5, 0.0],
    [0.5, 0.0, 0.0],
]

# Minimal DICOM attrs that produce the geometry above.
DICOM_ATTRS: dict[str, object] = {
    'ImagePositionPatient': ORIGIN_LPS,
    'ImageOrientationPatient': [1.0, 0.0, 0.0, 0.0, 1.0, 0.0],
    'PixelSpacing': [SPACING_MM[0], SPACING_MM[1]],
    'computed_slice_spacing_mm': SPACING_MM[2],
}


def create_zarr_store(
    store_path: Path,
    *,
    shape: tuple[int, ...] = SHAPE,
    dtype: str = 'float32',
) -> Path:
    """Create a minimal zarr store with ``raw/full`` and DICOM attrs."""
    root = zarr.open_group(store_path, mode='w')
    raw = root.create_group('raw')
    data = np.random.default_rng(42).standard_normal(shape).astype(dtype)
    arr = raw.create_array('full', data=data)
    arr.update_attributes(dict(DICOM_ATTRS))
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

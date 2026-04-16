"""Test helpers for voxhub-core — builder functions for test data."""

import json
from pathlib import Path
from typing import Any

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
            'spacing_mm': list(SPACING_MM),
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


# -- Canonical annotation payloads ------------------------------------------

_DEFAULT_SEG_SEGMENTS: list[dict[str, object]] = [
    {'id': 's0', 'name': 'cochlea', 'label_value': 1, 'color': '1 0 0'},
    {'id': 's1', 'name': 'vestibule', 'label_value': 2, 'color': '0 1 0'},
    {
        'id': 's2',
        'name': 'semicircular_canals',
        'label_value': 3,
        'color': '0 0 1',
    },
]


def default_seg_label_map() -> np.ndarray:
    """A canonical label map aligned with the inner-ear ontology (labels 1-3)."""
    lm = np.zeros(SHAPE, dtype=np.int16)
    lm[0, 0, 0] = 1
    lm[1, 1, 1] = 2
    lm[2, 2, 2] = 3
    return lm


def default_lmk_points() -> list[list[float]]:
    return [[-4.0, -5.0, -6.0], [-3.0, -4.0, -5.0], [-2.0, -3.0, -4.0]]


def default_lmk_labels() -> list[str]:
    return ['round_window', 'oval_window', 'cochlear_apex']


def build_staging_dir(
    root: Path,
    store_name: str,
    *,
    seg_label_map: np.ndarray | None = None,
    seg_segments: list[dict[str, object]] | None = None,
    lmk_points: list[list[float]] | None = None,
    lmk_labels: list[str] | None = None,
    lmk_coordinate_system: str = 'LPS',
) -> Path:
    """Build a staging directory mirroring what ``stage()`` would produce."""
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


def build_staging_dir_entries(
    store_dir: Path,
    *,
    include_seg: bool = True,
    include_lmk: bool = False,
    seg_label_map: np.ndarray | None = None,
    seg_segments: list[dict[str, object]] | None = None,
    lmk_points: list[list[float]] | None = None,
    lmk_labels: list[str] | None = None,
    lmk_coordinate_system: str = 'LPS',
) -> Path:
    """Populate a per-store subdirectory inside a staging dir with defaults.

    Unlike ``build_staging_dir`` this takes the per-store path directly, so
    it composes cleanly with multi-store fixtures.
    """
    store_dir.mkdir(parents=True, exist_ok=True)

    if include_seg:
        label_map = (
            seg_label_map if seg_label_map is not None else default_seg_label_map()
        )
        segments = seg_segments if seg_segments is not None else _DEFAULT_SEG_SEGMENTS
        write_seg_nrrd(
            store_dir / 'segmentation.seg.nrrd',
            label_map,
            segments,
        )

    if include_lmk:
        points = lmk_points if lmk_points is not None else default_lmk_points()
        labels = lmk_labels if lmk_labels is not None else default_lmk_labels()
        write_mrk_json(
            store_dir / 'landmarks.mrk.json',
            points,
            labels,
            lmk_coordinate_system,
        )

    return store_dir


# -- Manifest + annotation helpers -------------------------------------------


def populate_store_annotation(
    zarr_path: Path,
    *,
    annotator_id: str = 'alice',
    nano_id: str = 'xyz45678',
    ontology: str = 'inner-ear-structures',
    ontology_version: int = 1,
    date_str: str = '20260101',
    short_random: str = 'ab12',
    integrated_at: str = '2026-01-01T00:00:00+00:00',
    kind: str = 'segmentation',
    segments: list[dict[str, Any]] | None = None,
    labels: list[str] | None = None,
    omit_ontology_attr: bool = False,
) -> str:
    """Attach a synthetic annotation to an existing zarr store.

    Writes a small array at
    ``annotations/<annotator_id>-<nano_id>/<ontology>-<date>-<rand>/data``
    with canonical provenance attrs.  Returns the annotation path.

    Parameters
    ----------
    segments : list[dict] | None
        Segment records (``label_value``, ``name``, ...) written into the
        array's ``segments`` attribute so downstream audit code sees a
        segmentation annotation.
    labels : list[str] | None
        Landmark labels written into the array's ``labels`` attribute so
        downstream audit code sees a landmark annotation.
    omit_ontology_attr : bool
        If True, skip writing the ``ontology`` / ``ontology_version`` attrs
        to simulate a malformed legacy annotation.
    """
    annotator_dir = f'{annotator_id}-{nano_id}'
    instance_dir = f'{ontology}-{date_str}-{short_random}'
    ann_path = f'annotations/{annotator_dir}/{instance_dir}'

    root = zarr.open_group(zarr_path, mode='r+')
    arr = root.create_array(
        f'{ann_path}/data',
        data=np.zeros(SHAPE, dtype=np.int16),
        overwrite=True,
    )
    attrs_payload: dict[str, Any] = {
        'annotator_id': annotator_id,
        'nano_id': nano_id,
        'integrated_at': integrated_at,
        'kind': kind,
    }
    if not omit_ontology_attr:
        attrs_payload['ontology'] = ontology
        attrs_payload['ontology_version'] = ontology_version
    if segments is not None:
        attrs_payload['segments'] = segments
    if labels is not None:
        attrs_payload['labels'] = labels
    arr.update_attributes(attrs_payload)
    return ann_path

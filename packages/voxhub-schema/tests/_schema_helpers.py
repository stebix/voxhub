"""Test helpers for voxhub-schema — builder functions for test data."""

import json
from pathlib import Path

import nrrd
import numpy as np

# Canonical spatial metadata used across all tests.  Small volumes keep
# tests fast; the exact values are arbitrary but consistent.
SHAPE: tuple[int, int, int] = (10, 12, 14)
ORIGIN_LPS: list[float] = [-5.0, -6.0, -7.0]
SPACING_MM: list[float] = [0.5, 0.5, 0.5]
SPACE_DIRECTIONS: list[list[float]] = [
    [0.5, 0.0, 0.0],
    [0.0, 0.5, 0.0],
    [0.0, 0.0, 0.5],
]


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

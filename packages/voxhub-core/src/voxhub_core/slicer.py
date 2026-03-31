"""Parsers and writers for 3D Slicer annotation formats.

Handles two file formats:

- ``.seg.nrrd`` -- segmentation label maps (integer voxel volumes)
- ``.mrk.json`` -- Slicer Markups JSON (fiducial point landmarks)
"""

import json
from pathlib import Path
from typing import Any

import attrs
import nrrd
import numpy as np


@attrs.define
class Segment:
    """A single segment (label class) from a Slicer segmentation."""

    id: str
    name: str
    label_value: int
    color: tuple[float, float, float]


@attrs.define
class SegmentationData:
    """Parsed segmentation label map with spatial metadata."""

    label_map: np.ndarray
    segments: list[Segment]
    space_origin: np.ndarray
    space_directions: np.ndarray


@attrs.define
class LandmarkData:
    """Parsed landmark fiducial points."""

    points: np.ndarray  # (N, 3) float64
    labels: list[str]
    coordinate_system: str  # 'LPS' or 'RAS'


# -- Parsing -----------------------------------------------------------------


def parse_seg_nrrd(path: str | Path) -> SegmentationData:
    """Parse a 3D Slicer ``.seg.nrrd`` segmentation file.

    Parameters
    ----------
    path : str | Path
        Path to the ``.seg.nrrd`` file.

    Returns
    -------
    SegmentationData

    Raises
    ------
    FileNotFoundError
        If the file does not exist.
    """
    path = Path(path)
    if not path.exists():
        msg = f'Segmentation file not found: {path}'
        raise FileNotFoundError(msg)

    data, header = nrrd.read(str(path))

    space_origin = np.array(header.get('space origin', [0.0, 0.0, 0.0]), dtype=np.float64)
    space_directions = np.array(
        header.get('space directions', np.eye(3)),
        dtype=np.float64,
    )

    segments: list[Segment] = []
    i = 0
    while True:
        name_key = f'Segment{i}_Name'
        if name_key not in header:
            break

        seg_id = header.get(f'Segment{i}_ID', str(i))
        seg_name = header[name_key]
        label_value = int(header.get(f'Segment{i}_LabelValue', i + 1))

        color_raw = header.get(f'Segment{i}_Color', '0.5 0.5 0.5')
        if isinstance(color_raw, str):
            color = tuple(float(c) for c in color_raw.split())
        else:
            color = tuple(float(c) for c in color_raw[:3])

        segments.append(
            Segment(
                id=seg_id,
                name=seg_name,
                label_value=label_value,
                color=color,  # type: ignore[arg-type]
            )
        )
        i += 1

    return SegmentationData(
        label_map=data,
        segments=segments,
        space_origin=space_origin,
        space_directions=space_directions,
    )


def parse_mrk_json(path: str | Path) -> LandmarkData:
    """Parse a 3D Slicer Markups JSON (``.mrk.json``) fiducial file.

    Parameters
    ----------
    path : str | Path
        Path to the ``.mrk.json`` file.

    Returns
    -------
    LandmarkData

    Raises
    ------
    FileNotFoundError
        If the file does not exist.
    ValueError
        If the file is not a valid Slicer markups file.
    """
    path = Path(path)
    if not path.exists():
        msg = f'Landmarks file not found: {path}'
        raise FileNotFoundError(msg)

    raw = json.loads(path.read_text())

    markups = raw.get('markups')
    if not markups:
        markups = [raw] if 'controlPoints' in raw else None
    if not markups:
        msg = f"Cannot find 'markups' or 'controlPoints' in {path}"
        raise ValueError(msg)

    markup = markups[0]
    control_points = markup.get('controlPoints', [])
    coord_system = markup.get('coordinateSystem', 'LPS')

    points: list[list[float]] = []
    labels: list[str] = []

    for cp in control_points:
        position = cp.get('position', cp.get('pos'))
        if position is None:
            msg = f"Control point missing 'position' field in {path}"
            raise ValueError(msg)
        points.append([float(v) for v in position[:3]])
        labels.append(cp.get('label', f'point_{len(labels)}'))

    points_arr = (
        np.array(points, dtype=np.float64)
        if points
        else np.empty((0, 3), dtype=np.float64)
    )
    return LandmarkData(
        points=points_arr,
        labels=labels,
        coordinate_system=coord_system,
    )


# -- Writers -----------------------------------------------------------------


def _compute_segment_extent(label_map: np.ndarray, label_value: int) -> str:
    """Compute bounding box of a label value as space-separated index ranges."""
    mask = label_map == label_value
    if not mask.any():
        return '0 -1 0 -1 0 -1'
    coords = np.argwhere(mask)
    mins = coords.min(axis=0)
    maxs = coords.max(axis=0)
    parts: list[str] = []
    for d in range(len(mins)):
        parts.extend([str(mins[d]), str(maxs[d])])
    return ' '.join(parts)


def build_seg_nrrd_header(
    shape: tuple[int, ...],
    origin: np.ndarray,
    space_directions: np.ndarray,
    segments: list[dict[str, Any]],
    label_map: np.ndarray,
) -> dict[str, Any]:
    """Build an NRRD header with Slicer-specific segment metadata.

    Parameters
    ----------
    shape : tuple[int, ...]
        Volume shape.
    origin : np.ndarray
        Space origin in LPS.
    space_directions : np.ndarray
        Space direction matrix.
    segments : list[dict[str, Any]]
        Segment metadata dicts with keys ``id``, ``name``,
        ``label_value``, ``color``.
    label_map : np.ndarray
        The label map array (used for extent computation).

    Returns
    -------
    dict[str, Any]
        NRRD header dict.
    """
    header: dict[str, Any] = {
        'space': 'left-posterior-superior',
        'space origin': origin.tolist(),
        'space directions': space_directions.tolist(),
        'kinds': ['domain', 'domain', 'domain'],
        'Segmentation_ContainedRepresentationNames': ('Binary labelmap'),
        'Segmentation_MasterRepresentation': 'Binary labelmap',
        'Segmentation_ReferenceImageExtentOffset': '0 0 0',
    }
    for i, seg in enumerate(segments):
        extent = _compute_segment_extent(label_map, seg['label_value'])
        color = seg['color']
        header[f'Segment{i}_ID'] = seg['id']
        header[f'Segment{i}_Name'] = seg['name']
        header[f'Segment{i}_Color'] = f'{color[0]} {color[1]} {color[2]}'
        header[f'Segment{i}_LabelValue'] = str(seg['label_value'])
        header[f'Segment{i}_Layer'] = '0'
        header[f'Segment{i}_Extent'] = extent
        header[f'Segment{i}_NameAutoGenerated'] = '0'
    return header


def write_mrk_json(
    path: Path,
    points: np.ndarray,
    labels: list[str],
    coordinate_system: str = 'LPS',
) -> None:
    """Write a 3D Slicer Markups JSON (``.mrk.json``) fiducial file.

    Parameters
    ----------
    path : Path
        Output file path.
    points : np.ndarray
        Point coordinates, shape ``(N, 3)``.
    labels : list[str]
        Point labels.
    coordinate_system : str
        ``'LPS'`` or ``'RAS'``.
    """
    control_points = [
        {
            'id': str(i),
            'label': label,
            'position': [float(v) for v in pt[:3]],
        }
        for i, (label, pt) in enumerate(zip(labels, points, strict=False))
    ]
    markup = {
        '@schema': (
            'https://raw.githubusercontent.com/slicer/slicer/master/'
            'Modules/Loadable/Markups/Resources/Schema/'
            'markups-schema-v1.0.3.json#'
        ),
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

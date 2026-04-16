"""Parsers and writers for 3D Slicer annotation formats.

Handles two file formats:

- ``.seg.nrrd`` -- segmentation label maps (integer voxel volumes)
- ``.mrk.json`` -- Slicer Markups JSON (fiducial point landmarks)

Parsers are strict: they do not silently fill in defaults for missing or
malformed fields.  Instead, they raise :class:`SlicerParseError` (a
``ValueError`` subclass) with structured ``path``, ``field``, and ``reason``
context so callers can log with their own provenance (annotator, session,
store) on top.
"""

import json
from pathlib import Path
from typing import Any

import attrs
import nrrd
import numpy as np


class SlicerParseError(ValueError):
    """Base class for Slicer file parse failures.

    Attributes
    ----------
    path : Path
        File being parsed when the error occurred.
    field : str | None
        Header key or JSON field that caused the failure, if identifiable.
    reason : str
        Human-readable description of what was wrong.
    """

    def __init__(
        self,
        path: Path,
        reason: str,
        *,
        field: str | None = None,
    ) -> None:
        self.path = path
        self.field = field
        self.reason = reason
        context = f'path={path}'
        if field is not None:
            context = f'{context}, field={field!r}'
        super().__init__(f'{reason} ({context})')


class SegNrrdParseError(SlicerParseError):
    """Raised when a ``.seg.nrrd`` file cannot be parsed."""


class MrkJsonParseError(SlicerParseError):
    """Raised when a ``.mrk.json`` file cannot be parsed."""


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


def _require_header(header: dict[str, Any], key: str, path: Path) -> Any:
    if key not in header:
        raise SegNrrdParseError(path, f"missing '{key}' header", field=key)
    return header[key]


def _parse_space_origin(header: dict[str, Any], path: Path) -> np.ndarray:
    raw = _require_header(header, 'space origin', path)
    try:
        arr = np.asarray(raw, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise SegNrrdParseError(
            path,
            f"'space origin' is not numeric: {raw!r}",
            field='space origin',
        ) from exc
    if arr.shape != (3,):
        raise SegNrrdParseError(
            path,
            f"'space origin' must have shape (3,), got {arr.shape}",
            field='space origin',
        )
    return arr


def _parse_space_directions(header: dict[str, Any], path: Path) -> np.ndarray:
    raw = _require_header(header, 'space directions', path)
    try:
        arr = np.asarray(raw, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise SegNrrdParseError(
            path,
            f"'space directions' is not numeric: {raw!r}",
            field='space directions',
        ) from exc
    if arr.shape != (3, 3):
        raise SegNrrdParseError(
            path,
            f"'space directions' must have shape (3, 3), got {arr.shape}",
            field='space directions',
        )
    return arr


def _parse_color(raw: Any, path: Path, field: str) -> tuple[float, float, float]:
    if isinstance(raw, str):
        parts: list[Any] = raw.split()
    elif isinstance(raw, (list, tuple, np.ndarray)):
        parts = list(raw)
    else:
        raise SegNrrdParseError(
            path,
            f'color must be a string or sequence, got {type(raw).__name__}',
            field=field,
        )
    if len(parts) != 3:
        raise SegNrrdParseError(
            path,
            f'color must have 3 components, got {len(parts)}: {raw!r}',
            field=field,
        )
    try:
        values = tuple(float(c) for c in parts)
    except (TypeError, ValueError) as exc:
        raise SegNrrdParseError(
            path,
            f'color has non-numeric component: {raw!r}',
            field=field,
        ) from exc
    return values  # type: ignore[return-value]


def _parse_segment(header: dict[str, Any], idx: int, path: Path) -> Segment:
    id_key = f'Segment{idx}_ID'
    name_key = f'Segment{idx}_Name'
    label_key = f'Segment{idx}_LabelValue'
    color_key = f'Segment{idx}_Color'

    seg_id_raw = _require_header(header, id_key, path)
    name_raw = header[name_key]  # caller has already confirmed presence
    label_raw = _require_header(header, label_key, path)
    color_raw = _require_header(header, color_key, path)

    if not isinstance(name_raw, str):
        raise SegNrrdParseError(
            path,
            f'segment name must be a string, got {type(name_raw).__name__}',
            field=name_key,
        )

    try:
        label_value = int(label_raw)
    except (TypeError, ValueError) as exc:
        raise SegNrrdParseError(
            path,
            f'label value is not an integer: {label_raw!r}',
            field=label_key,
        ) from exc

    color = _parse_color(color_raw, path, color_key)

    return Segment(
        id=str(seg_id_raw),
        name=name_raw,
        label_value=label_value,
        color=color,
    )


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
    SegNrrdParseError
        If the file cannot be read, is missing required headers, or
        contains malformed spatial / segment metadata.  Subclass of
        ``ValueError``.
    """
    path = Path(path)
    if not path.exists():
        msg = f'Segmentation file not found: {path}'
        raise FileNotFoundError(msg)

    try:
        data, header = nrrd.read(str(path))
    except Exception as exc:
        raise SegNrrdParseError(path, f'failed to read NRRD file: {exc}') from exc

    if not isinstance(data, np.ndarray):
        raise SegNrrdParseError(
            path,
            f'label map must be an ndarray, got {type(data).__name__}',
        )
    if data.ndim != 3:
        raise SegNrrdParseError(
            path,
            f'label map must be 3-dimensional, got ndim={data.ndim}',
        )
    if not np.issubdtype(data.dtype, np.integer):
        raise SegNrrdParseError(
            path,
            f'label map must have integer dtype, got {data.dtype}',
        )

    space_origin = _parse_space_origin(header, path)
    space_directions = _parse_space_directions(header, path)

    segments: list[Segment] = []
    idx = 0
    while f'Segment{idx}_Name' in header:
        segments.append(_parse_segment(header, idx, path))
        idx += 1

    return SegmentationData(
        label_map=data,
        segments=segments,
        space_origin=space_origin,
        space_directions=space_directions,
    )


def _parse_control_point(cp: Any, idx: int, path: Path) -> tuple[list[float], str]:
    field_prefix = f'controlPoints[{idx}]'
    if not isinstance(cp, dict):
        raise MrkJsonParseError(
            path,
            f'control point must be an object, got {type(cp).__name__}',
            field=field_prefix,
        )

    position = cp.get('position', cp.get('pos'))
    if position is None:
        raise MrkJsonParseError(
            path, "missing 'position' field", field=f'{field_prefix}.position'
        )
    if not isinstance(position, (list, tuple)) or len(position) != 3:
        raise MrkJsonParseError(
            path,
            f"'position' must be a 3-element list, got {position!r}",
            field=f'{field_prefix}.position',
        )
    try:
        point = [float(v) for v in position]
    except (TypeError, ValueError) as exc:
        raise MrkJsonParseError(
            path,
            f"'position' has non-numeric component: {position!r}",
            field=f'{field_prefix}.position',
        ) from exc

    if 'label' not in cp:
        raise MrkJsonParseError(
            path, "missing 'label' field", field=f'{field_prefix}.label'
        )
    label = cp['label']
    if not isinstance(label, str):
        raise MrkJsonParseError(
            path,
            f"'label' must be a string, got {type(label).__name__}",
            field=f'{field_prefix}.label',
        )

    return point, label


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
    MrkJsonParseError
        If the JSON is invalid or a required field is missing or
        malformed.  Subclass of ``ValueError``.
    """
    path = Path(path)
    if not path.exists():
        msg = f'Landmarks file not found: {path}'
        raise FileNotFoundError(msg)

    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise MrkJsonParseError(path, f'invalid JSON: {exc}') from exc

    if not isinstance(raw, dict):
        raise MrkJsonParseError(
            path,
            f'top-level JSON must be an object, got {type(raw).__name__}',
        )

    markups_raw = raw.get('markups')
    if markups_raw is None and 'controlPoints' in raw:
        markups_raw = [raw]
    if markups_raw is None:
        raise MrkJsonParseError(
            path,
            "no 'markups' list or top-level 'controlPoints' found",
        )
    if not isinstance(markups_raw, list) or not markups_raw:
        raise MrkJsonParseError(
            path,
            f"'markups' must be a non-empty list, got {markups_raw!r}",
            field='markups',
        )

    markup = markups_raw[0]
    if not isinstance(markup, dict):
        raise MrkJsonParseError(
            path,
            f'markup entry must be an object, got {type(markup).__name__}',
            field='markups[0]',
        )

    if 'coordinateSystem' not in markup:
        raise MrkJsonParseError(
            path,
            "missing 'coordinateSystem' field",
            field='coordinateSystem',
        )
    coord_system = markup['coordinateSystem']
    if coord_system not in ('LPS', 'RAS'):
        raise MrkJsonParseError(
            path,
            f"'coordinateSystem' must be 'LPS' or 'RAS', got {coord_system!r}",
            field='coordinateSystem',
        )

    control_points = markup.get('controlPoints', [])
    if not isinstance(control_points, list):
        raise MrkJsonParseError(
            path,
            f"'controlPoints' must be a list, got {type(control_points).__name__}",
            field='controlPoints',
        )

    points: list[list[float]] = []
    labels: list[str] = []
    for idx, cp in enumerate(control_points):
        point, label = _parse_control_point(cp, idx, path)
        points.append(point)
        labels.append(label)

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
    origin: np.ndarray,
    space_directions: np.ndarray,
    segments: list[dict[str, Any]],
    label_map: np.ndarray,
) -> dict[str, Any]:
    """Build an NRRD header with Slicer-specific segment metadata.

    Parameters
    ----------
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

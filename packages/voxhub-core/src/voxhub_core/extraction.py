"""Extract zarr arrays as single on-disk files.

This module is the single home for "zarr array → standalone file"
transforms:

- :func:`extract_volume` — raw volume ``raw/full`` → NRRD (used by both
  the ``stage`` local CLI and ``prepare-pull``).
- :func:`extract_segmentation` — annotation label map → ``.seg.nrrd``.
- :func:`extract_landmarks` — annotation points → ``.mrk.json``.

It is distinct from :mod:`voxhub_core.export` (DICOM → zarr ingestion)
and from :mod:`voxhub_core.staging` (staging-directory assembly).
"""

import gzip
import hashlib
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import nrrd
import numpy as np
import zarr

from voxhub_core.memory_budget import (
    MemoryBudget,
    MemoryWarning,
    estimate_array_bytes,
)
from voxhub_core.memory_budget import (
    check as check_memory_budget,
)
from voxhub_core.slicer import write_mrk_json

# -- Fast NRRD writer -------------------------------------------------------

_NUMPY_TO_NRRD_TYPE: dict[str, str] = {
    'float32': 'float',
    'float64': 'double',
    'int8': 'int8',
    'int16': 'short',
    'int32': 'int',
    'int64': 'longlong',
    'uint8': 'unsigned char',
    'uint16': 'ushort',
    'uint32': 'uint',
    'uint64': 'ulonglong',
}

_NRRD_STANDARD_FIELDS: frozenset[str] = frozenset(
    {
        'content',
        'type',
        'block size',
        'dimension',
        'space',
        'space dimension',
        'sizes',
        'spacings',
        'thicknesses',
        'axis mins',
        'axis maxs',
        'centers',
        'labels',
        'units',
        'kinds',
        'endian',
        'encoding',
        'data file',
        'line skip',
        'byte skip',
        'number',
        'sample units',
        'space units',
        'space origin',
        'space directions',
        'measurement frame',
    }
)


class ExtractionError(Exception):
    """Raised when an array cannot be extracted to an on-disk file.

    Callers (notably ``_run_prepare_pull``) treat ``ExtractionError`` as
    non-fatal for annotation extraction: the offending annotation is
    skipped and surfaced in ``skipped_annotations`` so the annotator is
    never silently denied a reference file they asked for.
    """


def _format_nrrd_vector(
    v: list[float] | tuple[float, ...],
) -> str:
    """Format a vector as NRRD notation: ``(x,y,z)``."""
    return '(' + ','.join(str(x) for x in v) + ')'


def _reverse_extent(extent_str: str) -> str:
    """Reverse axis order of an NRRD extent string."""
    parts = extent_str.split()
    pairs = [(parts[i], parts[i + 1]) for i in range(0, len(parts), 2)]
    return ' '.join(p for pair in reversed(pairs) for p in pair)


def write_nrrd_raw(
    path: Path,
    data: np.ndarray,
    header: dict[str, Any],
    *,
    compress: bool = False,
) -> None:
    """Write an NRRD file with fast binary I/O.

    Bypasses pynrrd's slow Python-level serialization (~80x faster for
    large volumes).  Data is written in C-order with reversed header
    metadata (sizes, space directions, kinds, segment extents).

    Parameters
    ----------
    path : Path
        Output file path.
    data : np.ndarray
        The volume data.
    header : dict[str, Any]
        NRRD header fields.
    compress : bool
        Apply gzip compression.
    """
    nrrd_type = _NUMPY_TO_NRRD_TYPE.get(str(data.dtype))
    if nrrd_type is None:
        msg = f'Unsupported dtype for NRRD: {data.dtype}'
        raise ValueError(msg)

    bo = data.dtype.str[0]
    if bo in ('<', '|'):
        endian = 'little'
    elif bo == '>':
        endian = 'big'
    else:
        endian = sys.byteorder

    sizes_reversed = list(reversed(data.shape))

    lines: list[str] = [
        'NRRD0005',
        f'type: {nrrd_type}',
        f'dimension: {data.ndim}',
        f'sizes: {" ".join(str(s) for s in sizes_reversed)}',
        f'endian: {endian}',
        f'encoding: {"gzip" if compress else "raw"}',
    ]

    if 'kinds' not in header:
        lines.append(f'kinds: {" ".join(["domain"] * data.ndim)}')

    auto_fields = {'type', 'dimension', 'sizes', 'endian', 'encoding'}

    for key, value in header.items():
        if key in auto_fields:
            continue
        if key == 'space directions':
            dirs = list(reversed(value))
            lines.append(
                f'space directions: {" ".join(_format_nrrd_vector(d) for d in dirs)}'
            )
        elif key == 'space origin':
            lines.append(f'space origin: {_format_nrrd_vector(value)}')
        elif key == 'kinds':
            kinds = list(reversed(value)) if isinstance(value, list) else [value]
            lines.append(f'kinds: {" ".join(kinds)}')
        elif key.endswith('_Extent'):
            lines.append(f'{key}:={_reverse_extent(str(value))}')
        elif key in _NRRD_STANDARD_FIELDS:
            lines.append(f'{key}: {value}')
        else:
            lines.append(f'{key}:={value}')

    header_bytes = ('\n'.join(lines) + '\n\n').encode('ascii')
    raw_bytes = np.ascontiguousarray(data).tobytes()
    if compress:
        raw_bytes = gzip.compress(raw_bytes)

    with open(path, 'wb') as fh:
        fh.write(header_bytes)
        fh.write(raw_bytes)


# -- Spatial metadata & header builders -------------------------------------


def extract_spatial_metadata(
    attributes: dict[str, Any],
) -> tuple[np.ndarray, np.ndarray, list[float]]:
    """Extract NRRD spatial metadata from zarr array attributes.

    Parameters
    ----------
    attributes : dict[str, Any]
        Zarr array attributes containing DICOM geometry fields.

    Returns
    -------
    origin : np.ndarray
        Space origin in LPS, shape ``(3,)``.
    space_directions : np.ndarray
        Space direction matrix, shape ``(3, 3)``.
    spacing_mm : list[float]
        ``[z_spacing, y_spacing, x_spacing]`` in mm.
    """
    iop = attributes['ImageOrientationPatient']
    row_cosine = np.array([float(v) for v in iop[:3]])
    col_cosine = np.array([float(v) for v in iop[3:6]])
    slice_normal = np.cross(row_cosine, col_cosine)
    slice_normal = slice_normal / np.linalg.norm(slice_normal)

    pixel_spacing = attributes['PixelSpacing']
    row_spacing = float(pixel_spacing[0])
    col_spacing = float(pixel_spacing[1])
    slice_spacing = float(attributes['computed_slice_spacing_mm'])

    space_directions = np.array(
        [
            slice_normal * slice_spacing,
            col_cosine * row_spacing,
            row_cosine * col_spacing,
        ]
    )

    ipp = attributes['ImagePositionPatient']
    origin = np.array([float(v) for v in ipp[:3]])

    spacing_mm = [slice_spacing, row_spacing, col_spacing]
    return origin, space_directions, spacing_mm


def build_raw_volume_header(
    shape: tuple[int, ...],
    origin: np.ndarray,
    space_directions: np.ndarray,
) -> dict[str, Any]:
    """Build NRRD header dict for a volume with spatial metadata."""
    del shape  # reserved for future per-axis fields
    return {
        'space': 'left-posterior-superior',
        'space origin': origin.tolist(),
        'space directions': space_directions.tolist(),
    }


def compute_sha256(path: Path) -> str:
    """Compute ``sha256:<hex>`` of a file."""
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return f'sha256:{h.hexdigest()}'


# -- Raw-volume extraction --------------------------------------------------


def extract_volume(
    zarr_path: Path,
    dest: Path,
    *,
    compress: bool = False,
    budget: MemoryBudget | None = None,
) -> dict[str, Any]:
    """Extract a zarr store's raw volume (``raw/full``) to an NRRD file.

    Parameters
    ----------
    zarr_path : Path
        Path to the source ``.zarr`` store.
    dest : Path
        Destination NRRD file path.  Parent directories are created.
    compress : bool
        Apply gzip compression.
    budget : MemoryBudget | None
        Policy controlling RAM warnings and refusal of oversized
        materializations.  Defaults to :meth:`MemoryBudget.warn_only`.

    Returns
    -------
    dict[str, Any]
        Per-store metadata: ``raw_checksum``, ``shape``, ``dtype``,
        ``origin_lps``, ``spacing_mm``, ``space_directions``,
        ``staged_at``, ``warnings`` (list of memory advisories;
        possibly empty).

    Raises
    ------
    MemoryBudgetError
        When ``budget.refuse_when_low`` is set and the planned
        materialization would exceed available RAM headroom.
    """
    root = zarr.open_group(zarr_path, mode='r')
    arr: zarr.Array = root['raw']['full']
    attributes = dict(arr.attrs)

    effective_budget = budget if budget is not None else MemoryBudget.warn_only()
    volume_bytes = estimate_array_bytes(arr.shape, arr.dtype)
    warnings = check_memory_budget(
        volume_bytes, budget=effective_budget, context=zarr_path.name
    )

    volume_data = arr[:]

    origin, space_directions, spacing_mm = extract_spatial_metadata(attributes)
    header = build_raw_volume_header(arr.shape, origin, space_directions)

    dest.parent.mkdir(parents=True, exist_ok=True)
    write_nrrd_raw(dest, volume_data, header, compress=compress)

    return {
        'raw_checksum': compute_sha256(dest),
        'shape': list(arr.shape),
        'dtype': str(arr.dtype),
        'origin_lps': origin.tolist(),
        'spacing_mm': spacing_mm,
        'space_directions': space_directions.tolist(),
        'staged_at': datetime.now(UTC).isoformat(),
        'warnings': warnings,
    }


# -- Annotation extraction --------------------------------------------------


def _read_spatial_metadata(
    zarr_path: Path,
) -> tuple[np.ndarray, np.ndarray]:
    """Read ``(origin_lps, space_directions)`` from the store's raw volume."""
    try:
        root = zarr.open_group(zarr_path, mode='r')
        raw_attrs = dict(root['raw']['full'].attrs)
        origin, space_directions, _ = extract_spatial_metadata(raw_attrs)
    except (KeyError, FileNotFoundError) as exc:
        msg = f'failed to read spatial metadata from {zarr_path}: {exc}'
        raise ExtractionError(msg) from exc
    return origin, space_directions


def _open_annotation_array(zarr_path: Path, array_zarr_path: str) -> zarr.Array:
    """Open an annotation array at ``zarr_path/array_zarr_path``."""
    try:
        root = zarr.open_group(zarr_path, mode='r')
    except FileNotFoundError as exc:
        msg = f'zarr store not found: {zarr_path}'
        raise ExtractionError(msg) from exc
    try:
        node = root[array_zarr_path]
    except KeyError as exc:
        msg = f'annotation array not found: {array_zarr_path}'
        raise ExtractionError(msg) from exc
    if not isinstance(node, zarr.Array):
        msg = f'expected a zarr array at {array_zarr_path}, got {type(node).__name__}'
        raise ExtractionError(msg)
    return node


def _build_seg_nrrd_header(
    origin: np.ndarray,
    space_directions: np.ndarray,
    segments: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a Slicer-compatible NRRD header for a segmentation export.

    Minimal version of :func:`voxhub_core.slicer.build_seg_nrrd_header`
    — only what Slicer needs to round-trip.  We deliberately skip the
    per-segment ``Extent`` field: pynrrd will compute it from the label
    map itself when needed, and it would otherwise require a second pass
    over a potentially large volume.
    """
    header: dict[str, Any] = {
        'space': 'left-posterior-superior',
        'space origin': origin.tolist(),
        'space directions': space_directions.tolist(),
        'kinds': ['domain', 'domain', 'domain'],
        'Segmentation_ContainedRepresentationNames': 'Binary labelmap',
        'Segmentation_MasterRepresentation': 'Binary labelmap',
        'Segmentation_ReferenceImageExtentOffset': '0 0 0',
    }
    for i, seg in enumerate(segments):
        color = seg['color']
        header[f'Segment{i}_ID'] = str(seg['id'])
        header[f'Segment{i}_Name'] = str(seg['name'])
        header[f'Segment{i}_Color'] = (
            f'{float(color[0])} {float(color[1])} {float(color[2])}'
        )
        header[f'Segment{i}_LabelValue'] = str(int(seg['label_value']))
        header[f'Segment{i}_Layer'] = '0'
        header[f'Segment{i}_NameAutoGenerated'] = '0'
    return header


def extract_segmentation(
    zarr_path: Path,
    array_zarr_path: str,
    dest: Path,
    *,
    budget: MemoryBudget | None = None,
    warnings_out: list[MemoryWarning] | None = None,
) -> str:
    """Extract an integrated segmentation zarr array to a ``.seg.nrrd`` file.

    Parameters
    ----------
    zarr_path : Path
        Path to the source ``.zarr`` store.
    array_zarr_path : str
        Path of the annotation array within the zarr hierarchy, e.g.
        ``'annotations/alice-xyz45678/inner-ear-structures-20260101-ab12/data'``.
    dest : Path
        Destination file path for the extracted ``.seg.nrrd``.
    budget : MemoryBudget | None
        Policy controlling RAM warnings and refusal of oversized
        materializations.  Defaults to :meth:`MemoryBudget.warn_only`.
        The segmentation path holds two transient copies (``label_map``
        + ``pynrrd`` internal buffer) so the budget's safety factor
        should typically be ``>= 3.0`` here.
    warnings_out : list[MemoryWarning] | None
        Optional sink for memory advisories produced during the check.
        Caller-owned list; warnings are appended in the order they are
        emitted.

    Returns
    -------
    str
        ``sha256:<hex>`` digest of the written file.

    Raises
    ------
    ExtractionError
        If the array is missing, has an unsupported dtype, or lacks the
        ``segments`` attribute.
    MemoryBudgetError
        When ``budget.refuse_when_low`` is set and the planned
        materialization would exceed available RAM headroom.
    """
    origin, space_directions = _read_spatial_metadata(zarr_path)
    arr = _open_annotation_array(zarr_path, array_zarr_path)

    effective_budget = budget if budget is not None else MemoryBudget.warn_only()
    volume_bytes = estimate_array_bytes(arr.shape, arr.dtype)
    new_warnings = check_memory_budget(
        volume_bytes, budget=effective_budget, context=zarr_path.name
    )
    if warnings_out is not None:
        warnings_out.extend(new_warnings)

    try:
        label_map = np.asarray(arr[:])
    except Exception as exc:  # pragma: no cover — defensive
        msg = f'failed to read label map from {array_zarr_path}: {exc}'
        raise ExtractionError(msg) from exc

    if label_map.ndim != 3:
        msg = (
            f'segmentation label map at {array_zarr_path} must be '
            f'3-dimensional, got ndim={label_map.ndim}'
        )
        raise ExtractionError(msg)
    if not np.issubdtype(label_map.dtype, np.integer):
        msg = (
            f'segmentation label map at {array_zarr_path} must have integer '
            f'dtype, got {label_map.dtype}'
        )
        raise ExtractionError(msg)

    ann_attrs = dict(arr.attrs)
    segments_raw = ann_attrs.get('segments')
    if not isinstance(segments_raw, list):
        msg = (
            f"annotation at {array_zarr_path} is missing a 'segments' "
            f'attribute (is it really a segmentation?)'
        )
        raise ExtractionError(msg)

    segments: list[dict[str, Any]] = []
    for i, raw in enumerate(segments_raw):
        if not isinstance(raw, dict):
            msg = (
                f'annotation at {array_zarr_path}: segments[{i}] is not a '
                f'dict, got {type(raw).__name__}'
            )
            raise ExtractionError(msg)
        missing = {'id', 'name', 'label_value', 'color'} - set(raw.keys())
        if missing:
            msg = (
                f'annotation at {array_zarr_path}: segments[{i}] missing '
                f'keys {sorted(missing)!r}'
            )
            raise ExtractionError(msg)
        segments.append(dict(raw))

    header = _build_seg_nrrd_header(origin, space_directions, segments)

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        nrrd.write(str(dest), label_map, header)
    except Exception as exc:
        msg = f'failed to write {dest}: {exc}'
        raise ExtractionError(msg) from exc

    return compute_sha256(dest)


def extract_landmarks(
    zarr_path: Path,
    array_zarr_path: str,
    dest: Path,
) -> str:
    """Extract an integrated landmark zarr array to a ``.mrk.json`` file.

    The points are already stored in LPS (the integrate path converts
    RAS inputs on write), so the extracted file declares
    ``coordinateSystem: 'LPS'``.

    Parameters
    ----------
    zarr_path : Path
        Path to the source ``.zarr`` store.  Opened only to resolve the
        annotation array; landmark extraction does not consume spatial
        metadata from the raw volume.
    array_zarr_path : str
        Path of the annotation array within the zarr hierarchy.
    dest : Path
        Destination file path for the extracted ``.mrk.json``.

    Returns
    -------
    str
        ``sha256:<hex>`` digest of the written file.

    Raises
    ------
    ExtractionError
        If the array is missing, has an unexpected shape, or lacks the
        ``labels`` attribute.
    """
    arr = _open_annotation_array(zarr_path, array_zarr_path)

    try:
        points = np.asarray(arr[:], dtype=np.float64)
    except Exception as exc:  # pragma: no cover — defensive
        msg = f'failed to read landmark points from {array_zarr_path}: {exc}'
        raise ExtractionError(msg) from exc

    if points.ndim != 2 or points.shape[1] != 3:
        msg = (
            f'landmark points at {array_zarr_path} must have shape (N, 3), '
            f'got {points.shape}'
        )
        raise ExtractionError(msg)

    ann_attrs = dict(arr.attrs)
    labels_raw = ann_attrs.get('labels')
    if not isinstance(labels_raw, list):
        msg = (
            f"annotation at {array_zarr_path} is missing a 'labels' "
            f'attribute (is it really a landmark set?)'
        )
        raise ExtractionError(msg)
    if len(labels_raw) != points.shape[0]:
        msg = (
            f'annotation at {array_zarr_path}: labels count '
            f'({len(labels_raw)}) does not match points count '
            f'({points.shape[0]})'
        )
        raise ExtractionError(msg)

    labels: list[str] = []
    for i, lab in enumerate(labels_raw):
        if not isinstance(lab, str):
            msg = (
                f'annotation at {array_zarr_path}: labels[{i}] is not a '
                f'string, got {type(lab).__name__}'
            )
            raise ExtractionError(msg)
        labels.append(lab)

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        write_mrk_json(dest, points, labels, coordinate_system='LPS')
    except Exception as exc:
        msg = f'failed to write {dest}: {exc}'
        raise ExtractionError(msg) from exc

    return compute_sha256(dest)

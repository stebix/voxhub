"""Stage zarr volumes as NRRD files for 3D Slicer annotation.

Reads zarr stores, extracts spatial metadata, and writes NRRD files
to a WIP directory for annotation in 3D Slicer.
"""

import gzip
import hashlib
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import zarr
from rich.console import Console

from voxhub_core.catalog import discover_zarr_stores

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


def _write_nrrd_raw(
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


# -- Spatial metadata extraction ---------------------------------------------


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


def _build_nrrd_header(
    shape: tuple[int, ...],
    origin: np.ndarray,
    space_directions: np.ndarray,
) -> dict[str, Any]:
    """Build NRRD header dict for a volume with spatial metadata."""
    return {
        'space': 'left-posterior-superior',
        'space origin': origin.tolist(),
        'space directions': space_directions.tolist(),
    }


def _compute_sha256(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return f'sha256:{h.hexdigest()}'


# -- Stage -------------------------------------------------------------------


def stage(
    zarr_root: str | Path,
    wip_dir: str | Path,
    *,
    store_names: list[str] | None = None,
    compress: bool = False,
    force: bool = False,
    console: Console | None = None,
) -> dict[str, dict[str, Any]]:
    """Export zarr volumes as NRRD files into a WIP directory.

    Parameters
    ----------
    zarr_root : str | Path
        Directory containing ``.zarr`` stores.
    wip_dir : str | Path
        Target WIP directory to create.
    store_names : list[str] | None
        Specific store names to stage.  ``None`` stages all.
    compress : bool
        Apply gzip compression to NRRD files.
    force : bool
        Overwrite existing WIP directory contents.
    console : Console | None
        Optional rich Console for output.

    Returns
    -------
    dict[str, dict[str, Any]]
        Per-store metadata (checksums, shape, spatial info).

    Raises
    ------
    FileExistsError
        If *wip_dir* exists and *force* is False.
    FileNotFoundError
        If *zarr_root* does not exist or no stores found.
    """
    zarr_root = Path(zarr_root)
    wip_dir = Path(wip_dir)
    console = console or Console()

    if not zarr_root.is_dir():
        msg = f'Zarr root directory not found: {zarr_root}'
        raise FileNotFoundError(msg)

    if wip_dir.exists() and not force:
        msg = f'WIP directory already exists: {wip_dir}. Pass force=True to overwrite.'
        raise FileExistsError(msg)

    entries = discover_zarr_stores(zarr_root)
    if not entries:
        msg = f'No .zarr stores found under {zarr_root}'
        raise FileNotFoundError(msg)

    if store_names is not None:
        name_set = set(store_names)
        entries = [e for e in entries if e.path.name.removesuffix('.zarr') in name_set]
        if not entries:
            msg = f'No matching stores found for: {store_names}'
            raise FileNotFoundError(msg)

    wip_dir.mkdir(parents=True, exist_ok=True)

    store_metadata: dict[str, dict[str, Any]] = {}

    for entry in entries:
        store_name = entry.path.name.removesuffix('.zarr')
        console.print(f'  Staging [green]{store_name}[/green] ...')

        if entry.error:
            console.print(f'  [red]Skipping {store_name}: {entry.error}[/red]')
            continue

        root = zarr.open_group(entry.path, mode='r')
        arr = root['raw']['full']
        volume_data = arr[:]
        attributes = dict(arr.attrs)

        origin, space_directions, spacing_mm = extract_spatial_metadata(attributes)

        header = _build_nrrd_header(arr.shape, origin, space_directions)

        store_wip = wip_dir / store_name
        store_wip.mkdir(parents=True, exist_ok=True)
        nrrd_path = store_wip / 'raw.nrrd'
        _write_nrrd_raw(nrrd_path, volume_data, header, compress=compress)

        raw_checksum = _compute_sha256(nrrd_path)

        console.print(
            f'    shape={arr.shape}  '
            f'spacing=[{spacing_mm[0]:.3f}, '
            f'{spacing_mm[1]:.3f}, '
            f'{spacing_mm[2]:.3f}] mm'
        )

        store_metadata[store_name] = {
            'zarr_path': str(entry.path.resolve()),
            'raw_checksum': raw_checksum,
            'shape': list(arr.shape),
            'dtype': str(arr.dtype),
            'origin_lps': origin.tolist(),
            'spacing_mm': spacing_mm,
            'space_directions': space_directions.tolist(),
            'staged_at': datetime.now(UTC).isoformat(),
        }

    console.print(f'\n[bold]{len(store_metadata)}[/bold] store(s) staged to {wip_dir}')
    return store_metadata

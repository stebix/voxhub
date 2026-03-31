"""Slice geometry computation from DICOM datasets.

Computes inter-slice spacing, spatial sort order, and validates
ImageOrientationPatient consistency across slices.
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    import pydicom
    from numpy.typing import NDArray

COMPUTED_METADATA_KEYS: dict[str, str] = {
    'computed_slice_spacing_mm': (
        'Median center-to-center distance between adjacent slices '
        'in mm, computed from ImagePositionPatient projected onto '
        'the slice normal.'
    ),
    'computed_slice_spacing_uniform': (
        'Whether all consecutive slice spacings are within tolerance (True/False).'
    ),
    'computed_slice_spacing_max_deviation_mm': (
        'Maximum absolute deviation from the median spacing across '
        'all consecutive slice pairs, in mm.'
    ),
    'computed_slice_spacing_method': (
        "Method used: 'image_position_patient' (projected onto "
        "slice normal) or 'fallback_single_slice' if only one slice."
    ),
    'computed_spatial_sort_matches_filename': (
        'Whether the spatially-sorted order matched the original '
        'filename-integer order (True/False).'
    ),
}


def check_image_orientation_patient_consistency(
    datasets: list[pydicom.Dataset],
    tolerance_mm: float,
) -> tuple[bool, NDArray[np.floating[Any]], list[int]]:
    """Check that all datasets share consistent ImageOrientationPatient.

    Parameters
    ----------
    datasets : list[pydicom.Dataset]
        DICOM datasets to check.
    tolerance_mm : float
        Tolerance for orientation comparison.

    Returns
    -------
    consistent : bool
        True if all orientations match within tolerance.
    normal : NDArray
        Slice normal vector from the first dataset.
    inconsistent_indices : list[int]
        Indices of datasets with inconsistent orientation.
    """
    iop_key = 'ImageOrientationPatient'
    first_iop = getattr(datasets[0], iop_key, None)
    if first_iop is None:
        msg = f'Missing {iop_key} in first slice'
        raise RuntimeError(msg)

    row = np.array([float(v) for v in first_iop[:3]])
    col = np.array([float(v) for v in first_iop[3:]])
    normal = np.cross(row, col)
    normal = normal / np.linalg.norm(normal)

    abnormal_indices: list[int] = []

    for i, ds in enumerate(datasets[1:], 1):
        iop = getattr(ds, iop_key, None)
        if iop is None:
            msg = f'Missing {iop_key} in slice index {i}'
            raise RuntimeError(msg)
        ds_row = np.array([float(v) for v in iop[:3]])
        ds_col = np.array([float(v) for v in iop[3:]])
        if not (
            np.allclose(ds_row, row, atol=tolerance_mm)
            and np.allclose(ds_col, col, atol=tolerance_mm)
        ):
            abnormal_indices.append(i)

    return (len(abnormal_indices) == 0, normal, abnormal_indices)


def compute_slice_geometry(
    datasets: list[pydicom.Dataset],
    *,
    tolerance_mm: float = 0.01,
) -> tuple[list[int], dict[str, Any]]:
    """Compute inter-slice spacing and spatial sort order.

    Parameters
    ----------
    datasets : list[pydicom.Dataset]
        DICOM datasets for a single series.
    tolerance_mm : float
        Tolerance in mm for spacing uniformity.

    Returns
    -------
    sort_indices : list[int]
        Indices for physically correct slice order.
    computed_metadata : dict[str, Any]
        Geometry metadata (see :data:`COMPUTED_METADATA_KEYS`).
    """
    consistent, normal, abnormal_indices = check_image_orientation_patient_consistency(
        datasets, tolerance_mm
    )
    if not consistent:
        warnings.warn(
            'Inconsistent ImageOrientationPatient across slices '
            f'at indices: {abnormal_indices}. '
            'Geometry computations may be unreliable.',
            stacklevel=2,
        )

    # Project each slice's ImagePositionPatient onto the normal.
    projections = np.empty(len(datasets))
    for j, ds in enumerate(datasets):
        ipp = getattr(ds, 'ImagePositionPatient', None)
        if ipp is None:
            msg = f'Missing ImagePositionPatient in slice index {j}'
            raise RuntimeError(msg)
        ipp_vec = np.array([float(v) for v in ipp])
        projections[j] = np.dot(ipp_vec, normal)

    sort_indices = np.argsort(projections).tolist()

    identity = list(range(len(datasets)))
    spatial_matches_filename = sort_indices == identity
    if not spatial_matches_filename:
        warnings.warn(
            'Spatial slice order (from ImagePositionPatient) '
            'differs from filename order. '
            'Slices have been re-sorted spatially.',
            stacklevel=2,
        )

    sorted_projections = projections[sort_indices]
    spacings = np.diff(sorted_projections)
    median_spacing = float(np.median(spacings))
    max_deviation = float(np.max(np.abs(spacings - median_spacing)))
    uniform = max_deviation < tolerance_mm

    if not uniform:
        warnings.warn(
            f'Non-uniform slice spacing detected: '
            f'median={median_spacing:.4f} mm, '
            f'max deviation={max_deviation:.4f} mm '
            f'(tolerance={tolerance_mm} mm).',
            stacklevel=2,
        )

    # Cross-validate against SpacingBetweenSlices if present.
    sbs = getattr(datasets[0], 'SpacingBetweenSlices', None)
    if sbs is not None:
        sbs_val = float(sbs)
        if abs(sbs_val - median_spacing) > tolerance_mm:
            warnings.warn(
                f'Computed slice spacing ({median_spacing:.4f} mm) '
                f'differs from SpacingBetweenSlices tag '
                f'({sbs_val:.4f} mm).',
                stacklevel=2,
            )

    return sort_indices, {
        'computed_slice_spacing_mm': median_spacing,
        'computed_slice_spacing_uniform': uniform,
        'computed_slice_spacing_max_deviation_mm': max_deviation,
        'computed_slice_spacing_method': 'image_position_patient',
        'computed_spatial_sort_matches_filename': spatial_matches_filename,
    }

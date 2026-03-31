"""Client-side pre-flight validation checks.

Validates annotation files against spatial metadata from the pull manifest
and ontology conformance before push.  All checks are pure — no zarr or
DICOM I/O.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import nrrd
import numpy as np

from voxhub_schema.models import IssueRecord

if TYPE_CHECKING:
    from pathlib import Path

    from voxhub_schema.manifest import RemoteManifestEntry
    from voxhub_schema.ontology import Ontology


def _parse_seg_nrrd_header(
    path: Path,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[dict[str, object]]]:
    """Parse spatial metadata and segment info from a ``.seg.nrrd`` file.

    Returns
    -------
    tuple
        ``(label_map, space_origin, space_directions, segments)``
    """
    data, header = nrrd.read(str(path))

    space_origin = np.array(header.get('space origin', [0.0, 0.0, 0.0]), dtype=np.float64)
    space_directions = np.array(
        header.get('space directions', np.eye(3)),
        dtype=np.float64,
    )

    segments: list[dict[str, object]] = []
    i = 0
    while True:
        name_key = f'Segment{i}_Name'
        if name_key not in header:
            break
        segments.append(
            {
                'id': header.get(f'Segment{i}_ID', str(i)),
                'name': header[name_key],
                'label_value': int(header.get(f'Segment{i}_LabelValue', i + 1)),
            }
        )
        i += 1

    return data, space_origin, space_directions, segments


def validate_seg_preflight(
    seg_path: Path,
    manifest_entry: RemoteManifestEntry,
    ontology: Ontology,
    *,
    spatial_tolerance: float = 0.01,
) -> list[IssueRecord]:
    """Validate a segmentation NRRD against the manifest and ontology.

    Parameters
    ----------
    seg_path : Path
        Path to a ``.seg.nrrd`` file.
    manifest_entry : RemoteManifestEntry
        Per-store metadata from the pull manifest.
    ontology : Ontology
        The ontology this segmentation must conform to.
    spatial_tolerance : float
        Tolerance in mm for spatial metadata comparison.

    Returns
    -------
    list[IssueRecord]
        Validation issues.  Empty means valid.
    """
    issues: list[IssueRecord] = []

    if not seg_path.exists():
        issues.append(
            IssueRecord(
                severity='error',
                message=f'Segmentation file not found: {seg_path}',
            )
        )
        return issues

    label_map, space_origin, space_directions, segments = _parse_seg_nrrd_header(seg_path)

    expected_shape = tuple(manifest_entry.shape)

    # -- Shape match --
    if label_map.shape != expected_shape:
        issues.append(
            IssueRecord(
                severity='error',
                message=(
                    f'Shape mismatch: segmentation is {label_map.shape}, '
                    f'expected {expected_shape}'
                ),
            )
        )

    # -- Value integrity: all non-negative integers --
    if not np.issubdtype(label_map.dtype, np.integer) and not np.all(
        label_map == label_map.astype(int)
    ):
        issues.append(
            IssueRecord(
                severity='error',
                message=(
                    f'Label map contains non-integer values (dtype: {label_map.dtype})'
                ),
            )
        )

    if np.any(label_map < 0):
        issues.append(
            IssueRecord(
                severity='error',
                message='Label map contains negative values',
            )
        )

    # -- Spatial consistency: origin --
    expected_origin = np.array(manifest_entry.origin_lps)
    if not np.allclose(space_origin, expected_origin, atol=spatial_tolerance):
        issues.append(
            IssueRecord(
                severity='error',
                message=(
                    f'Space origin mismatch: segmentation has '
                    f'{space_origin.tolist()}, expected '
                    f'{expected_origin.tolist()} '
                    f'(tolerance={spatial_tolerance} mm)'
                ),
            )
        )

    # -- Spatial consistency: directions --
    expected_dirs = np.array(manifest_entry.space_directions)
    if not np.allclose(space_directions, expected_dirs, atol=spatial_tolerance):
        issues.append(
            IssueRecord(
                severity='error',
                message=(
                    f'Space directions mismatch: segmentation has been '
                    f'resampled or transformed '
                    f'(tolerance={spatial_tolerance} mm)'
                ),
            )
        )

    # -- Label integrity: sequential from zero --
    unique_labels = set(np.unique(label_map).tolist())
    unique_labels.discard(0)
    defined_labels = {seg['label_value'] for seg in segments}

    undefined = unique_labels - defined_labels
    if undefined:
        issues.append(
            IssueRecord(
                severity='warning',
                message=(
                    f'Label values {sorted(undefined)} found in volume but '
                    f'have no segment definition in header'
                ),
            )
        )

    if defined_labels:
        max_label = max(defined_labels)
        expected_range = set(range(1, max_label + 1))
        gaps = expected_range - defined_labels
        if gaps:
            issues.append(
                IssueRecord(
                    severity='warning',
                    message=(f'Gap in label sequence: missing labels {sorted(gaps)}'),
                )
            )

    # -- Ontology conformance --
    if not ontology.is_unconstrained:
        _check_seg_ontology_conformance(issues, segments, label_map, ontology)
    else:
        _check_unconstrained_conformance(issues, label_map, ontology)

    return issues


def _check_seg_ontology_conformance(
    issues: list[IssueRecord],
    segments: list[dict[str, object]],
    label_map: np.ndarray,
    ontology: Ontology,
) -> None:
    """Check that a segmentation conforms to a constrained ontology."""
    assert ontology.labels is not None

    expected_labels = {lbl.value: lbl.name for lbl in ontology.labels}
    seg_labels = {
        int(seg['label_value']): str(seg['name'])  # type: ignore[arg-type]
        for seg in segments
    }
    # Add background implicitly.
    seg_labels.setdefault(0, 'background')

    # Check for missing expected labels.
    for value, name in expected_labels.items():
        if value not in seg_labels:
            issues.append(
                IssueRecord(
                    severity='error',
                    message=(
                        f'Ontology requires label {value} ({name!r}) '
                        f'but it is not defined in the segmentation'
                    ),
                )
            )
        elif seg_labels[value] != name:
            issues.append(
                IssueRecord(
                    severity='warning',
                    message=(
                        f'Label {value} name mismatch: segmentation has '
                        f'{seg_labels[value]!r}, ontology expects {name!r}'
                    ),
                )
            )

    # Check for extra labels not in the ontology.
    for value, name in seg_labels.items():
        if value not in expected_labels:
            issues.append(
                IssueRecord(
                    severity='error',
                    message=(
                        f'Segmentation defines label {value} ({name!r}) '
                        f'which is not in the ontology'
                    ),
                )
            )


def _check_unconstrained_conformance(
    issues: list[IssueRecord],
    label_map: np.ndarray,
    ontology: Ontology,
) -> None:
    """Check structural constraints for an unconstrained ontology."""
    constraints = ontology.constraints or []
    unique_labels = sorted(int(v) for v in np.unique(label_map))

    if 'non_negative_integers' in constraints and any(v < 0 for v in unique_labels):
        issues.append(
            IssueRecord(
                severity='error',
                message='Constraint violated: label map contains negative values',
            )
        )

    if 'sequential_from_zero' in constraints:
        expected = list(range(len(unique_labels)))
        if unique_labels != expected:
            issues.append(
                IssueRecord(
                    severity='error',
                    message=(
                        f'Constraint violated: labels are not sequential '
                        f'from zero. Found {unique_labels}, '
                        f'expected {expected}'
                    ),
                )
            )

    if 'background_at_zero' in constraints and 0 not in unique_labels:
        issues.append(
            IssueRecord(
                severity='error',
                message='Constraint violated: label 0 (background) is not present',
            )
        )


def _parse_mrk_json(
    path: Path,
) -> tuple[np.ndarray, list[str], str]:
    """Parse a Slicer Markups JSON file.

    Returns
    -------
    tuple
        ``(points, labels, coordinate_system)``
    """
    import json

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
    return points_arr, labels, coord_system


def validate_lmk_preflight(
    lmk_path: Path,
    manifest_entry: RemoteManifestEntry,
    ontology: Ontology,
) -> list[IssueRecord]:
    """Validate a landmark file against the manifest and ontology.

    Parameters
    ----------
    lmk_path : Path
        Path to a ``.mrk.json`` file.
    manifest_entry : RemoteManifestEntry
        Per-store metadata from the pull manifest.
    ontology : Ontology
        The ontology this landmark set must conform to.

    Returns
    -------
    list[IssueRecord]
        Validation issues.  Empty means valid.
    """
    issues: list[IssueRecord] = []

    if not lmk_path.exists():
        issues.append(
            IssueRecord(
                severity='error',
                message=f'Landmarks file not found: {lmk_path}',
            )
        )
        return issues

    points, labels, coord_system = _parse_mrk_json(lmk_path)

    # -- Coordinate system known --
    if coord_system not in ('LPS', 'RAS'):
        issues.append(
            IssueRecord(
                severity='error',
                message=(
                    f"Unknown coordinate system: '{coord_system}' "
                    f"(expected 'LPS' or 'RAS')"
                ),
            )
        )

    # -- Label uniqueness --
    if len(set(labels)) != len(labels):
        seen: set[str] = set()
        dupes = [
            lab
            for lab in labels
            if lab in seen or seen.add(lab)  # type: ignore[func-returns-value]
        ]
        issues.append(
            IssueRecord(
                severity='error',
                message=f'Duplicate landmark labels: {dupes}',
            )
        )

    # -- Coordinate bounds check --
    if len(labels) > 0:
        origin = np.array(manifest_entry.origin_lps)
        shape = np.array(manifest_entry.shape)
        space_dirs = np.array(manifest_entry.space_directions)

        corner_offsets = shape[:, np.newaxis] * space_dirs
        far_corner = origin + corner_offsets.sum(axis=0)
        bbox_min = np.minimum(origin, far_corner)
        bbox_max = np.maximum(origin, far_corner)

        margin = 5.0
        bbox_min -= margin
        bbox_max += margin

        pts = points
        if coord_system == 'RAS':
            pts = pts.copy()
            pts[:, 0] *= -1
            pts[:, 1] *= -1

        for label, pt in zip(labels, pts, strict=False):
            if np.any(pt < bbox_min) or np.any(pt > bbox_max):
                issues.append(
                    IssueRecord(
                        severity='warning',
                        message=(
                            f"Landmark '{label}' at {pt.tolist()} is outside "
                            f'the volume bounding box '
                            f'[{bbox_min.tolist()}, {bbox_max.tolist()}]'
                        ),
                    )
                )

    # -- Ontology conformance --
    if ontology.type == 'landmarks' and ontology.points is not None:
        _check_lmk_ontology_conformance(issues, labels, ontology)

    return issues


def _check_lmk_ontology_conformance(
    issues: list[IssueRecord],
    labels: list[str],
    ontology: Ontology,
) -> None:
    """Check that landmarks conform to the expected ontology."""
    assert ontology.points is not None

    expected = set(ontology.points)
    actual = set(labels)

    missing = expected - actual
    if missing:
        issues.append(
            IssueRecord(
                severity='error',
                message=(
                    f'Ontology requires points {sorted(missing)} but they are missing'
                ),
            )
        )

    extra = actual - expected
    if extra:
        issues.append(
            IssueRecord(
                severity='error',
                message=(
                    f'Landmarks contain points {sorted(extra)} '
                    f'not defined in the ontology'
                ),
            )
        )

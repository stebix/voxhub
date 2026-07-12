"""Canonical pre-flight validation checks for annotations.

This module is the **single** validator implementation for the project.  It
is the authority the server calls at integrate time and (for fast local
feedback) the client calls before push.  It validates parsed NRRD / JSON
annotation structures against the raw volume's spatial metadata and against
a versioned ontology.

The package stays free of ``zarr`` (and any store I/O): the caller extracts
the volume's spatial metadata from its zarr attrs and passes it in as plain
values (``manifest_entry``), exactly as
:func:`voxhub_core.extraction.extract_spatial_metadata` already does.

Coordinate-system policy
------------------------
* **Segmentations** must be exported in LPS (``left-posterior-superior``).
  A RAS-exported ``.seg.nrrd`` is rejected with an error telling the
  annotator to re-export as LPS rather than being silently converted:
  re-orienting a full voxel grid's origin/direction matrix is error-prone,
  Slicer exports segmentations in LPS by default, and a wrong-space
  segmentation would otherwise mismatch the LPS manifest numerically and
  surface a confusing secondary error.
* **Landmarks** may be exported in LPS or RAS; RAS points are cheap and
  lossless to mirror (negate x/y), so they are converted for the
  bounding-box check and on write.  A landmark file whose coordinate
  system disagrees with the ontology's declared ``coordinate_system``
  produces a warning (the points are still converted to LPS on write).
"""

import json
from collections.abc import Mapping
from pathlib import Path

import nrrd
import numpy as np

from voxhub_schema.models import IssueRecord
from voxhub_schema.ontology import Ontology

# NRRD ``space`` field spellings (lower-cased) that mean LPS / RAS.
_LPS_SPACES = frozenset({'left-posterior-superior', 'lps'})
_RAS_SPACES = frozenset({'right-anterior-superior', 'ras'})

# Plain spatial-metadata mapping.  Required keys: ``shape`` (sequence of
# int), ``origin_lps`` (3 floats), ``space_directions`` (3x3 floats).  Extra
# keys (e.g. ``spacing_mm``) are ignored.
type VolumeMetadata = Mapping[str, object]


def _parse_seg_nrrd_header(
    path: Path,
) -> tuple[np.ndarray, str | None, np.ndarray, np.ndarray, list[dict[str, object]]]:
    """Parse label map + spatial metadata + segment info from a ``.seg.nrrd``.

    Returns
    -------
    tuple
        ``(label_map, space, space_origin, space_directions, segments)``.
        ``space`` is the raw NRRD ``space`` field (or ``None`` if absent).
        ``space_directions`` is returned as parsed -- it may be a ``(4, 3)``
        array with a NaN row for multi-layer exports; the caller detects
        that before any numeric comparison.
    """
    data, header = nrrd.read(str(path))

    space = header.get('space')
    space_str = str(space) if space is not None else None

    space_origin = np.asarray(
        header.get('space origin', [0.0, 0.0, 0.0]), dtype=np.float64
    )
    space_directions = np.asarray(
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

    return data, space_str, space_origin, space_directions, segments


def _is_multilayer_directions(space_directions: np.ndarray) -> bool:
    """Whether ``space directions`` signals a multi-layer (4D) export.

    pynrrd yields a ``(4, 3)`` array with an all-NaN row for the
    non-spatial axis of a multi-layer ``.seg.nrrd``.  A well-formed
    single-layer segmentation is exactly ``(3, 3)`` with no NaNs.
    """
    return (
        space_directions.ndim != 2
        or space_directions.shape != (3, 3)
        or bool(np.isnan(space_directions).any())
    )


def _check_seg_space(issues: list[IssueRecord], space: str | None) -> bool:
    """Validate the NRRD ``space`` field; return whether it is LPS.

    A non-LPS space appends an error and returns ``False`` so the caller
    skips the (now meaningless) numeric origin/direction comparison.
    """
    if space is None:
        issues.append(
            IssueRecord(
                severity='error',
                message=(
                    "Segmentation header has no 'space' field; export from "
                    "3D Slicer in LPS ('left-posterior-superior')"
                ),
            )
        )
        return False

    normalized = space.strip().lower()
    if normalized in _LPS_SPACES:
        return True
    if normalized in _RAS_SPACES:
        issues.append(
            IssueRecord(
                severity='error',
                message=(
                    f'Segmentation was exported in RAS ({space!r}); re-export '
                    "it in LPS ('left-posterior-superior')"
                ),
            )
        )
        return False

    issues.append(
        IssueRecord(
            severity='error',
            message=(
                f'Segmentation has an unrecognized NRRD space {space!r}; '
                "export in LPS ('left-posterior-superior')"
            ),
        )
    )
    return False


def validate_seg_preflight(
    seg_path: Path,
    manifest_entry: VolumeMetadata,
    ontology: Ontology | None,
    *,
    spatial_tolerance: float = 0.01,
) -> list[IssueRecord]:
    """Validate a segmentation NRRD against the volume metadata and ontology.

    Parameters
    ----------
    seg_path : Path
        Path to a ``.seg.nrrd`` file.
    manifest_entry : VolumeMetadata
        Plain spatial metadata of the raw volume: ``shape``, ``origin_lps``,
        ``space_directions`` (see :data:`VolumeMetadata`).
    ontology : Ontology | None
        The ontology this segmentation must conform to.  ``None`` skips all
        ontology-specific checks (structural integrity is still enforced);
        an *unconstrained* ontology enforces its structural ``constraints``;
        a constrained ontology enforces its exact label set, including at
        the voxel level.
    spatial_tolerance : float
        Tolerance in mm for spatial metadata comparison.

    Returns
    -------
    list[IssueRecord]
        Validation issues.  Empty means valid.  This function never raises:
        a file that cannot be read is reported as an error issue.
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

    try:
        label_map, space, space_origin, space_directions, segments = (
            _parse_seg_nrrd_header(seg_path)
        )
    except Exception as exc:  # pre-flight must never raise
        issues.append(
            IssueRecord(
                severity='error',
                message=f'Failed to read segmentation NRRD: {exc}',
            )
        )
        return issues

    # -- Multi-layer / 4D detection (must precede any np.allclose) --
    if label_map.ndim != 3 or _is_multilayer_directions(space_directions):
        issues.append(
            IssueRecord(
                severity='error',
                message=(
                    'multi-layer seg.nrrd not supported -- export a single '
                    'merged segmentation'
                ),
            )
        )
        return issues

    # -- Coordinate space --
    space_is_lps = _check_seg_space(issues, space)

    expected_shape = tuple(manifest_entry['shape'])  # type: ignore[arg-type]

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

    # -- Spatial consistency (skipped for non-LPS: the space error above is
    #    the actionable one; a numeric mismatch would only be noise) --
    if space_is_lps:
        expected_origin = np.array(manifest_entry['origin_lps'])
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

        expected_dirs = np.array(manifest_entry['space_directions'])
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

    # -- Label integrity: header declarations --
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
    if ontology is not None:
        if ontology.is_unconstrained:
            _check_unconstrained_conformance(issues, label_map, ontology)
        else:
            _check_seg_ontology_conformance(issues, segments, label_map, ontology)

    return issues


def _check_seg_ontology_conformance(
    issues: list[IssueRecord],
    segments: list[dict[str, object]],
    label_map: np.ndarray,
    ontology: Ontology,
) -> None:
    """Check a segmentation against a constrained ontology's label set.

    Enforces the label contract at two levels: the header-declared
    ``segments`` (missing / extra / mis-named labels) *and* the actual
    voxel values (a value present in the data but declared neither in the
    header nor in the ontology is an error, not a warning).
    """
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

    # Check for extra header-declared labels not in the ontology.
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

    # Voxel-level check: a value present in the data but declared neither in
    # the header nor in the ontology is an error (closes the hole where a
    # stray voxel value only warned).  Background (0) is always allowed.
    allowed = set(expected_labels) | {0}
    declared = set(seg_labels)
    voxel_values = {int(v) for v in np.unique(label_map)}
    for value in sorted(voxel_values - allowed - declared):
        issues.append(
            IssueRecord(
                severity='error',
                message=(
                    f'Voxel value {value} is present in the segmentation but '
                    f'is declared neither in the header nor in ontology '
                    f'{ontology.name!r} v{ontology.version}'
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
    manifest_entry: VolumeMetadata,
    ontology: Ontology | None,
) -> list[IssueRecord]:
    """Validate a landmark file against the volume metadata and ontology.

    Parameters
    ----------
    lmk_path : Path
        Path to a ``.mrk.json`` file.
    manifest_entry : VolumeMetadata
        Plain spatial metadata of the raw volume (see
        :data:`VolumeMetadata`).
    ontology : Ontology | None
        The ontology this landmark set must conform to.  ``None`` skips the
        ontology point-set and coordinate-system checks (structural checks
        still run).

    Returns
    -------
    list[IssueRecord]
        Validation issues.  Empty means valid.  This function never raises:
        a file that cannot be read is reported as an error issue.
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

    try:
        points, labels, coord_system = _parse_mrk_json(lmk_path)
    except Exception as exc:  # pre-flight must never raise
        issues.append(
            IssueRecord(
                severity='error',
                message=f'Failed to read landmarks JSON: {exc}',
            )
        )
        return issues

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

    # -- Coordinate system matches the ontology's declared frame --
    if (
        ontology is not None
        and ontology.coordinate_system is not None
        and coord_system in ('LPS', 'RAS')
        and coord_system != ontology.coordinate_system
    ):
        issues.append(
            IssueRecord(
                severity='warning',
                message=(
                    f'Landmark coordinate system {coord_system!r} differs from '
                    f'ontology {ontology.name!r} expected '
                    f'{ontology.coordinate_system!r}; points will be converted '
                    f'to LPS on write'
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
        origin = np.array(manifest_entry['origin_lps'])
        shape = np.array(manifest_entry['shape'])
        space_dirs = np.array(manifest_entry['space_directions'])

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
    if (
        ontology is not None
        and ontology.type == 'landmarks'
        and ontology.points is not None
    ):
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

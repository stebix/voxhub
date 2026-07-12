"""Integrate 3D Slicer annotations back into zarr stores.

Reads validated segmentation label maps and landmark points from a
staging directory and writes them as annotator-scoped groups/arrays in
the corresponding zarr stores.

This module is the **local** integration logic.  It knows nothing about
server-specific concerns (annotator ID, session ID, locks).  Server
wrappers add provenance on top.
"""

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import zarr
from rich.console import Console

from voxhub_core.extraction import extract_spatial_metadata
from voxhub_core.slicer import (
    LandmarkData,
    SegmentationData,
    parse_mrk_json,
    parse_seg_nrrd,
)
from voxhub_schema import (
    UNCONSTRAINED_SEGMENTATION,
    IssueRecord,
    Ontology,
    generate_nano_id,
    validate_lmk_preflight,
    validate_seg_preflight,
)


def find_annotation_files(
    store_dir: Path,
) -> tuple[Path | None, Path | None]:
    """Scan a staging store directory for Slicer annotation files.

    Parameters
    ----------
    store_dir : Path
        Directory to scan.

    Returns
    -------
    tuple[Path | None, Path | None]
        ``(segmentation_path, landmarks_path)`` -- ``None`` if not found.
    """
    seg_path = None
    lmk_path = None

    seg_candidates = list(store_dir.glob('*.seg.nrrd'))
    if seg_candidates:
        seg_path = seg_candidates[0]

    lmk_candidates = list(store_dir.glob('*.mrk.json'))
    if lmk_candidates:
        lmk_path = lmk_candidates[0]

    return seg_path, lmk_path


def write_segmentation_to_zarr(
    zarr_path: Path,
    seg: SegmentationData,
    group_path: str,
    *,
    ontology: Ontology | None = None,
    force: bool = False,
) -> None:
    """Write a segmentation label map into a zarr store.

    Parameters
    ----------
    zarr_path : Path
        Path to the ``.zarr`` store.
    seg : SegmentationData
        Validated segmentation data.
    group_path : str
        Slash-separated path within the zarr hierarchy.
    ontology : Ontology | None
        Ontology to record in array attributes.
    force : bool
        Overwrite existing group/array if present.
    """
    root = zarr.open_group(zarr_path, mode='r+')

    parts = group_path.strip('/').split('/')
    array_name = parts[-1]
    group_parts = parts[:-1]

    parent = root
    for part in group_parts:
        try:
            parent = parent[part]
        except KeyError:
            parent = parent.create_group(part)

    max_label = int(seg.label_map.max()) if seg.label_map.size > 0 else 0
    dtype = np.uint8 if max_label < 256 else np.uint16
    label_data = seg.label_map.astype(dtype)

    if array_name in parent:
        if not force:
            msg = (
                f"Array '{group_path}' already exists in {zarr_path}. "
                f'Pass force=True to overwrite.'
            )
            raise FileExistsError(msg)
        del parent[array_name]

    arr = parent.create_array(array_name, data=label_data)

    segments_meta = [
        {
            'id': s.id,
            'name': s.name,
            'label_value': s.label_value,
            'color': list(s.color),
        }
        for s in seg.segments
    ]
    attributes: dict[str, object] = {
        'segments': segments_meta,
        'coordinate_system': 'LPS',
        'integrated_at': datetime.now(UTC).isoformat(),
        'source_file': 'segmentation.seg.nrrd',
    }
    if ontology is not None:
        attributes['ontology'] = ontology.name
        attributes['ontology_version'] = ontology.version

    arr.update_attributes(attributes)


def write_landmarks_to_zarr(
    zarr_path: Path,
    lmk: LandmarkData,
    group_path: str,
    *,
    ontology: Ontology | None = None,
    force: bool = False,
) -> None:
    """Write landmark points into a zarr store.

    Parameters
    ----------
    zarr_path : Path
        Path to the ``.zarr`` store.
    lmk : LandmarkData
        Validated landmark data.
    group_path : str
        Slash-separated path within the zarr hierarchy.
    ontology : Ontology | None
        Ontology to record in array attributes.
    force : bool
        Overwrite existing group/array if present.
    """
    root = zarr.open_group(zarr_path, mode='r+')

    parts = group_path.strip('/').split('/')
    array_name = parts[-1]
    group_parts = parts[:-1]

    parent = root
    for part in group_parts:
        try:
            parent = parent[part]
        except KeyError:
            parent = parent.create_group(part)

    points = lmk.points.copy()
    coord_system = lmk.coordinate_system
    if coord_system == 'RAS':
        points[:, 0] *= -1
        points[:, 1] *= -1
        coord_system = 'LPS'

    if array_name in parent:
        if not force:
            msg = (
                f"Array '{group_path}' already exists in {zarr_path}. "
                f'Pass force=True to overwrite.'
            )
            raise FileExistsError(msg)
        del parent[array_name]

    arr = parent.create_array(array_name, data=points)
    attributes: dict[str, object] = {
        'labels': lmk.labels,
        'coordinate_system': coord_system,
        'original_coordinate_system': lmk.coordinate_system,
        'integrated_at': datetime.now(UTC).isoformat(),
        'source_file': 'landmarks.mrk.json',
    }
    if ontology is not None:
        attributes['ontology'] = ontology.name
        attributes['ontology_version'] = ontology.version

    arr.update_attributes(attributes)


def integrate(
    staging_dir: str | Path,
    stores_dir: str | Path,
    *,
    annotator_id: str,
    nano_id: str,
    ontology: Ontology | None = None,
    unconstrained: bool = False,
    force: bool = False,
    validate_only: bool = False,
    console: Console | None = None,
) -> dict[str, list[IssueRecord]]:
    """Integrate annotations from a staging directory into zarr stores.

    This is the **local** integration entrypoint.  It validates
    annotations then writes them to annotator-scoped zarr paths.

    Ontology policy
    ---------------
    Callers must state intent explicitly via exactly one of:

    * ``ontology=<Ontology>`` — the declared ontology is recorded in
      provenance *and* enforced against the annotation's labels/points.
      If the declared ontology's ``type`` does not match a staged
      annotation's type (segmentation vs. landmarks), that annotation is
      rejected with an error issue rather than silently unconstrained.
    * ``unconstrained=True`` — explicit opt-in to no-ontology
      integration.  Provenance records ``ontology='unconstrained'``.

    Passing both or neither is a :class:`ValueError` at entry.  Silent
    fallback to "unconstrained" would corrupt the ground-truth story by
    making a missing declaration indistinguishable from a deliberate
    no-ontology integration.

    Parameters
    ----------
    staging_dir : str | Path
        Staging directory containing staged NRRDs and annotation files.
    stores_dir : str | Path
        Directory containing the ``.zarr`` stores.
    annotator_id : str
        Annotator identifier (e.g. ``'alice'``).
    nano_id : str
        8-char nano-ID associated with the annotator.
    ontology : Ontology | None
        Ontology to validate against and record.  Mutually exclusive
        with ``unconstrained``.
    unconstrained : bool
        Explicit opt-in to unconstrained integration.  Mutually
        exclusive with ``ontology``.
    force : bool
        Overwrite existing annotations / ignore errors.
    validate_only : bool
        Only validate, don't write to zarr.
    console : Console | None
        Optional rich Console.

    Returns
    -------
    dict[str, list[IssueRecord]]
        Per-store validation issues.

    Raises
    ------
    ValueError
        If the ontology policy is ambiguous: neither or both of
        ``ontology`` and ``unconstrained`` specified.
    RuntimeError
        If validation errors prevent integration and *force* is False.
    """
    if ontology is None and not unconstrained:
        msg = (
            'integrate() requires an explicit ontology policy: pass '
            '`ontology=<Ontology>` for enforced integration, or '
            '`unconstrained=True` to explicitly opt out.'
        )
        raise ValueError(msg)
    if ontology is not None and unconstrained:
        msg = (
            '`ontology` and `unconstrained=True` are mutually exclusive; '
            'pass one or the other.'
        )
        raise ValueError(msg)

    staging_dir = Path(staging_dir)
    stores_dir = Path(stores_dir)
    console = console or Console()

    # Per-annotation-type ontology resolution.  Under --unconstrained the
    # segmentation is validated against the shipped ``unconstrained``
    # ontology so its structural constraints (non_negative_integers,
    # sequential_from_zero, background_at_zero) are actually enforced --
    # there is no unconstrained landmark ontology, so landmarks stay None.
    # Under a declared ontology, only the matching annotation type receives
    # it; the other type triggers a type-mismatch error below rather than
    # silently defaulting to unconstrained — that would misrepresent the
    # provenance record.
    if unconstrained:
        seg_ontology: Ontology | None = UNCONSTRAINED_SEGMENTATION
        lmk_ontology: Ontology | None = None
    else:
        assert ontology is not None  # entry-point check guarantees this
        seg_ontology = ontology if ontology.type == 'segmentation' else None
        lmk_ontology = ontology if ontology.type == 'landmarks' else None

    all_issues: dict[str, list[IssueRecord]] = {}
    stores_to_integrate: list[
        tuple[
            str,
            Path,
            SegmentationData | None,
            LandmarkData | None,
        ]
    ] = []
    has_errors = False

    # Discover stores in the staging directory.
    for store_dir in sorted(staging_dir.iterdir()):
        if not store_dir.is_dir() or store_dir.name.startswith('.'):
            continue

        store_name = store_dir.name
        zarr_path = stores_dir / f'{store_name}.zarr'

        if not zarr_path.is_dir():
            console.print(f'  [yellow]{store_name}: zarr store not found (skip)[/yellow]')
            continue

        seg_file, lmk_file = find_annotation_files(store_dir)

        if seg_file is None and lmk_file is None:
            console.print(f'  [dim]{store_name}: no annotations found (skip)[/dim]')
            continue

        console.print(f'  Validating [green]{store_name}[/green] ...')
        issues: list[IssueRecord] = []
        seg_data: SegmentationData | None = None
        lmk_data: LandmarkData | None = None

        # Read volume metadata for validation.
        root = zarr.open_group(zarr_path, mode='r')
        arr = root['raw']['full']
        vol_attrs = dict(arr.attrs)
        origin, space_directions, spacing_mm = extract_spatial_metadata(vol_attrs)
        manifest_entry = {
            'shape': list(arr.shape),
            'origin_lps': origin.tolist(),
            'space_directions': space_directions.tolist(),
            'spacing_mm': spacing_mm,
        }

        if seg_file is not None:
            if not unconstrained and seg_ontology is None:
                assert ontology is not None
                issues.append(
                    IssueRecord(
                        severity='error',
                        message=(
                            f'Declared ontology {ontology.name!r} is type '
                            f'{ontology.type!r} but staging contains a '
                            f'segmentation file. Declare a segmentation '
                            f'ontology or pass `unconstrained=True` to opt '
                            f'out explicitly.'
                        ),
                    )
                )
            else:
                seg_issues = validate_seg_preflight(
                    seg_file, manifest_entry, seg_ontology
                )
                issues.extend(seg_issues)
                try:
                    seg_data = parse_seg_nrrd(seg_file)
                    console.print(
                        f'    segmentation: {seg_file.name}  '
                        f'shape={seg_data.label_map.shape}  '
                        f'segments={len(seg_data.segments)}'
                    )
                except Exception as exc:
                    issues.append(
                        IssueRecord(
                            severity='error',
                            message=f'Failed to parse segmentation: {exc}',
                        )
                    )

        if lmk_file is not None:
            if not unconstrained and lmk_ontology is None:
                assert ontology is not None
                issues.append(
                    IssueRecord(
                        severity='error',
                        message=(
                            f'Declared ontology {ontology.name!r} is type '
                            f'{ontology.type!r} but staging contains a '
                            f'landmarks file. Declare a landmarks ontology '
                            f'or pass `unconstrained=True` to opt out '
                            f'explicitly.'
                        ),
                    )
                )
            else:
                lmk_issues = validate_lmk_preflight(
                    lmk_file, manifest_entry, lmk_ontology
                )
                issues.extend(lmk_issues)
                try:
                    lmk_data = parse_mrk_json(lmk_file)
                    console.print(
                        f'    landmarks: {lmk_file.name}  '
                        f'points={len(lmk_data.labels)}  '
                        f'system={lmk_data.coordinate_system}'
                    )
                except Exception as exc:
                    issues.append(
                        IssueRecord(
                            severity='error',
                            message=f'Failed to parse landmarks: {exc}',
                        )
                    )

        for issue in issues:
            style = 'red' if issue.severity == 'error' else 'yellow'
            console.print(f'    [{style}]{issue.severity}: {issue.message}[/{style}]')

        all_issues[store_name] = issues

        errors = [i for i in issues if i.severity == 'error']
        if errors:
            has_errors = True
        else:
            stores_to_integrate.append((store_name, zarr_path, seg_data, lmk_data))

    if validate_only:
        n_valid = len(stores_to_integrate)
        n_errors = sum(
            1
            for issues in all_issues.values()
            if any(i.severity == 'error' for i in issues)
        )
        console.print(
            f'\n[bold]Validation complete:[/bold] {n_valid} valid, {n_errors} with errors'
        )
        return all_issues

    if has_errors and not force:
        msg = (
            'Validation errors found. Fix issues or pass --force to '
            'integrate stores without errors anyway.'
        )
        raise RuntimeError(msg)

    # Write to zarr with annotator-scoped paths.
    integrated_count = 0
    date_str = datetime.now(UTC).strftime('%Y%m%d')
    annotator_dir = f'{annotator_id}-{nano_id}'

    for store_name, zarr_path, seg_data, lmk_data in stores_to_integrate:
        console.print(f'  Integrating [green]{store_name}[/green] ...')

        try:
            if seg_data is not None:
                short_random = generate_nano_id(size=4)
                ont_name = seg_ontology.name if seg_ontology else 'unconstrained'
                instance_dir = f'{ont_name}-{date_str}-{short_random}'
                seg_path = f'annotations/{annotator_dir}/{instance_dir}/data'
                write_segmentation_to_zarr(
                    zarr_path,
                    seg_data,
                    seg_path,
                    ontology=seg_ontology,
                    force=force,
                )
                console.print(f'    wrote segmentation -> {seg_path}')

            if lmk_data is not None:
                short_random = generate_nano_id(size=4)
                ont_name = lmk_ontology.name if lmk_ontology else 'unconstrained'
                instance_dir = f'{ont_name}-{date_str}-{short_random}'
                lmk_path = f'annotations/{annotator_dir}/{instance_dir}/data'
                write_landmarks_to_zarr(
                    zarr_path,
                    lmk_data,
                    lmk_path,
                    ontology=lmk_ontology,
                    force=force,
                )
                console.print(f'    wrote landmarks -> {lmk_path}')

            integrated_count += 1

        except Exception as exc:
            console.print(f'    [red]Failed to integrate {store_name}: {exc}[/red]')
            all_issues.setdefault(store_name, []).append(
                IssueRecord(severity='error', message=str(exc))
            )

    console.print(f'\n[bold]{integrated_count}[/bold] store(s) integrated.')
    return all_issues

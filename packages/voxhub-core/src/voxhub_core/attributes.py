"""Read, write, and validate dataset attributes on zarr stores.

Dataset attributes are structured metadata stored in the zarr root group
under the ``"dataset_attributes"`` key. They provide a human-curated,
queryable view of domain-specific properties (modality, resolution,
origin, tags).

See ``docs/dataset-attribute-feature.md`` for design rationale.
"""

from pathlib import Path
from typing import Any

import attrs
import zarr

from voxhub_schema import (
    DatasetAttributes,
    LengthUnit,
    WriteMode,
)

DATASET_ATTRIBUTES_KEY = 'dataset_attributes'


def dataset_attributes_to_dict(
    attributes: DatasetAttributes,
) -> dict[str, Any]:
    """Convert DatasetAttributes to a JSON-serializable dict for zarr attrs."""
    d = attrs.asdict(attributes)
    # Convert enum values to their string representation.
    d['modality'] = attributes.modality.value
    d['resolution']['unit'] = attributes.resolution.unit.value
    return d


def set_dataset_attributes(
    store_path: str | Path,
    attributes: DatasetAttributes,
    *,
    mode: WriteMode = WriteMode.CREATE,
) -> None:
    """Write dataset attributes to a zarr store's root group.

    Parameters
    ----------
    store_path
        Path to the ``.zarr`` store directory.
    attributes
        The dataset attributes to write.
    mode
        Write mode: CREATE (error if exists), REPLACE (overwrite),
        or MERGE (merge tags, overwrite typed fields).

    Raises
    ------
    FileExistsError
        If *mode* is CREATE and attributes already exist.
    """
    store_path = Path(store_path)
    root = zarr.open_group(store_path, mode='r+')
    existing = dict(root.attrs).get(DATASET_ATTRIBUTES_KEY)

    if mode is WriteMode.CREATE and existing is not None:
        msg = (
            f'Dataset attributes already exist on {store_path.name}. '
            f'Use WriteMode.REPLACE or WriteMode.MERGE to update.'
        )
        raise FileExistsError(msg)

    new_dict = dataset_attributes_to_dict(attributes)

    if mode is WriteMode.MERGE and existing is not None:
        merged: dict[str, Any] = dict(existing)  # type: ignore[arg-type]
        merged['modality'] = new_dict['modality']
        merged['resolution'] = new_dict['resolution']
        merged['origin'] = new_dict['origin']
        merged_tags = dict(merged.get('tags', {}))
        merged_tags.update(new_dict.get('tags', {}))
        merged['tags'] = merged_tags
        new_dict = merged

    all_attrs = dict(root.attrs)
    all_attrs[DATASET_ATTRIBUTES_KEY] = new_dict  # type: ignore[assignment]
    root.update_attributes(all_attrs)


def get_dataset_attributes(
    store_path: str | Path,
) -> DatasetAttributes | None:
    """Read dataset attributes from a zarr store's root group.

    Parameters
    ----------
    store_path
        Path to the ``.zarr`` store directory.

    Returns
    -------
    DatasetAttributes | None
        Parsed attributes, or ``None`` if not set.
    """
    store_path = Path(store_path)
    root = zarr.open_group(store_path, mode='r')
    raw = dict(root.attrs).get(DATASET_ATTRIBUTES_KEY)
    if raw is None:
        return None
    return DatasetAttributes.from_dict(raw)  # type: ignore[arg-type]


@attrs.define
class ValidationIssue:
    """A mismatch between dataset attributes and array metadata."""

    field: str
    declared: object
    actual: object
    message: str


_UNIT_TO_MM: dict[LengthUnit, float] = {
    LengthUnit.METER: 1000.0,
    LengthUnit.CENTIMETER: 10.0,
    LengthUnit.MILLIMETER: 1.0,
    LengthUnit.MICROMETER: 0.001,
}


def _to_mm(value: float, unit: LengthUnit) -> float:
    """Convert a length value to millimeters."""
    return value * _UNIT_TO_MM[unit]


def validate_dataset_attributes(
    store_path: str | Path,
    *,
    tolerance: float = 1e-2,
) -> list[ValidationIssue]:
    """Check consistency between dataset attributes and array metadata.

    Compares the manually-set resolution in dataset attributes against
    the ``spacing_mm`` derived from the ``raw/full`` array attrs. Reports
    mismatches as warnings.

    Parameters
    ----------
    store_path
        Path to the ``.zarr`` store directory.
    tolerance
        Absolute tolerance in mm for floating-point comparison.

    Returns
    -------
    list[ValidationIssue]
        Empty if attributes are consistent or absent.
    """
    store_path = Path(store_path)
    da = get_dataset_attributes(store_path)
    if da is None:
        return []

    issues: list[ValidationIssue] = []

    root = zarr.open_group(store_path, mode='r')
    arr = root['raw']['full']  # type: ignore[index]
    arr_attrs = dict(arr.attrs)  # type: ignore[union-attr]

    spacing_mm_raw = arr_attrs.get('spacing_mm')
    if spacing_mm_raw is None:
        # Try to derive from space directions if available.
        return issues

    spacing_mm = [float(v) for v in spacing_mm_raw]  # type: ignore[union-attr]
    declared_mm = [_to_mm(v, da.resolution.unit) for v in da.resolution.voxel_size]

    mismatches = [
        abs(d - a) > tolerance for d, a in zip(declared_mm, spacing_mm, strict=True)
    ]
    if any(mismatches):
        issues.append(
            ValidationIssue(
                field='resolution.voxel_size',
                declared=list(da.resolution.voxel_size),
                actual=spacing_mm,
                message=(
                    'Declared resolution does not match array spacing_mm '
                    f'(declared {declared_mm} mm vs actual {spacing_mm} mm, '
                    f'tolerance={tolerance} mm)'
                ),
            )
        )

    return issues

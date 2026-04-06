# Dataset Attributes Feature

**Status**: Agreed — ready for implementation
**Date**: 2026-04-04

## Motivation

All zarr stores currently appear as a flat list in the catalog. Users cannot
distinguish stores by domain-specific properties (resolution tier, modality,
anatomical region, acquisition site, etc.) without pulling the data first.

Dataset attributes attach structured, queryable metadata to each zarr store so
users can browse, filter, and select stores before pulling.

## Design Decisions

### Storage location

Attributes live in the **zarr root group attrs** under a dedicated
`"dataset_attributes"` key:

```
store.zarr/
  zarr.json          <- root group
  attrs              <- { "dataset_attributes": { ... } }
  raw/
    full/
      attrs          <- DICOM/spatial metadata (unchanged)
      ...
```

The `raw/full` array attrs remain unchanged — they hold DICOM-derived spatial
metadata. Dataset attributes are a separate, higher-level concern.

**Important**: The dataset attributes are a **convenience view** of information
that is ultimately rooted in the underlying DICOM-derived zarr metadata. They
are manually set and may fall out of sync with the ground-truth metadata in
`raw/full` attrs. The `validate-attributes` command (see below) exists to
detect such drift. The authoritative source for spatial metadata remains the
`raw/full` array attrs — dataset attributes provide a human-curated,
semantically richer layer on top.

### Why manual, not auto-derived

Attributes are set manually via scripts after store creation. They are **not**
auto-derived from DICOM metadata because:

- Origin/site cannot be reliably parsed from DICOM tags.
- Modality distinctions relevant to this project (e.g. FPVCT vs FPVCT_SECO)
  don't map cleanly to DICOM's `Modality` tag.
- The directory naming conventions already encode this information — scripts
  formalize it into structured zarr attrs.

### Write semantics

Three modes for `set_dataset_attributes()`:

```python
class WriteMode(enum.Enum):
    CREATE = "create"     # error if attributes already exist
    REPLACE = "replace"   # overwrite entirely
    MERGE = "merge"       # merge tags, overwrite typed fields if provided
```

`CREATE` is the default — safe against accidental overwrites. `REPLACE` is the
explicit "I know what I'm doing" mode. `MERGE` is useful for adding tags
incrementally without touching the typed fields.

### Access model

**Server-side only.** There is no remote/SSH command for setting attributes.
Annotators are consumers of this metadata — they use it to decide which stores
to pull. Only admins with direct server access set attributes via scripts.

### Filtering

- Multiple `--tag` / `--modality` / etc. flags use **AND** semantics.
- **Exact match** only — no glob or substring matching.
- Typed fields get **separate CLI flags** (e.g. `--modality CT`) for
  discoverability and validation. Freeform tags use `--tag key=value`.

## Schema

All types defined in **voxhub-schema** (`models.py` or a dedicated
`dataset_attributes.py`).

### Enums

```python
class LengthUnit(enum.Enum):
    METER = "m"
    CENTIMETER = "cm"
    MILLIMETER = "mm"
    MICROMETER = "um"


class Modality(enum.Enum):
    MRI = "MRI"
    """General Magnetic Resonance Imaging."""

    MSCT = "MSCT"
    """Multislice Computed Tomography."""

    CBCT = "CBCT"
    """Cone Beam Computed Tomography."""

    FPVCT = "FPVCT"
    """Flat Panel Volume Computed Tomography."""

    FPVCT_SECO = "FPVCT_SECO"
    """FPVCT with secondary reconstruction."""

    PTCT = "PTCT"
    """Photon Counting Computed Tomography."""
```

### Resolution

```python
@attrs.define
class Resolution:
    """Voxel resolution with explicit unit.

    This is a convenience view derived from the underlying spatial metadata
    (spacing_mm in raw/full attrs). The authoritative source is the array-level
    metadata — this provides a human-readable, unit-aware representation.
    """

    voxel_size: tuple[float, float, float]
    unit: LengthUnit
    isotropic: bool = attrs.field(init=False)

    def __attrs_post_init__(self) -> None:
        self.isotropic = (
            self.voxel_size[0] == self.voxel_size[1] == self.voxel_size[2]
        )

    @classmethod
    def create_isotropic(cls, voxel_size: float, unit: LengthUnit | str) -> Self:
        """Create a resolution with equal voxel size in all dimensions."""
        if not isinstance(unit, LengthUnit):
            unit = LengthUnit(unit)
        return cls(voxel_size=(voxel_size, voxel_size, voxel_size), unit=unit)
```

### DatasetAttributes

```python
@attrs.define
class DatasetAttributes:
    """Structured metadata describing a zarr store's dataset properties.

    These attributes are a manually curated convenience view. They may fall
    out of sync with the ground-truth DICOM-derived metadata in raw/full
    attrs. Use ``validate-attributes`` to check consistency.
    """

    modality: Modality
    resolution: Resolution
    origin: str
    """Freeform string identifying where the data came from."""

    tags: dict[str, str] = attrs.Factory(dict)
    """Freeform key-value pairs for additional metadata."""

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> Self: ...
```

All fields on `DatasetAttributes` are **required** (except `tags`). A store
either has complete dataset attributes or none. This is enforced consistently
since the system has not yet been deployed.

### Tags

The freeform `tags` dict is **unconstrained** — no limits on key/value length,
character set, or count. Constraints can be added later if needed.

## Interface

### Python API (voxhub-core)

#### `set_dataset_attributes`

```python
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
    """
```

#### `get_dataset_attributes`

```python
def get_dataset_attributes(
    store_path: str | Path,
) -> DatasetAttributes | None:
    """Read dataset attributes from a zarr store's root group.

    Returns None if no dataset attributes are set.
    """
```

#### `validate_dataset_attributes`

```python
def validate_dataset_attributes(
    store_path: str | Path,
) -> list[ValidationIssue]:
    """Check consistency between dataset attributes and array metadata.

    Compares the manually-set resolution in dataset attributes against
    the spacing_mm derived from the raw/full array attrs. Reports
    mismatches as warnings.

    Returns an empty list if attributes are consistent or absent.
    """
```

The validation normalizes both values to the same unit before comparing,
and uses a tolerance for floating-point comparison. Mismatches are reported
as warnings, not errors — the ground-truth is always `raw/full` attrs.

### Server CLI (voxhub-server)

#### `validate-attributes`

```
voxhub-server validate-attributes <zarr_root> [--stores store1 store2 ...]
```

Runs `validate_dataset_attributes` across all (or selected) stores in a zarr
root. Outputs a JSON report:

```json
{
  "protocol_version": 1,
  "results": {
    "patient-001": { "status": "ok", "issues": [] },
    "patient-002": {
      "status": "warning",
      "issues": [
        {
          "field": "resolution.voxel_size",
          "declared": [0.5, 0.5, 0.5],
          "actual_spacing_mm": [0.488, 0.488, 0.625],
          "message": "Declared isotropic resolution does not match array spacing"
        }
      ]
    },
    "patient-003": { "status": "missing", "issues": [] }
  }
}
```

No `set-attributes` CLI command — attribute setting is done via Python scripts
that import `set_dataset_attributes` directly. This avoids a clunky CLI
interface for structured data.

### Client CLI (voxhub)

#### `remote-catalog` updates

Display dataset attributes under each store node:

```
server:/zarr_root
├── patient-001.zarr  128 x 512 x 512
│   FPVCT  0.5 x 0.5 x 0.5 mm (isotropic)  origin=clinic-A
│   └── inner-ear-structures v1 by alice at 2026-03-31T15:00:00
├── patient-002.zarr  256 x 256 x 256
│   MRI  0.3 x 0.3 x 0.6 mm  origin=clinic-B  dataset=training
│   └── (no annotations)
```

New filter flags:

```
voxhub remote-catalog user@host:/root --modality FPVCT
voxhub remote-catalog user@host:/root --modality CT --tag dataset=training
voxhub remote-catalog user@host:/root --tag origin=clinic-A
```

#### `pull` updates

Same filter flags available on `pull` to select a subset of stores:

```
voxhub pull user@host:/root ./local --modality FPVCT
voxhub pull user@host:/root ./local --tag origin=clinic-A
```

## Read Path Changes

### catalog.py

`_probe_zarr()` reads `root.attrs.get("dataset_attributes")` from the root
group. If present, validates with `DatasetAttributes.from_dict()` and attaches
to `ZarrEntry`. If absent, the field is `None`.

### server/cli.py — list-stores

`_run_list_stores()` includes `"dataset_attributes"` in each store's response
dict. Stores without attributes emit `"dataset_attributes": null`.

### models.py — StoreInfo

Add `dataset_attributes: DatasetAttributes | None = None` to `StoreInfo` and
update `from_dict()`.

## Affected Files

| Package | File | Change |
|---------|------|--------|
| voxhub-schema | `models.py` | Add `LengthUnit`, `Modality`, `Resolution`, `DatasetAttributes`, `WriteMode`; update `StoreInfo` |
| voxhub-core | `attributes.py` (new) | `set_dataset_attributes()`, `get_dataset_attributes()`, `validate_dataset_attributes()` |
| voxhub-core | `catalog.py` | Read `dataset_attributes` from root group in `_probe_zarr()` |
| voxhub-core | `export.py` | Accept optional `DatasetAttributes` on `export_zarr()` |
| voxhub-core | `server/cli.py` | Include in list-stores response; add `validate-attributes` subcommand |
| voxhub-client | `cli.py` | Display attributes in catalog; add `--modality` / `--tag` filters to `remote-catalog` and `pull` |

## Migration / Backward Compatibility

- Existing stores have no `dataset_attributes` key in root attrs. All read
  paths treat missing attributes as `None` — no migration needed.
- The `list-stores` response adds a new optional field. Clients that don't
  understand it ignore it. Protocol version bump is **not** required since
  the field is additive and optional.
- Since the system has not yet been deployed, all stores will have dataset
  attributes set from initial setup.

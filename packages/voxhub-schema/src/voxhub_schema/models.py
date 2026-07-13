"""SSH protocol data models.

Every JSON message that crosses the SSH pipe between client and server is
defined here as an ``attrs`` class.  Serialization:
``json.dumps(attrs.asdict(obj))``.  Deserialization: manual
``from_dict()`` classmethods.
"""

import enum
import json
import re
from typing import Literal, Self, cast, get_args

import attrs

PROTOCOL_VERSION: int = 2
"""Current wire protocol version.  Bumped on breaking changes.

Version 2: requests travel as a single JSON object on the server
process's stdin (``voxhub-server rpc``) instead of argv flags, and
integrate checksums are path-keyed :class:`ChecksumEntry` objects
instead of packed ``<filename>:sha256:<hex>`` strings.
"""

_SHA256_HEX64 = re.compile(r'[0-9a-f]{64}')
"""Bare sha256 digest shape: exactly 64 lowercase hex characters."""


# -- Dataset attributes ------------------------------------------------------


class LengthUnit(enum.Enum):
    """Physical length unit for voxel resolution."""

    METER = 'm'
    CENTIMETER = 'cm'
    MILLIMETER = 'mm'
    MICROMETER = 'um'


class Modality(enum.Enum):
    """Imaging modality of a dataset."""

    MRI = 'MRI'
    """General Magnetic Resonance Imaging."""

    MSCT = 'MSCT'
    """Multislice Computed Tomography."""

    CBCT = 'CBCT'
    """Cone Beam Computed Tomography."""

    FPVCT = 'FPVCT'
    """Flat Panel Volume Computed Tomography."""

    FPVCT_SECO = 'FPVCT_SECO'
    """FPVCT with secondary reconstruction."""

    PTCT = 'PTCT'
    """Photon Counting Computed Tomography."""


class WriteMode(enum.Enum):
    """Write semantics for ``set_dataset_attributes``."""

    CREATE = 'create'
    """Error if attributes already exist."""

    REPLACE = 'replace'
    """Overwrite entirely."""

    MERGE = 'merge'
    """Merge tags, overwrite typed fields if provided."""


@attrs.define
class Resolution:
    """Voxel resolution with explicit unit.

    This is a convenience view derived from the underlying spatial metadata
    (spacing_mm in raw/full attrs). The authoritative source is the
    array-level metadata — this provides a human-readable, unit-aware
    representation.
    """

    voxel_size: tuple[float, float, float]
    unit: LengthUnit
    isotropic: bool = attrs.field(init=False)

    def __attrs_post_init__(self) -> None:
        self.isotropic = self.voxel_size[0] == self.voxel_size[1] == self.voxel_size[2]

    @classmethod
    def create_isotropic(cls, voxel_size: float, unit: LengthUnit | str) -> Self:
        """Create a resolution with equal voxel size in all dimensions."""
        if not isinstance(unit, LengthUnit):
            unit = LengthUnit(unit)
        return cls(voxel_size=(voxel_size, voxel_size, voxel_size), unit=unit)

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> Self:
        """Deserialize from a plain dict."""
        raw_vs = d['voxel_size']
        if isinstance(raw_vs, (list, tuple)):
            voxel_size = tuple(float(v) for v in raw_vs)
        else:
            raise TypeError(f'Expected list for voxel_size, got {type(raw_vs)}')
        unit = LengthUnit(str(d['unit']))
        return cls(
            voxel_size=(voxel_size[0], voxel_size[1], voxel_size[2]),
            unit=unit,
        )


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
    def from_dict(cls, d: dict[str, object]) -> Self:
        """Deserialize from a plain dict."""
        resolution_raw = d['resolution']
        if not isinstance(resolution_raw, dict):
            raise TypeError(f'Expected dict for resolution, got {type(resolution_raw)}')
        tags_raw = d.get('tags', {})
        if not isinstance(tags_raw, dict):
            raise TypeError(f'Expected dict for tags, got {type(tags_raw)}')
        return cls(
            modality=Modality(str(d['modality'])),
            resolution=Resolution.from_dict(resolution_raw),
            origin=str(d['origin']),
            tags={str(k): str(v) for k, v in tags_raw.items()},
        )


# -- Shared types ------------------------------------------------------------

SEVERITY_LEVEL = Literal['error', 'warning']
"""Severity levels for issues surfaced to the client."""


@attrs.define
class IssueRecord:
    """A validation or integration issue surfaced to the client."""

    severity: SEVERITY_LEVEL
    message: str

    @classmethod
    def from_dict(cls, d: dict[str, str]) -> Self:
        """Deserialize from a plain dict."""
        if d['severity'] not in get_args(SEVERITY_LEVEL):
            raise ValueError(f'Invalid severity: {d["severity"]}')
        severity = cast('SEVERITY_LEVEL', d['severity'])
        return cls(severity=severity, message=d['message'])


@attrs.define
class AnnotationInfo:
    """Metadata about an existing annotation in a zarr store."""

    ontology: str
    ontology_version: int
    annotator_id: str
    integrated_at: str

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> Self:
        """Deserialize from a plain dict."""
        return cls(
            ontology=str(d['ontology']),
            ontology_version=int(d['ontology_version']),  # type: ignore[arg-type]
            annotator_id=str(d['annotator_id']),
            integrated_at=str(d['integrated_at']),
        )


# -- list-stores -------------------------------------------------------------


@attrs.define
class StoreInfo:
    """Metadata for a single zarr store returned by ``list-stores``."""

    name: str
    shape: list[int]
    dtype: str
    origin_lps: list[float]
    spacing_mm: list[float]
    space_directions: list[list[float]]
    annotations: list[AnnotationInfo]
    error: str | None = None
    dataset_attributes: DatasetAttributes | None = None

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> Self:
        """Deserialize from a plain dict."""
        annotations_raw = d.get('annotations', [])
        annotations = [
            AnnotationInfo.from_dict(a)  # type: ignore[arg-type]
            for a in annotations_raw  # type: ignore[union-attr]
        ]
        da_raw = d.get('dataset_attributes')
        dataset_attributes = (
            DatasetAttributes.from_dict(da_raw)  # type: ignore[arg-type]
            if da_raw is not None
            else None
        )
        return cls(
            name=str(d['name']),
            shape=list(d['shape']),  # type: ignore[arg-type]
            dtype=str(d['dtype']),
            origin_lps=list(d['origin_lps']),  # type: ignore[arg-type]
            spacing_mm=list(d['spacing_mm']),  # type: ignore[arg-type]
            space_directions=[
                list(row)  # type: ignore[arg-type]
                for row in d['space_directions']  # type: ignore[union-attr]
            ],
            annotations=annotations,
            error=d.get('error'),  # type: ignore[arg-type]
            dataset_attributes=dataset_attributes,
        )


# -- prepare-pull ------------------------------------------------------------


@attrs.define
class PrepareRequest:
    """Arguments for ``prepare-pull``.

    Mirrors the live server handler surface.  A staging-dir override is
    deliberately absent: the staging path is server-authoritative
    (minted under the operator-configured staging root) and must never
    be client-controllable.
    """

    store_name: str
    include_existing_annotations: list[str] | None = None
    compress: bool = False
    annotator_id: str | None = None
    """Optional identity for local/dev use.  Over SSH the key-bound
    ``VOXHUB_ANNOTATOR`` environment variable is authoritative and a
    disagreement fails the request."""

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> Self:
        """Deserialize from a plain dict."""
        include_raw = d.get('include_existing_annotations')
        if include_raw is not None and not isinstance(include_raw, list):
            raise TypeError(
                f'Expected list for include_existing_annotations, got {type(include_raw)}'
            )
        annotator_raw = d.get('annotator_id')
        return cls(
            store_name=str(d['store_name']),
            include_existing_annotations=(
                None if include_raw is None else [str(p) for p in include_raw]
            ),
            compress=bool(d.get('compress', False)),
            annotator_id=None if annotator_raw is None else str(annotator_raw),
        )


@attrs.define
class PrepareResponse:
    """Response from ``prepare-pull`` (single-store).

    ``prepare-pull`` operates on exactly one store per invocation; the
    volume metadata previously nested under ``stores[<name>]`` is now
    flattened onto this envelope.

    Parameters
    ----------
    protocol_version : int
    staging_dir : str
        Server-side staging directory the client should rsync.  The
        directory contains ``raw.nrrd``, ``.voxhub_pull.json``, and
        (optionally) a ``reference/`` subdirectory with exported
        reference annotations.
    server_host : str
        FQDN of the staging server, echoed for client-side audit log.
    server_stores_dir : str
        Absolute path to the operator-configured stores directory on
        the server.
    store_name : str
        Name of the store this staging dir originated from — the
        reintegration target for push.
    raw_name : str
        Filename of the raw volume in *staging_dir*.
    raw_checksum, shape, spacing_mm, origin_lps, space_directions
        Volume metadata (per :func:`voxhub_core.staging.stage`).
    skipped_annotations : list[dict[str, str]]
        Requested annotations that were not exported to reference files,
        with a human-readable reason per entry.  Non-fatal on the server
        side; the client surfaces this prominently so the annotator is
        never silently denied a reference file they asked for.  Each
        entry has keys ``'path'`` and ``'reason'``.
    memory_warnings : list[dict[str, object]]
        Soft memory advisories raised while staging (see
        ``voxhub_core.memory_budget.MemoryWarning.to_dict``).  Non-fatal;
        surfaced so the annotator knows the server was under pressure.
    """

    protocol_version: int
    staging_dir: str
    server_host: str
    server_stores_dir: str
    store_name: str
    raw_name: str
    raw_checksum: str
    shape: list[int]
    spacing_mm: list[float]
    origin_lps: list[float]
    space_directions: list[list[float]]
    skipped_annotations: list[dict[str, str]] = attrs.Factory(list)
    memory_warnings: list[dict[str, object]] = attrs.Factory(list)

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> Self:
        """Deserialize from a plain dict."""
        skipped_raw = d.get('skipped_annotations', []) or []
        skipped: list[dict[str, str]] = [
            {'path': str(e['path']), 'reason': str(e['reason'])}
            for e in skipped_raw  # type: ignore[union-attr]
        ]
        memory_raw = d.get('memory_warnings', []) or []
        memory_warnings: list[dict[str, object]] = [
            dict(w)  # type: ignore[call-overload]
            for w in memory_raw  # type: ignore[union-attr]
        ]
        return cls(
            protocol_version=int(d['protocol_version']),  # type: ignore[arg-type]
            staging_dir=str(d['staging_dir']),
            server_host=str(d['server_host']),
            server_stores_dir=str(d['server_stores_dir']),
            store_name=str(d['store_name']),
            raw_name=str(d['raw_name']),
            raw_checksum=str(d['raw_checksum']),
            shape=list(d['shape']),  # type: ignore[arg-type]
            spacing_mm=list(d['spacing_mm']),  # type: ignore[arg-type]
            origin_lps=list(d['origin_lps']),  # type: ignore[arg-type]
            space_directions=[
                list(row)  # type: ignore[arg-type]
                for row in d['space_directions']  # type: ignore[union-attr]
            ],
            skipped_annotations=skipped,
            memory_warnings=memory_warnings,
        )


# -- integrate-annotations ---------------------------------------------------


@attrs.define
class ChecksumEntry:
    """Integrity assertion for one staged annotation file.

    ``path`` is relative to the staging directory, with POSIX
    separators (``<store-dir>/<filename>``), so equal basenames in
    different store subdirectories stay distinct.  ``sha256`` is the
    bare 64-character lowercase hex digest of the file contents.
    """

    path: str
    sha256: str

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> Self:
        """Deserialize from a plain dict, validating the digest shape."""
        path = d['path']
        sha256 = d['sha256']
        if not isinstance(path, str) or not path:
            raise ValueError(f'checksum path must be a non-empty string, got {path!r}')
        if not isinstance(sha256, str) or _SHA256_HEX64.fullmatch(sha256) is None:
            raise ValueError(
                f'sha256 must be exactly 64 lowercase hex characters, got {sha256!r}'
            )
        return cls(path=path, sha256=sha256)


@attrs.define
class IntegratedAnnotation:
    """A single annotation written by ``integrate-annotations``."""

    path: str
    """Zarr-internal path of the written annotation array
    (``annotations/<annotator>-<nano_id>/<instance>/data``)."""

    ontology: str
    ontology_version: int

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> Self:
        """Deserialize from a plain dict."""
        return cls(
            path=str(d['path']),
            ontology=str(d['ontology']),
            ontology_version=int(d['ontology_version']),  # type: ignore[arg-type]
        )


@attrs.define
class IntegrateResult:
    """Per-store result from ``integrate-annotations``."""

    status: str
    annotations: list[IntegratedAnnotation]
    issues: list[IssueRecord]

    code: str | None = None
    """Machine-readable failure code, additive.  ``'validation_failed'``
    when the store failed because error-severity validation issues were
    present (never overridable by client flags); ``None`` otherwise."""

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> Self:
        """Deserialize from a plain dict."""
        annotations = [
            IntegratedAnnotation.from_dict(a)  # type: ignore[arg-type]
            for a in d.get('annotations', [])  # type: ignore[union-attr]
        ]
        issues = [
            IssueRecord.from_dict(i)  # type: ignore[arg-type]
            for i in d.get('issues', [])  # type: ignore[union-attr]
        ]
        code = d.get('code')
        return cls(
            status=str(d['status']),
            annotations=annotations,
            issues=issues,
            code=str(code) if code is not None else None,
        )


@attrs.define
class IntegrateRequest:
    """Arguments for ``integrate-annotations``.

    Mirrors the live server handler surface: exactly one of
    ``expected_ontology`` / ``unconstrained`` must be given (enforced
    server-side), and ``checksums`` carries staging-dir-relative,
    path-keyed integrity assertions.
    """

    staging_dir: str
    annotator_id: str
    machine_id: str
    nano_id: str
    checksums: list[ChecksumEntry] = attrs.Factory(list)
    expected_ontology: list[str] = attrs.Factory(list)
    unconstrained: bool = False
    force: bool = False

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> Self:
        """Deserialize from a plain dict."""
        checksums_raw = d.get('checksums') or []
        if not isinstance(checksums_raw, list):
            raise TypeError(f'Expected list for checksums, got {type(checksums_raw)}')
        expected_raw = d.get('expected_ontology') or []
        if not isinstance(expected_raw, list):
            raise TypeError(
                f'Expected list for expected_ontology, got {type(expected_raw)}'
            )
        return cls(
            staging_dir=str(d['staging_dir']),
            annotator_id=str(d['annotator_id']),
            machine_id=str(d['machine_id']),
            nano_id=str(d['nano_id']),
            checksums=[ChecksumEntry.from_dict(e) for e in checksums_raw],
            expected_ontology=[str(o) for o in expected_raw],
            unconstrained=bool(d.get('unconstrained', False)),
            force=bool(d.get('force', False)),
        )


@attrs.define
class IntegrateResponse:
    """Response from ``integrate-annotations``."""

    protocol_version: int
    stores: dict[str, IntegrateResult]

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> Self:
        """Deserialize from a plain dict."""
        stores_raw = d.get('stores', {})
        stores = {
            name: IntegrateResult.from_dict(info)  # type: ignore[arg-type]
            for name, info in stores_raw.items()  # type: ignore[union-attr]
        }
        return cls(
            protocol_version=int(d['protocol_version']),  # type: ignore[arg-type]
            stores=stores,
        )


# -- cleanup & gc ------------------------------------------------------------


@attrs.define
class CleanupResponse:
    """Response from ``cleanup``."""

    protocol_version: int
    status: str

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> Self:
        """Deserialize from a plain dict."""
        return cls(
            protocol_version=int(d['protocol_version']),  # type: ignore[arg-type]
            status=str(d['status']),
        )


@attrs.define
class GcResponse:
    """Response from ``gc``."""

    protocol_version: int
    removed: list[str]
    count: int

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> Self:
        """Deserialize from a plain dict."""
        return cls(
            protocol_version=int(d['protocol_version']),  # type: ignore[arg-type]
            removed=list(d.get('removed', [])),  # type: ignore[arg-type]
            count=int(d.get('count', 0)),  # type: ignore[arg-type]
        )


# -- error envelope ----------------------------------------------------------


@attrs.define
class ServerError:
    """Structured error envelope written to stdout on failure."""

    protocol_version: int
    error: bool
    code: str
    message: str

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> Self:
        """Deserialize from a plain dict."""
        return cls(
            protocol_version=int(d['protocol_version']),  # type: ignore[arg-type]
            error=bool(d.get('error', True)),
            code=str(d['code']),
            message=str(d['message']),
        )


# -- Serialization helpers ---------------------------------------------------


def _json_default(obj: object) -> object:
    """Handle non-standard types during JSON serialization."""
    if isinstance(obj, enum.Enum):
        return obj.value
    raise TypeError(f'Object of type {type(obj)} is not JSON serializable')


def serialize(obj: object) -> str:
    """Serialize an attrs instance to JSON.

    Parameters
    ----------
    obj
        Any attrs-decorated instance.

    Returns
    -------
    str
        Compact JSON string.
    """
    return json.dumps(attrs.asdict(obj), default=_json_default)  # type: ignore[arg-type]

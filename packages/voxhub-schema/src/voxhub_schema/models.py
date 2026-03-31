"""SSH protocol data models.

Every JSON message that crosses the SSH pipe between client and server is
defined here as an ``attrs`` class.  Serialization:
``json.dumps(attrs.asdict(obj))``.  Deserialization: manual
``from_dict()`` classmethods.
"""

import json
from typing import Literal, Self

import attrs

PROTOCOL_VERSION: int = 1
"""Current wire protocol version.  Bumped on breaking changes."""


# -- Shared types ------------------------------------------------------------


@attrs.define
class IssueRecord:
    """A validation or integration issue surfaced to the client."""

    severity: Literal['error', 'warning']
    message: str

    @classmethod
    def from_dict(cls, d: dict[str, str]) -> Self:
        """Deserialize from a plain dict."""
        return cls(severity=d['severity'], message=d['message'])


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

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> Self:
        """Deserialize from a plain dict."""
        annotations_raw = d.get('annotations', [])
        annotations = [
            AnnotationInfo.from_dict(a)  # type: ignore[arg-type]
            for a in annotations_raw  # type: ignore[union-attr]
        ]
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
        )


# -- prepare-pull ------------------------------------------------------------


@attrs.define
class PreparedStore:
    """Per-store data returned by ``prepare-pull``."""

    raw_checksum: str
    shape: list[int]
    spacing_mm: list[float]
    origin_lps: list[float]
    space_directions: list[list[float]]
    expected_ontologies: list[str]
    included_annotations: list[str]

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> Self:
        """Deserialize from a plain dict."""
        return cls(
            raw_checksum=str(d['raw_checksum']),
            shape=list(d['shape']),  # type: ignore[arg-type]
            spacing_mm=list(d['spacing_mm']),  # type: ignore[arg-type]
            origin_lps=list(d['origin_lps']),  # type: ignore[arg-type]
            space_directions=[
                list(row)  # type: ignore[arg-type]
                for row in d['space_directions']  # type: ignore[union-attr]
            ],
            expected_ontologies=list(
                d.get('expected_ontologies', [])  # type: ignore[arg-type]
            ),
            included_annotations=list(
                d.get('included_annotations', [])  # type: ignore[arg-type]
            ),
        )


@attrs.define
class PrepareRequest:
    """Arguments for ``prepare-pull``."""

    zarr_root: str
    store_names: list[str] | None = None
    ontologies: list[str] | None = None
    wip_dir: str | None = None
    include_existing_annotations: list[str] | None = None
    compress: bool = False


@attrs.define
class PrepareResponse:
    """Response from ``prepare-pull``."""

    protocol_version: int
    wip_dir: str
    stores: dict[str, PreparedStore]

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> Self:
        """Deserialize from a plain dict."""
        stores_raw = d.get('stores', {})
        stores = {
            name: PreparedStore.from_dict(info)  # type: ignore[arg-type]
            for name, info in stores_raw.items()  # type: ignore[union-attr]
        }
        return cls(
            protocol_version=int(d['protocol_version']),  # type: ignore[arg-type]
            wip_dir=str(d['wip_dir']),
            stores=stores,
        )


# -- integrate-annotations ---------------------------------------------------


@attrs.define
class IntegrateResult:
    """Per-store result from ``integrate-annotations``."""

    status: str
    annotations: list[AnnotationInfo]
    issues: list[IssueRecord]

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> Self:
        """Deserialize from a plain dict."""
        annotations = [
            AnnotationInfo.from_dict(a)  # type: ignore[arg-type]
            for a in d.get('annotations', [])  # type: ignore[union-attr]
        ]
        issues = [
            IssueRecord.from_dict(i)  # type: ignore[arg-type]
            for i in d.get('issues', [])  # type: ignore[union-attr]
        ]
        return cls(
            status=str(d['status']),
            annotations=annotations,
            issues=issues,
        )


@attrs.define
class IntegrateRequest:
    """Arguments for ``integrate-annotations``."""

    zarr_root: str
    wip_dir: str
    annotator_id: str
    machine_id: str
    nano_id: str
    checksums: list[str] | None = None
    force: bool = False


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
    return json.dumps(attrs.asdict(obj))  # type: ignore[arg-type]

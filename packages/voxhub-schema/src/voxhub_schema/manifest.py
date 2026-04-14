"""Remote manifest schema.

The manifest is written to the local staging directory after a pull and
updated after push/integration.  It records the server origin,
per-store spatial metadata, expected ontologies, and workflow status.
"""

import json
from pathlib import Path
from typing import Literal, Self

import attrs

type ManifestStatus = Literal['pulled', 'pushed', 'integrated']


@attrs.define
class RemoteManifestEntry:
    """Per-store entry in the remote manifest."""

    status: ManifestStatus
    raw_checksum: str
    shape: list[int]
    spacing_mm: list[float]
    origin_lps: list[float]
    space_directions: list[list[float]]
    expected_ontologies: list[str]
    included_annotations: list[str] = attrs.Factory(list)

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> Self:
        """Deserialize from a plain dict."""
        return cls(
            status=d['status'],  # type: ignore[arg-type]
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
class RemoteManifest:
    """Manifest written to the staging directory after ``pull``.

    Records the server target, protocol version, pull session ID,
    and per-store metadata + expected ontologies.

    Parameters
    ----------
    server_host : str
        SSH host string (``user@host``) the pull was issued against.
    server_stores_dir : str
        Absolute path to the server's configured stores directory, as
        reported by ``prepare-pull``.  Recorded for provenance only —
        the client never supplies or parses this.
    """

    server_host: str
    server_stores_dir: str
    protocol_version: int
    pull_session_id: str
    pulled_at: str
    stores: dict[str, RemoteManifestEntry]

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> Self:
        """Deserialize from a plain dict."""
        stores_raw = d.get('stores', {})
        stores = {
            name: RemoteManifestEntry.from_dict(entry)  # type: ignore[arg-type]
            for name, entry in stores_raw.items()  # type: ignore[union-attr]
        }
        return cls(
            server_host=str(d['server_host']),
            server_stores_dir=str(d['server_stores_dir']),
            protocol_version=int(d['protocol_version']),  # type: ignore[arg-type]
            pull_session_id=str(d['pull_session_id']),
            pulled_at=str(d['pulled_at']),
            stores=stores,
        )

    def to_json(self) -> str:
        """Serialize to a JSON string."""
        return json.dumps(attrs.asdict(self), indent=2)

    @classmethod
    def from_json(cls, text: str) -> Self:
        """Deserialize from a JSON string."""
        return cls.from_dict(json.loads(text))

    @classmethod
    def read(cls, staging_dir: Path) -> Self:
        """Read a manifest from a staging directory.

        Parameters
        ----------
        staging_dir : Path
            The local staging directory containing ``.voxhub_manifest.json``.

        Returns
        -------
        RemoteManifest

        Raises
        ------
        FileNotFoundError
            If the manifest file does not exist.
        """
        path = staging_dir / '.voxhub_manifest.json'
        if not path.exists():
            msg = f'No manifest found at {path}'
            raise FileNotFoundError(msg)
        return cls.from_json(path.read_text())

    def write(self, staging_dir: Path) -> None:
        """Write the manifest to a staging directory.

        Parameters
        ----------
        staging_dir : Path
            The local staging directory to write to.
        """
        path = staging_dir / '.voxhub_manifest.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json() + '\n')


@attrs.define
class PullAnnotationEntry:
    """One exported reference annotation in a pull session.

    Emitted by the server when ``prepare-pull`` exports an integrated
    annotation back to its source file format under ``reference/``.
    Read by the client (via :class:`PullManifest`) to verify reference
    file integrity, and later by push to build its reject-by-name /
    reject-by-hash sets.

    Parameters
    ----------
    zarr_source_path : str
        Instance-group path within the source zarr store, matching the
        format used by :class:`voxhub_core.catalog.AnnotationEntry.path`
        (e.g. ``annotations/alice-xyz45678/inner-ear-structures-20260101-ab12``).
    kind : str
        Annotation type — ``'segmentation'`` or ``'landmarks'``.
    ontology : str
        Ontology name recorded in the zarr array attrs.
    ontology_version : int
        Ontology version.
    annotator_id : str
        Annotator ID derived from the slug in *zarr_source_path*.
    integrated_at : str
        ISO timestamp from the zarr array attrs.
    reference_filename : str
        Filename (no directory component) of the exported reference file
        under ``<staging_dir>/reference/``.
    reference_checksum : str
        ``sha256:<hex>`` digest of the exported reference file.
    """

    zarr_source_path: str
    kind: str
    ontology: str
    ontology_version: int
    annotator_id: str
    integrated_at: str
    reference_filename: str
    reference_checksum: str

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> Self:
        """Deserialize from a plain dict."""
        return cls(
            zarr_source_path=str(d['zarr_source_path']),
            kind=str(d['kind']),
            ontology=str(d['ontology']),
            ontology_version=int(d['ontology_version']),  # type: ignore[arg-type]
            annotator_id=str(d['annotator_id']),
            integrated_at=str(d['integrated_at']),
            reference_filename=str(d['reference_filename']),
            reference_checksum=str(d['reference_checksum']),
        )


@attrs.define
class PullManifest:
    """Server-authoritative manifest emitted by ``prepare-pull``.

    Written to ``<staging_dir>/.voxhub_pull.json`` on the server and
    rsync'd down alongside the raw volume and reference annotations.
    Carries all data the client needs to verify, lock, and later push
    the session:

    * volume identity (checksum, spatial metadata)
    * reference annotation provenance (source zarr paths, checksums,
      exported filenames)
    * reintegration target (``store_name``)

    Consumed by the client after rsync (checksum verification, session
    locking, audit logging) and by push (reject-by-name / reject-by-hash
    sets).

    Parameters
    ----------
    protocol_version : int
        Wire-protocol version of the emitting server.
    prepared_at : str
        ISO UTC timestamp of manifest creation.
    server_host : str
        FQDN of the staging server (provenance only).
    server_stores_dir : str
        Absolute stores directory on the server (provenance only).
    store_name : str
        Reintegration target for push — the zarr store this session
        originated from.
    raw_name : str
        Filename of the raw volume in the session directory.
    raw_checksum : str
        ``sha256:<hex>`` of the raw volume file.
    shape, spacing_mm, origin_lps, space_directions
        Spatial metadata (per :func:`voxhub_core.staging.stage`).
    annotations : list[PullAnnotationEntry]
        Exported reference annotations.
    """

    protocol_version: int
    prepared_at: str
    server_host: str
    server_stores_dir: str
    store_name: str
    raw_name: str
    raw_checksum: str
    shape: list[int]
    spacing_mm: list[float]
    origin_lps: list[float]
    space_directions: list[list[float]]
    annotations: list[PullAnnotationEntry] = attrs.Factory(list)

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> Self:
        """Deserialize from a plain dict."""
        annotations_raw = d.get('annotations', []) or []
        return cls(
            protocol_version=int(d['protocol_version']),  # type: ignore[arg-type]
            prepared_at=str(d['prepared_at']),
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
            annotations=[
                PullAnnotationEntry.from_dict(entry)  # type: ignore[arg-type]
                for entry in annotations_raw  # type: ignore[union-attr]
            ],
        )

    def to_json(self) -> str:
        """Serialize to a JSON string."""
        return json.dumps(attrs.asdict(self), indent=2)

    @classmethod
    def from_json(cls, text: str) -> Self:
        """Deserialize from a JSON string."""
        return cls.from_dict(json.loads(text))

    @classmethod
    def read(cls, staging_dir: Path) -> Self:
        """Read a pull manifest from a staging / session directory.

        Parameters
        ----------
        staging_dir : Path
            Directory containing ``.voxhub_pull.json``.

        Returns
        -------
        PullManifest

        Raises
        ------
        FileNotFoundError
            If the manifest file does not exist.
        """
        path = staging_dir / '.voxhub_pull.json'
        if not path.exists():
            msg = f'No pull manifest found at {path}'
            raise FileNotFoundError(msg)
        return cls.from_json(path.read_text())

    def write(self, staging_dir: Path) -> None:
        """Write the pull manifest to a staging / session directory.

        Parameters
        ----------
        staging_dir : Path
            Directory to write ``.voxhub_pull.json`` into.
        """
        path = staging_dir / '.voxhub_pull.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json() + '\n')

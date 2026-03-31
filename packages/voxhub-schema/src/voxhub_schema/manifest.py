"""Remote manifest schema.

The manifest is written to the local WIP directory after a pull and
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
    """Manifest written to the WIP directory after ``pull``.

    Records the server target, protocol version, pull session ID,
    and per-store metadata + expected ontologies.
    """

    server_host: str
    server_zarr_root: str
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
            server_zarr_root=str(d['server_zarr_root']),
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
    def read(cls, wip_dir: Path) -> Self:
        """Read a manifest from a WIP directory.

        Parameters
        ----------
        wip_dir : Path
            The local WIP directory containing ``.voxhub_manifest.json``.

        Returns
        -------
        RemoteManifest

        Raises
        ------
        FileNotFoundError
            If the manifest file does not exist.
        """
        path = wip_dir / '.voxhub_manifest.json'
        if not path.exists():
            msg = f'No manifest found at {path}'
            raise FileNotFoundError(msg)
        return cls.from_json(path.read_text())

    def write(self, wip_dir: Path) -> None:
        """Write the manifest to a WIP directory.

        Parameters
        ----------
        wip_dir : Path
            The local WIP directory to write to.
        """
        path = wip_dir / '.voxhub_manifest.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json() + '\n')

"""Provenance recording for annotation integration.

Writes provenance metadata to zarr array attrs and to the central
``.meta/provenance.jsonl`` index at the stores directory root.
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import zarr

from voxhub_core.server.locks import provenance_lock
from voxhub_schema import IssueRecord


def record_provenance(
    stores_dir: Path,
    store_name: str,
    annotation_path: str,
    *,
    annotator_id: str,
    machine_id: str,
    nano_id: str,
    pull_session_id: str,
    ontology: str,
    ontology_version: int,
    source_nrrd_checksum: str,
    source_file: str,
    identity_source: str | None = None,
    issues: list[IssueRecord] | None = None,
) -> None:
    """Record full provenance for an annotation integration.

    Updates the zarr array attrs and appends to the JSONL index.

    Parameters
    ----------
    stores_dir : Path
        Directory containing zarr stores.
    store_name : str
        Name of the zarr store (without ``.zarr``).
    annotation_path : str
        Path within the zarr store to the annotation array.
    annotator_id : str
        Annotator identifier.
    machine_id : str
        Machine identifier hash.
    nano_id : str
        8-char nano-ID.
    pull_session_id : str
        Session ID from the pull that created the staging directory.
    ontology : str
        Ontology name.
    ontology_version : int
        Ontology version.
    source_nrrd_checksum : str
        SHA-256 checksum of the source NRRD file.
    source_file : str
        Original annotation filename.
    identity_source : str | None
        How the ``annotator_id`` was determined: ``'ssh_key'`` (bound to
        the connecting SSH key via ``VOXHUB_ANNOTATOR``), ``'flag'``
        (client-supplied ``--annotator-id``), or ``None``.  Recorded as an
        additive field in both the zarr attrs and the JSONL line only when
        supplied; readers must tolerate its absence on older records.
    issues : list[IssueRecord] | None
        Validation issues (warnings that were accepted).
    """
    timestamp = datetime.now(UTC).isoformat()
    zarr_path = stores_dir / f'{store_name}.zarr'

    # Update zarr array attributes.
    root = zarr.open_group(zarr_path, mode='r+')
    parts = annotation_path.strip('/').split('/')
    node: Any = root
    for part in parts:
        node = node[part]

    provenance_attrs = {
        'integrated_at': timestamp,
        'annotator_id': annotator_id,
        'machine_id': machine_id,
        'nano_id': nano_id,
        'pull_session_id': pull_session_id,
        'source_nrrd_checksum': source_nrrd_checksum,
        'source_file': source_file,
        'ontology': ontology,
        'ontology_version': ontology_version,
    }
    # Additive field: only stamp when known so pre-B records stay unchanged.
    if identity_source is not None:
        provenance_attrs['identity_source'] = identity_source
    node.update_attributes(provenance_attrs)

    # Append to provenance JSONL index.
    meta_dir = stores_dir / '.meta'
    meta_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = meta_dir / 'provenance.jsonl'

    session_id = f'dt-push-{datetime.now(UTC).strftime("%Y%m%dT%H%M%S")}'
    record: dict[str, Any] = {
        'event': 'push',
        'session_id': session_id,
        'pull_session_id': pull_session_id,
        'store': store_name,
        'annotation_path': annotation_path,
        'annotator_id': annotator_id,
        'machine_id': machine_id,
        'timestamp': timestamp,
        'ontology': ontology,
        'ontology_version': ontology_version,
        'issues': [
            {'severity': i.severity, 'message': i.message} for i in (issues or [])
        ],
    }
    # Additive field: only present when known so older readers using .get and
    # pre-B lines round-trip unchanged.
    if identity_source is not None:
        record['identity_source'] = identity_source

    # The central JSONL is appended to across *all* stores, so the per-store
    # store_lock does not serialize these writes.  O_APPEND atomicity only
    # covers writes under PIPE_BUF (and is not guaranteed on some network
    # filesystems), so a large record could tear when concurrent appends
    # interleave.  Serialize the append with a dedicated filelock; keep
    # O_APPEND + fsync for durability.
    with provenance_lock(stores_dir), open(jsonl_path, 'a') as f:
        f.write(json.dumps(record) + '\n')
        f.flush()
        os.fsync(f.fileno())


def validate_provenance_jsonl(path: Path) -> list[str]:
    """Validate a provenance JSONL file.

    Each line must be valid JSON. Missing files are not considered
    errors (the file is created on first push).

    Parameters
    ----------
    path : Path
        Path to a ``provenance.jsonl`` file.

    Returns
    -------
    list[str]
        Error descriptions. Empty list means the file is valid.
    """
    if not path.is_file():
        return []

    errors: list[str] = []
    with open(path) as f:
        for line_no, line in enumerate(f, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                json.loads(stripped)
            except json.JSONDecodeError as exc:
                errors.append(f'line {line_no}: {exc}')
    return errors

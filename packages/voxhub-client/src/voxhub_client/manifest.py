"""Local manifest operations for the client.

Thin wrappers around ``voxhub_schema.RemoteManifest`` for reading,
writing, and updating manifests in WIP directories.
"""

from __future__ import annotations

from pathlib import Path

from voxhub_schema import ManifestStatus, RemoteManifest


def read_manifest(wip_dir: str | Path) -> RemoteManifest:
    """Read the manifest from a WIP directory.

    Parameters
    ----------
    wip_dir : str | Path
        The local WIP directory.

    Returns
    -------
    RemoteManifest

    Raises
    ------
    FileNotFoundError
        If the manifest file does not exist.
    """
    return RemoteManifest.read(Path(wip_dir))


def write_manifest(wip_dir: str | Path, manifest: RemoteManifest) -> None:
    """Write the manifest to a WIP directory.

    Parameters
    ----------
    wip_dir : str | Path
        The local WIP directory.
    manifest : RemoteManifest
        The manifest to write.
    """
    manifest.write(Path(wip_dir))


def update_manifest_status(
    wip_dir: str | Path,
    store_name: str,
    status: ManifestStatus,
) -> None:
    """Update the status of a store in the manifest.

    Parameters
    ----------
    wip_dir : str | Path
        The local WIP directory.
    store_name : str
        Name of the store to update.
    status : ManifestStatus
        New status value.

    Raises
    ------
    KeyError
        If the store is not in the manifest.
    """
    wip_dir = Path(wip_dir)
    manifest = RemoteManifest.read(wip_dir)

    if store_name not in manifest.stores:
        msg = f"Store '{store_name}' not found in manifest"
        raise KeyError(msg)

    manifest.stores[store_name].status = status
    manifest.write(wip_dir)

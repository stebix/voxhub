"""Local manifest operations for the client.

Thin wrappers around ``voxhub_schema.RemoteManifest`` for reading,
writing, and updating manifests in staging directories.
"""

from pathlib import Path

from voxhub_schema import ManifestStatus, RemoteManifest


def read_manifest(staging_dir: str | Path) -> RemoteManifest:
    """Read the manifest from a staging directory.

    Parameters
    ----------
    staging_dir : str | Path
        The local staging directory.

    Returns
    -------
    RemoteManifest

    Raises
    ------
    FileNotFoundError
        If the manifest file does not exist.
    """
    return RemoteManifest.read(Path(staging_dir))


def write_manifest(staging_dir: str | Path, manifest: RemoteManifest) -> None:
    """Write the manifest to a staging directory.

    Parameters
    ----------
    staging_dir : str | Path
        The local staging directory.
    manifest : RemoteManifest
        The manifest to write.
    """
    manifest.write(Path(staging_dir))


def update_manifest_status(
    staging_dir: str | Path,
    store_name: str,
    status: ManifestStatus,
) -> None:
    """Update the status of a store in the manifest.

    Parameters
    ----------
    staging_dir : str | Path
        The local staging directory.
    store_name : str
        Name of the store to update.
    status : ManifestStatus
        New status value.

    Raises
    ------
    KeyError
        If the store is not in the manifest.
    """
    staging_dir = Path(staging_dir)
    manifest = RemoteManifest.read(staging_dir)

    if store_name not in manifest.stores:
        msg = f"Store '{store_name}' not found in manifest"
        raise KeyError(msg)

    manifest.stores[store_name].status = status
    manifest.write(staging_dir)

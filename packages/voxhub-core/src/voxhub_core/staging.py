"""Assemble a staging directory by extracting zarr stores to NRRD files.

The staging directory is a Slicer-ready on-disk view of one or more
zarr stores.  Per-store zarr → NRRD transformation lives in
:mod:`voxhub_core.extraction`; this module handles the directory
choreography: discovery, layout, force/exists checks, metadata
collection.
"""

from pathlib import Path
from typing import Any

from rich.console import Console

from voxhub_core.catalog import discover_zarr_stores
from voxhub_core.extraction import extract_volume


def stage(
    stores_dir: str | Path,
    staging_dir: str | Path,
    *,
    store_names: list[str] | None = None,
    compress: bool = False,
    force: bool = False,
    console: Console | None = None,
) -> dict[str, dict[str, Any]]:
    """Stage zarr volumes as NRRD files into a staging directory.

    Parameters
    ----------
    stores_dir : str | Path
        Directory containing ``.zarr`` stores.
    staging_dir : str | Path
        Target staging directory to create.
    store_names : list[str] | None
        Specific store names to stage.  ``None`` stages all.
    compress : bool
        Apply gzip compression to NRRD files.
    force : bool
        Overwrite existing staging directory contents.
    console : Console | None
        Optional rich Console for output.

    Returns
    -------
    dict[str, dict[str, Any]]
        Per-store metadata (checksums, shape, spatial info).

    Raises
    ------
    FileExistsError
        If *staging_dir* exists and *force* is False.
    FileNotFoundError
        If *stores_dir* does not exist or no stores found.
    """
    stores_dir = Path(stores_dir)
    staging_dir = Path(staging_dir)
    console = console or Console()

    if not stores_dir.is_dir():
        msg = f'Stores directory not found: {stores_dir}'
        raise FileNotFoundError(msg)

    if staging_dir.exists() and not force:
        msg = (
            f'Staging directory already exists: {staging_dir}. '
            f'Pass force=True to overwrite.'
        )
        raise FileExistsError(msg)

    entries = discover_zarr_stores(stores_dir)
    if not entries:
        msg = f'No .zarr stores found under {stores_dir}'
        raise FileNotFoundError(msg)

    if store_names is not None:
        name_set = set(store_names)
        entries = [e for e in entries if e.path.name.removesuffix('.zarr') in name_set]
        if not entries:
            msg = f'No matching stores found for: {store_names}'
            raise FileNotFoundError(msg)

    staging_dir.mkdir(parents=True, exist_ok=True)

    store_metadata: dict[str, dict[str, Any]] = {}

    for entry in entries:
        store_name = entry.path.name.removesuffix('.zarr')
        console.print(f'  Staging [green]{store_name}[/green] ...')

        if entry.error:
            console.print(f'  [red]Skipping {store_name}: {entry.error}[/red]')
            continue

        nrrd_path = staging_dir / store_name / 'raw.nrrd'
        meta = extract_volume(entry.path, nrrd_path, compress=compress)
        meta['zarr_path'] = str(entry.path.resolve())

        shape = meta['shape']
        spacing_mm = meta['spacing_mm']
        console.print(
            f'    shape={tuple(shape)}  '
            f'spacing=[{spacing_mm[0]:.3f}, '
            f'{spacing_mm[1]:.3f}, '
            f'{spacing_mm[2]:.3f}] mm'
        )

        store_metadata[store_name] = meta

    console.print(
        f'\n[bold]{len(store_metadata)}[/bold] store(s) staged to {staging_dir}'
    )
    return store_metadata

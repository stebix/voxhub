"""Zarr store discovery and catalog display.

Discovers ``.zarr`` stores under a directory tree, probes their
metadata (including annotation information), and renders structured
overviews using ``rich``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import attrs
import numpy as np
import zarr
from rich.console import Console
from rich.table import Table
from rich.tree import Tree


@attrs.define
class AnnotationEntry:
    """Metadata for an annotation discovered in a zarr store."""

    path: str
    ontology: str
    ontology_version: int
    annotator_id: str
    integrated_at: str


@attrs.define
class ZarrEntry:
    """Metadata extracted from a single discovered zarr store."""

    path: Path
    shape: tuple[int, ...] | None = None
    dtype: str | None = None
    source_directory: str | None = None
    series_directory: str | None = None
    attributes: dict[str, Any] = attrs.Factory(dict)
    annotations: list[AnnotationEntry] = attrs.Factory(list)
    error: str | None = None


def _discover_annotations(root: zarr.Group) -> list[AnnotationEntry]:
    """Walk ``annotations/`` subgroups to find integrated annotations."""
    results: list[AnnotationEntry] = []
    try:
        ann_group = root['annotations']
    except KeyError:
        return results

    for annotator_name in ann_group:
        annotator_group = ann_group[annotator_name]
        if not isinstance(annotator_group, zarr.Group):
            continue
        for instance_name in annotator_group:
            instance = annotator_group[instance_name]
            if isinstance(instance, zarr.Group):
                # Look for the array inside the instance group.
                for arr_name in instance:
                    child = instance[arr_name]
                    if isinstance(child, zarr.Array):
                        a = dict(child.attrs)
                        results.append(
                            AnnotationEntry(
                                path=(f'annotations/{annotator_name}/{instance_name}'),
                                ontology=a.get('ontology', ''),
                                ontology_version=int(a.get('ontology_version', 0)),
                                annotator_id=a.get('annotator_id', ''),
                                integrated_at=a.get('integrated_at', ''),
                            )
                        )
                        break
            elif isinstance(instance, zarr.Array):
                a = dict(instance.attrs)
                results.append(
                    AnnotationEntry(
                        path=(f'annotations/{annotator_name}/{instance_name}'),
                        ontology=a.get('ontology', ''),
                        ontology_version=int(a.get('ontology_version', 0)),
                        annotator_id=a.get('annotator_id', ''),
                        integrated_at=a.get('integrated_at', ''),
                    )
                )
    return results


def _probe_zarr(path: Path) -> ZarrEntry:
    """Open a zarr store and extract shape, dtype, and metadata."""
    entry = ZarrEntry(path=path)
    try:
        root = zarr.open_group(path, mode='r')
        arr = root['raw']['full']
        entry.shape = arr.shape
        entry.dtype = str(arr.dtype)
        a = dict(arr.attrs)
        entry.attributes = a
        entry.source_directory = a.get('source_directory')
        entry.series_directory = a.get('series_directory')
        entry.annotations = _discover_annotations(root)
    except Exception as exc:
        entry.error = str(exc)
    return entry


def discover_zarr_stores(root: Path) -> list[ZarrEntry]:
    """Recursively find all ``.zarr`` directories under *root*.

    Parameters
    ----------
    root : Path
        Directory to search.

    Returns
    -------
    list[ZarrEntry]
        One entry per discovered store, sorted by path.
    """
    zarr_dirs = sorted(p for p in root.rglob('*.zarr') if p.is_dir())
    return [_probe_zarr(p) for p in zarr_dirs]


def _load_manifest(directory: Path) -> dict[str, str] | None:
    """Try to load a ``manifest.json`` from *directory*."""
    manifest_path = directory / 'manifest.json'
    if manifest_path.is_file():
        return json.loads(manifest_path.read_text())
    return None


def _format_shape(shape: tuple[int, ...] | None) -> str:
    if shape is None:
        return '?'
    return ' x '.join(str(s) for s in shape)


def _format_size_bytes(shape: tuple[int, ...] | None, dtype: str | None) -> str:
    if shape is None or dtype is None:
        return '?'
    try:
        nbytes = float(int(np.prod(shape)) * np.dtype(dtype).itemsize)
    except TypeError:
        return '?'
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if nbytes < 1024:
            return f'{nbytes:.1f} {unit}'
        nbytes /= 1024
    return f'{nbytes:.1f} PiB'


def build_tree(
    root: Path,
    entries: list[ZarrEntry],
    manifest: dict[str, str] | None = None,
) -> Tree:
    """Build a ``rich.Tree`` showing the directory layout and metadata.

    Parameters
    ----------
    root : Path
        Scanned root directory.
    entries : list[ZarrEntry]
        Discovered zarr entries.
    manifest : dict[str, str] | None
        Optional manifest mapping ``{source_id: generated_name}``.

    Returns
    -------
    Tree
    """
    name_to_source: dict[str, str] = {}
    if manifest:
        name_to_source = {v: k for k, v in manifest.items()}

    tree = Tree(f'[bold]{root}[/bold]')
    subtrees: dict[str, Tree] = {}

    for entry in entries:
        rel = entry.path.relative_to(root)
        parts = rel.parts

        parent = tree
        for i, part in enumerate(parts[:-1]):
            node_key = '/'.join(parts[: i + 1])
            if node_key not in subtrees:
                subtrees[node_key] = parent.add(f'[blue]{part}/[/blue]')
            parent = subtrees[node_key]

        zarr_name = parts[-1]
        stem = zarr_name.removesuffix('.zarr')

        if entry.error:
            label = f'[red]{zarr_name}[/red]  [dim]error: {entry.error}[/dim]'
        else:
            shape_str = _format_shape(entry.shape)
            size_str = _format_size_bytes(entry.shape, entry.dtype)
            label = (
                f'[green]{zarr_name}[/green]  '
                f'[dim]{shape_str}[/dim]  '
                f'[dim]{entry.dtype}[/dim]  '
                f'[dim]{size_str}[/dim]'
            )
            source = name_to_source.get(stem) or entry.source_directory
            if source:
                label += f'  [yellow]<- {source}[/yellow]'
            if entry.annotations:
                ann_str = ', '.join(
                    f'{a.ontology} ({a.annotator_id})' for a in entry.annotations
                )
                label += f'  [cyan]annotations: {ann_str}[/cyan]'

        parent.add(label)

    return tree


def build_summary_table(entries: list[ZarrEntry]) -> Table:
    """Build a ``rich.Table`` summarizing all discovered zarr stores.

    Parameters
    ----------
    entries : list[ZarrEntry]
        Discovered zarr entries.

    Returns
    -------
    Table
    """
    table = Table(title='Zarr Store Summary')
    table.add_column('Name', style='green')
    table.add_column('Shape', style='dim')
    table.add_column('Dtype', style='dim')
    table.add_column('Size', style='dim')
    table.add_column('Source', style='yellow')
    table.add_column('Annotations', style='cyan')
    table.add_column('Status')

    for entry in entries:
        name = entry.path.name
        if entry.error:
            table.add_row(
                name,
                '',
                '',
                '',
                '',
                '',
                f'[red]{entry.error}[/red]',
            )
        else:
            ann_count = str(len(entry.annotations))
            table.add_row(
                name,
                _format_shape(entry.shape),
                entry.dtype or '?',
                _format_size_bytes(entry.shape, entry.dtype),
                entry.source_directory or '',
                ann_count,
                '[green]ok[/green]',
            )
    return table


def catalog(
    root: str | Path,
    *,
    show_table: bool = False,
    console: Console | None = None,
) -> list[ZarrEntry]:
    """Discover and display all zarr stores under *root*.

    Parameters
    ----------
    root : str | Path
        Directory to search recursively.
    show_table : bool
        Also print a summary table.
    console : Console | None
        Optional rich Console.

    Returns
    -------
    list[ZarrEntry]
    """
    root = Path(root)
    console = console or Console()

    if not root.is_dir():
        console.print(f'[red]Not a directory: {root}[/red]')
        return []

    entries = discover_zarr_stores(root)

    if not entries:
        console.print(f'[yellow]No .zarr stores found under {root}[/yellow]')
        return entries

    manifest = _load_manifest(root)
    tree = build_tree(root, entries, manifest)
    console.print(tree)
    console.print(f'\n[bold]{len(entries)}[/bold] zarr store(s) found.')

    if show_table:
        console.print()
        console.print(build_summary_table(entries))

    return entries

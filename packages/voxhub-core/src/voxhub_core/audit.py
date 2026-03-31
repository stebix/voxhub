"""Cross-store ontology-based coherence audit.

Checks that annotations across zarr stores are consistent with their
declared ontologies: same labels, same values, same landmark names.
"""

from pathlib import Path
from typing import Any

import attrs
import zarr
from rich.console import Console
from rich.table import Table

from voxhub_core.catalog import discover_zarr_stores


@attrs.define
class StoreAnnotationInfo:
    """Annotation metadata extracted from a single zarr store."""

    store_name: str
    store_path: Path
    segment_map: dict[int, str] = attrs.Factory(dict)
    landmark_labels: list[str] = attrs.Factory(list)
    has_segmentation: bool = False
    has_landmarks: bool = False
    ontology: str | None = None
    ontology_version: int | None = None
    error: str | None = None


@attrs.define
class CoherenceIssue:
    """A single cross-store coherence finding."""

    severity: str
    category: str
    store_name: str
    message: str


def _navigate_zarr_path(root: zarr.Group, path: str) -> Any | None:
    """Walk a slash-separated path in a zarr group hierarchy."""
    current: Any = root
    for part in path.strip('/').split('/'):
        try:
            current = current[part]
        except KeyError:
            return None
    return current


def _discover_annotation_arrays(
    root: zarr.Group,
) -> list[tuple[str, dict[str, Any]]]:
    """Find all annotation arrays in a zarr store.

    Returns
    -------
    list[tuple[str, dict]]
        ``(path, attrs_dict)`` for each annotation array found.
    """
    results: list[tuple[str, dict[str, Any]]] = []
    try:
        ann_group = root['annotations']
    except KeyError:
        return results

    def _walk(group: zarr.Group, prefix: str) -> None:
        for name in group:
            path = f'{prefix}/{name}' if prefix else name
            child = group[name]
            if isinstance(child, zarr.Array):
                results.append((path, dict(child.attrs)))
            elif isinstance(child, zarr.Group):
                _walk(child, path)

    _walk(ann_group, 'annotations')
    return results


def _probe_store_annotations(
    store_path: Path,
    ontology_filter: str | None = None,
) -> list[StoreAnnotationInfo]:
    """Read annotation metadata from a single zarr store."""
    store_name = store_path.name.removesuffix('.zarr')
    results: list[StoreAnnotationInfo] = []

    try:
        root = zarr.open_group(store_path, mode='r')
    except Exception as exc:
        return [
            StoreAnnotationInfo(
                store_name=store_name,
                store_path=store_path,
                error=f'Cannot open store: {exc}',
            )
        ]

    annotations = _discover_annotation_arrays(root)

    for _path, a in annotations:
        ont = a.get('ontology')
        if ontology_filter and ont != ontology_filter:
            continue

        info = StoreAnnotationInfo(
            store_name=store_name,
            store_path=store_path,
            ontology=ont,
            ontology_version=a.get('ontology_version'),
        )

        segments = a.get('segments')
        if segments:
            info.has_segmentation = True
            for seg in segments:
                lv = seg.get('label_value')
                nm = seg.get('name', '')
                if lv is not None:
                    info.segment_map[int(lv)] = nm

        labels = a.get('labels')
        if labels:
            info.has_landmarks = True
            info.landmark_labels = list(labels)

        results.append(info)

    return results


def _check_segmentation_coherence(
    infos: list[StoreAnnotationInfo],
) -> list[CoherenceIssue]:
    """Check segmentation label consistency across stores."""
    issues: list[CoherenceIssue] = []

    seg_infos = [i for i in infos if i.has_segmentation]
    if len(seg_infos) < 2:
        return issues

    # Use majority segment set as reference.
    from collections import Counter

    name_sets = [frozenset(i.segment_map.values()) for i in seg_infos]
    most_common = Counter(name_sets).most_common(1)[0][0]
    ref_map = {}
    for i in seg_infos:
        if frozenset(i.segment_map.values()) == most_common:
            ref_map = i.segment_map
            break

    for info in seg_infos:
        actual_names = set(info.segment_map.values())
        ref_names = set(ref_map.values())

        missing = ref_names - actual_names
        if missing:
            issues.append(
                CoherenceIssue(
                    severity='error',
                    category='segmentation',
                    store_name=info.store_name,
                    message=(f'Missing segment names: {sorted(missing)}'),
                )
            )

        extra = actual_names - ref_names
        if extra:
            issues.append(
                CoherenceIssue(
                    severity='warning',
                    category='segmentation',
                    store_name=info.store_name,
                    message=(f'Extra segment names: {sorted(extra)}'),
                )
            )

        for value, name in ref_map.items():
            if value in info.segment_map and info.segment_map[value] != name:
                issues.append(
                    CoherenceIssue(
                        severity='error',
                        category='segmentation',
                        store_name=info.store_name,
                        message=(
                            f'Label value {value}: expected '
                            f'{name!r}, got '
                            f'{info.segment_map[value]!r}'
                        ),
                    )
                )

    return issues


def _check_landmark_coherence(
    infos: list[StoreAnnotationInfo],
) -> list[CoherenceIssue]:
    """Check landmark label consistency across stores."""
    issues: list[CoherenceIssue] = []

    lmk_infos = [i for i in infos if i.has_landmarks]
    if len(lmk_infos) < 2:
        return issues

    from collections import Counter

    label_sets = [frozenset(i.landmark_labels) for i in lmk_infos]
    most_common = Counter(label_sets).most_common(1)[0][0]

    for info in lmk_infos:
        actual = set(info.landmark_labels)
        missing = most_common - actual
        if missing:
            issues.append(
                CoherenceIssue(
                    severity='error',
                    category='landmarks',
                    store_name=info.store_name,
                    message=(f'Missing landmark labels: {sorted(missing)}'),
                )
            )
        extra = actual - most_common
        if extra:
            issues.append(
                CoherenceIssue(
                    severity='warning',
                    category='landmarks',
                    store_name=info.store_name,
                    message=(f'Extra landmark labels: {sorted(extra)}'),
                )
            )

    return issues


def audit(
    zarr_root: str | Path,
    *,
    ontology_filter: str | None = None,
    store_names: list[str] | None = None,
    console: Console | None = None,
) -> list[CoherenceIssue]:
    """Audit cross-store annotation coherence.

    Parameters
    ----------
    zarr_root : str | Path
        Directory containing ``.zarr`` stores.
    ontology_filter : str | None
        If set, only check annotations for this ontology.
    store_names : list[str] | None
        Specific store names to audit.
    console : Console | None
        Optional rich Console.

    Returns
    -------
    list[CoherenceIssue]
    """
    zarr_root = Path(zarr_root)
    console = console or Console()

    entries = discover_zarr_stores(zarr_root)

    if store_names is not None:
        name_set = set(store_names)
        entries = [e for e in entries if e.path.name.removesuffix('.zarr') in name_set]

    all_infos: list[StoreAnnotationInfo] = []
    for entry in entries:
        infos = _probe_store_annotations(entry.path, ontology_filter=ontology_filter)
        all_infos.extend(infos)

    for info in all_infos:
        if info.error:
            console.print(f'  [red]{info.store_name}: {info.error}[/red]')

    issues: list[CoherenceIssue] = []
    issues.extend(_check_segmentation_coherence(all_infos))
    issues.extend(_check_landmark_coherence(all_infos))

    # Report.
    errors = [i for i in issues if i.severity == 'error']
    warnings = [i for i in issues if i.severity == 'warning']

    if issues:
        table = Table(title='Coherence Issues')
        table.add_column('Severity')
        table.add_column('Category')
        table.add_column('Store')
        table.add_column('Message')

        for issue in issues:
            style = 'red' if issue.severity == 'error' else 'yellow'
            table.add_row(
                f'[{style}]{issue.severity}[/{style}]',
                issue.category,
                issue.store_name,
                issue.message,
            )

        console.print(table)

    console.print(
        f'\n[bold]Audit complete:[/bold] '
        f'{len(errors)} errors, {len(warnings)} warnings, '
        f'{len(all_infos)} annotations checked '
        f'across {len(entries)} stores'
    )

    return issues

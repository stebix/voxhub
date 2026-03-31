"""User-facing CLI for voxhub remote workflows.

Commands: pull, push, remote-catalog, whoami, set-identity.
"""

import argparse
import hashlib
import sys
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.tree import Tree

from voxhub_client.identity import get_identity, set_identity
from voxhub_client.manifest import (
    read_manifest,
    update_manifest_status,
    write_manifest,
)
from voxhub_client.ssh import SshRunner, SshTarget
from voxhub_client.transfer import RsyncTransfer
from voxhub_schema import (
    PROTOCOL_VERSION,
    RemoteManifest,
    RemoteManifestEntry,
    load_ontology,
    validate_lmk_preflight,
    validate_seg_preflight,
)

console = Console()


def _compute_sha256(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return f'sha256:{h.hexdigest()}'


# -- pull --------------------------------------------------------------------


def _run_pull(args: argparse.Namespace) -> None:
    target = SshTarget.parse(args.target)
    local_wip = Path(args.local_wip)
    runner = SshRunner(target=target)
    transfer = RsyncTransfer(target=target)

    console.print(
        f'[bold]Pulling from {target.ssh_destination}:{target.zarr_root}[/bold]'
    )

    # 1. List stores.
    console.print('  Listing stores...')
    list_args = ['list-stores', target.zarr_root]
    runner.run(*list_args)

    # 2. Prepare pull.
    console.print('  Preparing pull...')
    prepare_args = ['prepare-pull', target.zarr_root]
    if args.stores:
        prepare_args.extend(['--stores', *args.stores])
    if args.ontologies:
        prepare_args.extend(['--ontologies', *args.ontologies])
    if args.include_existing_annotations:
        prepare_args.extend(
            [
                '--include-existing-annotations',
                *args.include_existing_annotations,
            ]
        )
    if args.compress:
        prepare_args.append('--compress')

    prepare_response = runner.run(*prepare_args, timeout=600)
    wip_dir = prepare_response['wip_dir']

    # 3. Rsync pull.
    console.print('  Transferring files...')
    local_wip.mkdir(parents=True, exist_ok=True)
    transfer.pull(wip_dir, str(local_wip))

    # 4. Cleanup server temp dir.
    console.print('  Cleaning up server...')
    runner.run('cleanup', wip_dir)

    # 5. Write manifest.
    stores_data = prepare_response.get('stores', {})
    manifest_stores: dict[str, RemoteManifestEntry] = {}
    for name, info in stores_data.items():
        manifest_stores[name] = RemoteManifestEntry(
            status='pulled',
            raw_checksum=info['raw_checksum'],
            shape=info['shape'],
            spacing_mm=info['spacing_mm'],
            origin_lps=info['origin_lps'],
            space_directions=info['space_directions'],
            expected_ontologies=info.get('expected_ontologies', []),
            included_annotations=info.get('included_annotations', []),
        )

    manifest = RemoteManifest(
        server_host=target.ssh_destination,
        server_zarr_root=target.zarr_root,
        protocol_version=PROTOCOL_VERSION,
        pull_session_id=Path(wip_dir).name,
        pulled_at=datetime.now(UTC).isoformat(),
        stores=manifest_stores,
    )
    write_manifest(local_wip, manifest)

    console.print(
        f'\n[bold green]Pulled {len(manifest_stores)} store(s) '
        f'to {local_wip}[/bold green]'
    )


# -- push --------------------------------------------------------------------


def _run_push(args: argparse.Namespace) -> None:
    local_wip = Path(args.local_wip)

    # 1. Get identity.
    try:
        identity = get_identity()
    except FileNotFoundError as exc:
        console.print(f'[red]{exc}[/red]')
        sys.exit(1)

    console.print(f'[bold]Pushing as {identity.annotator_id} ({identity.nano_id})[/bold]')

    # 2. Read manifest.
    try:
        manifest = read_manifest(local_wip)
    except FileNotFoundError as exc:
        console.print(f'[red]{exc}[/red]')
        sys.exit(1)

    target = SshTarget.parse(f'{manifest.server_host}:{manifest.server_zarr_root}')
    runner = SshRunner(target=target)
    transfer = RsyncTransfer(target=target)

    # 3. Discover annotation files.
    annotation_files: dict[str, list[Path]] = {}
    for store_name in manifest.stores:
        store_dir = local_wip / store_name
        if not store_dir.is_dir():
            continue
        files: list[Path] = []
        files.extend(store_dir.glob('*.seg.nrrd'))
        files.extend(store_dir.glob('*.mrk.json'))
        if files:
            annotation_files[store_name] = files

    if not annotation_files:
        console.print('[yellow]No annotation files found to push.[/yellow]')
        return

    # 4. Pre-flight validation.
    console.print('  Running pre-flight validation...')
    has_errors = False
    for store_name, files in annotation_files.items():
        entry = manifest.stores[store_name]
        for ont_name in entry.expected_ontologies:
            try:
                ontology = load_ontology(ont_name)
            except FileNotFoundError:
                console.print(
                    f'  [yellow]Warning: ontology {ont_name!r} not found[/yellow]'
                )
                continue

            for f in files:
                if f.name.endswith('.seg.nrrd'):
                    issues = validate_seg_preflight(f, entry, ontology)
                elif f.name.endswith('.mrk.json'):
                    issues = validate_lmk_preflight(f, entry, ontology)
                else:
                    continue

                for issue in issues:
                    style = 'red' if issue.severity == 'error' else 'yellow'
                    console.print(
                        f'    [{style}]{store_name}/{f.name}: '
                        f'{issue.severity}: '
                        f'{issue.message}[/{style}]'
                    )
                    if issue.severity == 'error':
                        has_errors = True

    if has_errors and not args.force:
        console.print('[red]Validation errors found. Fix issues or use --force.[/red]')
        if args.validate_only:
            sys.exit(1)
        sys.exit(1)

    if args.validate_only:
        console.print('[green]Validation passed.[/green]')
        return

    # 5. Compute checksums.
    checksums: list[str] = []
    for files in annotation_files.values():
        for f in files:
            checksum = _compute_sha256(f)
            checksums.append(f'{f.name}:{checksum}')

    # 6. Create server temp dir.
    console.print('  Creating server temp dir...')
    server_wip = runner.mktemp()

    # 7. Push files.
    console.print('  Transferring files...')
    transfer.push(str(local_wip), server_wip)

    # 8. Integrate.
    console.print('  Integrating annotations...')
    integrate_args = [
        'integrate-annotations',
        target.zarr_root,
        server_wip,
        '--annotator-id',
        identity.annotator_id,
        '--machine-id',
        identity.machine_id,
        '--nano-id',
        identity.nano_id,
    ]
    if checksums:
        integrate_args.extend(['--checksums', *checksums])
    if args.force:
        integrate_args.append('--force')

    response = runner.run(*integrate_args, timeout=600)

    # 9. Cleanup.
    console.print('  Cleaning up server...')
    runner.run('cleanup', server_wip)

    # 10. Update manifest.
    for store_name in response.get('stores', {}):
        store_result = response['stores'][store_name]
        if store_result.get('status') == 'integrated':
            update_manifest_status(local_wip, store_name, 'integrated')

    console.print('\n[bold green]Push complete.[/bold green]')


# -- remote-catalog ----------------------------------------------------------


def _run_remote_catalog(args: argparse.Namespace) -> None:
    target = SshTarget.parse(args.target)
    runner = SshRunner(target=target)

    console.print(
        f'[bold]Catalog for {target.ssh_destination}:{target.zarr_root}[/bold]\n'
    )

    response = runner.run('list-stores', target.zarr_root)
    stores = response.get('stores', [])

    if not stores:
        console.print('[yellow]No stores found.[/yellow]')
        return

    # Optional ontology filter.
    if args.ontology:
        ont_filter = set(args.ontology)
        stores = [
            s
            for s in stores
            if any(a.get('ontology') in ont_filter for a in s.get('annotations', []))
            or not s.get('annotations')
        ]

    tree = Tree(f'[bold]{target.ssh_destination}:{target.zarr_root}[/bold]')
    for store in stores:
        name = store['name']
        shape = store.get('shape', [])
        shape_str = ' x '.join(str(s) for s in shape) if shape else '?'
        label = f'[green]{name}.zarr[/green]  [dim]{shape_str}[/dim]'

        if store.get('error'):
            label += f'  [red]{store["error"]}[/red]'

        node = tree.add(label)

        for ann in store.get('annotations', []):
            ann_label = (
                f'[cyan]{ann["ontology"]}[/cyan] v{ann["ontology_version"]} '
                f'by [yellow]{ann["annotator_id"]}[/yellow] '
                f'at {ann["integrated_at"]}'
            )
            node.add(ann_label)

    console.print(tree)
    console.print(f'\n[bold]{len(stores)}[/bold] store(s)')


# -- whoami ------------------------------------------------------------------


def _run_whoami(args: argparse.Namespace) -> None:
    try:
        identity = get_identity()
    except FileNotFoundError as exc:
        console.print(f'[red]{exc}[/red]')
        sys.exit(1)

    table = Table(title='voxhub Identity')
    table.add_column('Field')
    table.add_column('Value')
    table.add_row('Annotator ID', identity.annotator_id)
    table.add_row('Nano ID', identity.nano_id)
    table.add_row('Machine ID', identity.machine_id)
    console.print(table)


# -- set-identity ------------------------------------------------------------


def _run_set_identity(args: argparse.Namespace) -> None:
    identity = set_identity(args.name)
    console.print(
        f'[green]Identity set:[/green] {identity.annotator_id} ({identity.nano_id})'
    )
    console.print(f'Machine ID: {identity.machine_id}')


# -- Parser ------------------------------------------------------------------


def main() -> None:
    """Entry point for the ``voxhub`` client CLI."""
    parser = argparse.ArgumentParser(
        prog='voxhub',
        description='voxhub: remote annotation workflows',
    )
    subparsers = parser.add_subparsers(dest='command')

    # pull
    pull_p = subparsers.add_parser('pull', help='Pull data from remote for annotation.')
    pull_p.add_argument(
        'target',
        help='SSH target: user@host:/zarr_root',
    )
    pull_p.add_argument(
        'local_wip',
        type=Path,
        help='Local WIP directory.',
    )
    pull_p.add_argument(
        '--stores',
        nargs='*',
        help='Specific store names to pull.',
    )
    pull_p.add_argument(
        '--ontologies',
        nargs='*',
        help='Expected ontologies for annotation.',
    )
    pull_p.add_argument(
        '--include-existing-annotations',
        nargs='*',
        help='Annotation paths to include.',
    )
    pull_p.add_argument(
        '--compress',
        action='store_true',
        help='Request gzip-compressed NRRDs.',
    )
    pull_p.set_defaults(func=_run_pull)

    # push
    push_p = subparsers.add_parser('push', help='Push annotations back to remote.')
    push_p.add_argument(
        'local_wip',
        type=Path,
        help='Local WIP directory.',
    )
    push_p.add_argument(
        '--validate-only',
        action='store_true',
        help='Only validate, do not push.',
    )
    push_p.add_argument(
        '--force',
        action='store_true',
        help='Push despite validation warnings.',
    )
    push_p.set_defaults(func=_run_push)

    # remote-catalog
    rc_p = subparsers.add_parser('remote-catalog', help='Display remote zarr stores.')
    rc_p.add_argument(
        'target',
        help='SSH target: user@host:/zarr_root',
    )
    rc_p.add_argument(
        '--ontology',
        nargs='*',
        help='Filter by ontology name.',
    )
    rc_p.set_defaults(func=_run_remote_catalog)

    # whoami
    wi_p = subparsers.add_parser('whoami', help='Show current annotator identity.')
    wi_p.set_defaults(func=_run_whoami)

    # set-identity
    si_p = subparsers.add_parser('set-identity', help='Set annotator identity.')
    si_p.add_argument('name', help='Annotator name (e.g. alice).')
    si_p.set_defaults(func=_run_set_identity)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    args.func(args)

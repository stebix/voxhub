"""Annotator-facing CLI for voxhub remote annotation workflows.

Commands: set-server, set-identity, whoami, list-stores.
"""

import argparse
import sys

from rich.console import Console
from rich.table import Table

from voxhub_client.catalog_cache import (
    ClientCatalogCache,
    list_stores_cached,
    server_key_for,
)
from voxhub_client.identity import get_identity, set_identity
from voxhub_client.server_config import SERVER_INTERACTION_USER, get_server, set_server
from voxhub_client.ssh import SshRunner


def _run_set_server(args: argparse.Namespace) -> None:
    console = Console()
    config = set_server(args.host, port=args.port)
    console.print(f'[green]Server configured:[/green] {config.connection_string}')


def _run_set_identity(args: argparse.Namespace) -> None:
    console = Console()
    identity = set_identity(args.name)
    console.print(f'[green]Identity configured:[/green] {identity.annotator_id}')
    console.print(f'  Nano ID    : {identity.nano_id}')
    console.print(f'  Machine ID : {identity.machine_id}')


def _run_list_stores(args: argparse.Namespace) -> None:
    console = Console()

    try:
        server = get_server()
    except FileNotFoundError as exc:
        console.print(f'[red]{exc}[/red]')
        sys.exit(1)

    target = server.to_ssh_target()
    runner = SshRunner(target=target)
    cache = ClientCatalogCache()
    server_key = server_key_for(target)

    response = list_stores_cached(
        runner,
        cache,
        server_key,
        force=args.no_cache,
    )

    stores = response.get('stores', [])
    catalog_version = response.get('catalog_version')

    table = Table(
        title=f'Stores on {server.connection_string} (catalog v{catalog_version})',
    )
    table.add_column('Name', style='cyan')
    table.add_column('Shape')
    table.add_column('Spacing (mm)')
    table.add_column('Annotations', justify='right')
    table.add_column('Status')

    for store in stores:
        name = str(store.get('name', '?'))
        shape = ' x '.join(str(d) for d in store.get('shape', []))
        spacing = store.get('spacing_mm') or []
        spacing_str = ' x '.join(f'{s:g}' for s in spacing)
        ann_count = str(len(store.get('annotations', [])))
        error = store.get('error')
        status = '[red]error[/red]' if error else '[green]ok[/green]'
        table.add_row(name, shape, spacing_str, ann_count, status)

    console.print(table)


def _run_whoami(args: argparse.Namespace) -> None:
    console = Console()

    try:
        identity = get_identity()
        console.print('[bold]Identity[/bold]')
        console.print(f'  Annotator ID : {identity.annotator_id}')
        console.print(f'  Nano ID      : {identity.nano_id}')
        console.print(f'  Machine ID   : {identity.machine_id}')
    except FileNotFoundError:
        console.print('[bold]Identity[/bold]')
        console.print(
            "  [yellow](not configured — run 'voxhub set-identity <name>')[/yellow]"
        )

    console.print()

    try:
        server = get_server()
        console.print('[bold]Server[/bold]')
        console.print(f'  Host     : {server.host}')
        console.print(f'  SSH user : {SERVER_INTERACTION_USER}')
        console.print(
            f'  Port     : {server.port if server.port is not None else "(default)"}'
        )
    except FileNotFoundError:
        console.print('[bold]Server[/bold]')
        console.print(
            "  [yellow](not configured — run 'voxhub set-server <host>')[/yellow]"
        )


def main() -> None:
    """Entry point for the annotator-facing ``voxhub`` CLI."""
    parser = argparse.ArgumentParser(
        prog='voxhub',
        description='voxhub: collaborative volumetric annotation',
    )
    subparsers = parser.add_subparsers(dest='command')

    # set-server
    ss = subparsers.add_parser('set-server', help='Configure the remote voxhub server.')
    ss.add_argument('host', help='SSH hostname or IP address of the server.')
    ss.add_argument(
        '--port',
        type=int,
        default=None,
        help='SSH port override (default: 22).',
    )
    ss.set_defaults(func=_run_set_server)

    # set-identity
    si = subparsers.add_parser('set-identity', help='Set the annotator identity.')
    si.add_argument('name', help='Human-readable annotator name (e.g. alice).')
    si.set_defaults(func=_run_set_identity)

    # whoami
    wi = subparsers.add_parser(
        'whoami', help='Show current identity and server configuration.'
    )
    wi.set_defaults(func=_run_whoami)

    # list-stores
    ls = subparsers.add_parser(
        'list-stores',
        help='List zarr stores available on the configured remote server.',
    )
    ls.add_argument(
        '--no-cache',
        action='store_true',
        help=(
            'Skip the client-side catalog cache and force the server to '
            'return the full payload. Useful for debugging or after a '
            'suspected cache corruption.'
        ),
    )
    ls.set_defaults(func=_run_list_stores)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    args.func(args)

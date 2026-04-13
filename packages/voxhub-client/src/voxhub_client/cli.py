"""Annotator-facing CLI for voxhub remote annotation workflows.

Commands: set-server, set-identity, whoami.
"""

import argparse
import sys

from rich.console import Console

from voxhub_client.identity import get_identity, set_identity
from voxhub_client.server_config import SERVER_INTERACTION_USER, get_server, set_server


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

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    args.func(args)

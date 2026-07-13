"""Annotator-facing CLI for voxhub remote annotation workflows.

Commands: set-server, set-identity, whoami, list-stores, pull.
"""

import argparse
import hashlib
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import attrs
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from voxhub_client import pull_log
from voxhub_client.catalog_cache import (
    ClientCatalogCache,
    list_stores_cached,
    server_key_for,
)
from voxhub_client.identity import get_identity, set_identity
from voxhub_client.server_config import SERVER_INTERACTION_USER, get_server, set_server
from voxhub_client.ssh import RemoteError, SshRunner
from voxhub_client.transfer import RsyncTransfer
from voxhub_schema import (
    PROTOCOL_VERSION,
    CleanupResponse,
    ManifestError,
    PrepareRequest,
    PrepareResponse,
    PullManifest,
)


class ChecksumError(Exception):
    """Raised when a checksum verification fails after rsync."""


# -- Pull helpers ------------------------------------------------------------


def _compute_sha256(path: Path) -> str:
    """Compute ``sha256:<hex>`` of a file."""
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return f'sha256:{h.hexdigest()}'


def _verify_checksums(session_dir: Path, manifest: PullManifest) -> None:
    """Verify the raw volume and every reference file matches the manifest.

    Parameters
    ----------
    session_dir : Path
        Local session directory — where rsync landed the pull.
    manifest : PullManifest
        Parsed ``.voxhub_pull.json`` from the session dir.

    Raises
    ------
    ChecksumError
        On any missing file or digest mismatch.  Message names the
        offending file.
    """
    raw_path = session_dir / manifest.raw_name
    if not raw_path.is_file():
        raise ChecksumError(f'raw volume missing: {raw_path}')
    actual = _compute_sha256(raw_path)
    if actual != manifest.raw_checksum:
        raise ChecksumError(
            f'{manifest.raw_name}: checksum mismatch '
            f'(expected {manifest.raw_checksum}, got {actual})'
        )

    for entry in manifest.annotations:
        ref_path = session_dir / 'reference' / entry.reference_filename
        if not ref_path.is_file():
            raise ChecksumError(f'reference file missing: {ref_path}')
        actual = _compute_sha256(ref_path)
        if actual != entry.reference_checksum:
            raise ChecksumError(
                f'{entry.reference_filename}: checksum mismatch '
                f'(expected {entry.reference_checksum}, got {actual})'
            )


def _write_trust_sidecar(session_dir: Path) -> str:
    """Hash ``.voxhub_pull.json`` and write the digest to ``.voxhub_pull.sha256``.

    The sidecar is push's tamper-evident anchor for the manifest.  It
    lives inside the session dir so it travels with the data under
    rename, move, or copy — unlike an ``$HOME``-keyed log entry would.

    Returns
    -------
    str
        The ``sha256:<hex>`` digest (for inclusion in the audit log).
    """
    manifest_path = session_dir / '.voxhub_pull.json'
    digest = _compute_sha256(manifest_path)
    sidecar_path = session_dir / '.voxhub_pull.sha256'
    # A prior pull to this dest locks the sidecar read-only (0o444, see
    # ``_lock_session``); ``write_text`` on it would raise ``PermissionError``.
    # Refresh pulls are a normal workflow, so drop any stale sidecar before
    # rewriting — the session root stays writable, so this always succeeds.
    sidecar_path.unlink(missing_ok=True)
    sidecar_path.write_text(digest + '\n')
    return digest


def _lock_session(session_dir: Path, manifest: PullManifest) -> None:
    """Lock server-authoritative files against accidental modification.

    Sets read-only permissions in one pass:

    * ``.voxhub_pull.json``         → 0o444
    * ``.voxhub_pull.sha256``       → 0o444 (trust sidecar)
    * ``<raw_name>``                → 0o444 (server-authoritative)
    * ``reference/``                → 0o555 (listable, not writable)
    * ``reference/*``               → 0o444 (each reference file)

    The session root directory is left writable so the annotator can
    create new files.  ``OSError`` anywhere in the traversal is
    non-fatal — we warn via rich Console but do not fail the pull, as
    locking is defence-in-depth on top of the checksum/sidecar backstop.
    """
    console = Console(stderr=True)
    targets: list[Path] = [
        session_dir / '.voxhub_pull.json',
        session_dir / '.voxhub_pull.sha256',
        session_dir / manifest.raw_name,
    ]
    for p in targets:
        try:
            p.chmod(0o444)
        except OSError as exc:
            console.print(f'[yellow]warning:[/yellow] could not lock {p}: {exc}')

    ref_dir = session_dir / 'reference'
    if ref_dir.is_dir():
        for ref in ref_dir.iterdir():
            if ref.is_file():
                try:
                    ref.chmod(0o444)
                except OSError as exc:
                    console.print(
                        f'[yellow]warning:[/yellow] could not lock {ref}: {exc}'
                    )
        try:
            ref_dir.chmod(0o555)
        except OSError as exc:
            console.print(f'[yellow]warning:[/yellow] could not lock {ref_dir}: {exc}')


# -- Handlers ----------------------------------------------------------------


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


def _run_pull(args: argparse.Namespace) -> None:
    """Pull a single store from the remote server.

    Steps (see docs/plans/voxhub-pull-refactor.md §client):

    1. Load identity.
    2. Load server config, build SSH runner + rsync transfer.
    3. Resolve local destination.
    4. Invoke remote ``prepare-pull`` over SSH.
    5. Rsync staging dir down.  Failure → no cleanup (GC will handle).
    6. Read ``.voxhub_pull.json`` from the landed session and enforce
       its protocol version.
    7. Verify checksums — raw + every reference file.
    8. Write the trust sidecar (``.voxhub_pull.sha256``).  This is the
       one non-I/O-ignorable failure after rsync; without the anchor,
       push will refuse anyway.
    9. Lock manifest / sidecar / raw / reference files.  Non-fatal.
    10. Signal delivery ACK via ``cleanup``.  Non-fatal (GC backstop).
    11. Append audit entry.  Non-fatal.
    12. Render summary with warning panel for any skipped annotations.
    """
    console = Console()
    err_console = Console(stderr=True)

    # -- 1. identity --------------------------------------------------------
    try:
        identity = get_identity()
    except FileNotFoundError as exc:
        err_console.print(f'[red]{exc}[/red]')
        sys.exit(1)

    # -- 2. server + transport ---------------------------------------------
    try:
        server = get_server()
    except FileNotFoundError as exc:
        err_console.print(f'[red]{exc}[/red]')
        sys.exit(1)

    target = server.to_ssh_target()
    runner = SshRunner(target=target)
    transfer = RsyncTransfer(target=target)

    # -- 3. destination ----------------------------------------------------
    dest = Path(args.dest) if args.dest else Path.cwd()
    dest_str = str(dest)

    # -- 4. remote prepare-pull --------------------------------------------
    request = PrepareRequest(
        store_name=args.store,
        include_existing_annotations=(
            list(args.include_existing_annotations)
            if args.include_existing_annotations
            else None
        ),
        compress=bool(args.compress),
    )

    try:
        response = runner.run('prepare-pull', attrs.asdict(request))
    except RemoteError as exc:
        err_console.print(f'[red]prepare-pull failed:[/red] {exc}')
        sys.exit(1)

    try:
        prepare = PrepareResponse.from_dict(response)
    except (KeyError, TypeError, ValueError) as exc:
        err_console.print(
            f'[red]malformed prepare-pull response:[/red] {exc!r}; '
            f'server and client schema versions have likely drifted.'
        )
        sys.exit(1)

    staging_dir_remote = prepare.staging_dir
    skipped_annotations = prepare.skipped_annotations

    # -- 5. rsync ----------------------------------------------------------
    # The rsync transport is confined by rrsync to the operator's staging
    # root (forced-command wrapper contract), which resolves every
    # requested path relative to that root — an absolute path would be
    # re-rooted underneath it and fail.  Staging dirs are minted as direct
    # children of the root (server contract), so the rsync-visible name of
    # the session is exactly the staging dir's basename.  The absolute
    # ``staging_dir_remote`` stays in play for the RPC surface, where
    # ``cleanup`` (and later ``integrate-annotations``) echo it verbatim.
    staging_rsync_name = PurePosixPath(staging_dir_remote).name
    try:
        transfer.pull(staging_rsync_name, dest_str)
    except subprocess.CalledProcessError as exc:
        err_console.print(
            f'[red]rsync failed[/red] (exit {exc.returncode}); '
            f'server staging dir left in place for diagnosis, GC will reap.'
        )
        sys.exit(1)

    # -- 6. read manifest --------------------------------------------------
    try:
        manifest = PullManifest.read(dest)
    except FileNotFoundError as exc:
        err_console.print(
            f'[red]missing pull manifest after rsync:[/red] {exc}; '
            f'server staging dir left in place.'
        )
        sys.exit(1)
    except ManifestError as exc:
        err_console.print(
            f'[red]pull manifest unreadable or malformed:[/red] {exc}; '
            f'server staging dir left in place for diagnosis '
            f'(rsync corruption or schema drift).'
        )
        sys.exit(1)

    if manifest.protocol_version != PROTOCOL_VERSION:
        err_console.print(
            f'[red]pull manifest has protocol version '
            f'{manifest.protocol_version}, this client expects '
            f'{PROTOCOL_VERSION}[/red] — update voxhub on both sides and '
            f're-pull with a current client.'
        )
        sys.exit(1)

    # -- 7. checksum verification -----------------------------------------
    try:
        _verify_checksums(dest, manifest)
    except ChecksumError as exc:
        err_console.print(
            f'[red]checksum verification failed:[/red] {exc}; '
            f'server staging dir left in place for diagnosis.'
        )
        sys.exit(1)

    # -- 8. trust sidecar (fatal on failure) ------------------------------
    try:
        manifest_digest = _write_trust_sidecar(dest)
    except OSError as exc:
        err_console.print(
            f'[red]could not write trust sidecar:[/red] {exc}; '
            f'session is not fully valid, server staging dir left in place.'
        )
        sys.exit(1)

    # -- 9. lock session (non-fatal) --------------------------------------
    try:
        _lock_session(dest, manifest)
    except OSError as exc:
        err_console.print(f'[yellow]warning: locking incomplete: {exc}[/yellow]')

    # -- 10. delivery ACK via cleanup (non-fatal) --------------------------
    try:
        CleanupResponse.from_dict(
            runner.run('cleanup', {'staging_dir': staging_dir_remote})
        )
    except RemoteError as exc:
        err_console.print(
            f'[yellow]warning: cleanup ACK failed ({exc}); '
            f'GC will reap the staging dir.[/yellow]'
        )
    except (KeyError, TypeError, ValueError) as exc:
        err_console.print(
            f'[yellow]warning: malformed cleanup response ({exc!r}); '
            f'GC will reap the staging dir if the ACK was lost.[/yellow]'
        )

    # -- 11. audit log (non-fatal) ----------------------------------------
    pull_log.append_entry(
        {
            'pulled_at': datetime.now(UTC).isoformat(),
            'annotator_id': identity.annotator_id,
            'machine_id': identity.machine_id,
            'server_host': server.connection_string,
            'server_stores_dir': manifest.server_stores_dir,
            'dest': dest_str,
            'store': manifest.store_name,
            'compress': bool(args.compress),
            'protocol_version': PROTOCOL_VERSION,
        }
    )

    # -- 12. summary -------------------------------------------------------
    _render_pull_summary(
        console,
        dest=dest,
        manifest=manifest,
        manifest_digest=manifest_digest,
        skipped_annotations=skipped_annotations,
    )


def _render_pull_summary(
    console: Console,
    *,
    dest: Path,
    manifest: PullManifest,
    manifest_digest: str,
    skipped_annotations: list[dict[str, str]],
) -> None:
    """Render a rich summary of a successful pull."""
    table = Table(title=f'Pulled {manifest.store_name!r}', show_header=False)
    table.add_column(style='bold')
    table.add_column()
    table.add_row('Destination', str(dest))
    table.add_row('Raw volume', f'{manifest.raw_name} ({manifest.raw_checksum})')
    table.add_row('Shape', ' x'.join(str(s) for s in manifest.shape))
    table.add_row(
        'Spacing (mm)',
        ' x'.join(f'{s:g}' for s in manifest.spacing_mm),
    )
    table.add_row('Server host', manifest.server_host)
    table.add_row('Prepared at', manifest.prepared_at)
    table.add_row('References', str(len(manifest.annotations)))
    table.add_row('Trust sidecar', manifest_digest)
    console.print(table)

    if skipped_annotations:
        body_lines = [
            f'• [bold]{e["path"]}[/bold]\n  {e["reason"]}' for e in skipped_annotations
        ]
        console.print(
            Panel(
                '\n'.join(body_lines),
                title='[yellow]Skipped annotations[/yellow]',
                border_style='yellow',
            )
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

    # pull
    pull = subparsers.add_parser('pull', help='Pull a store from the remote server.')
    pull.add_argument('--store', required=True, help='Store name to pull.')
    pull.add_argument(
        '--dest',
        default=None,
        help='Local destination directory (default: current working directory).',
    )
    pull.add_argument('--compress', action='store_true')
    pull.add_argument(
        '--include-existing-annotations',
        nargs='*',
        metavar='PATH',
        help=(
            'Zarr annotation paths to export as reference files under '
            'reference/ in the session directory.'
        ),
    )
    pull.set_defaults(func=_run_pull)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    args.func(args)

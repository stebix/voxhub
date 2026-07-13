"""Annotator-facing CLI for voxhub remote annotation workflows.

Commands: set-server, set-identity, whoami, list-stores, pull, push.
"""

import argparse
import hashlib
import shutil
import subprocess
import sys
import tempfile
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
    UNCONSTRAINED_SEGMENTATION,
    ChecksumEntry,
    CleanupResponse,
    IntegrateRequest,
    IntegrateResponse,
    IssueRecord,
    ManifestError,
    Ontology,
    PreparePushResponse,
    PrepareRequest,
    PrepareResponse,
    PullManifest,
    RemoteManifest,
    RemoteManifestEntry,
    load_ontology,
    validate_lmk_preflight,
    validate_seg_preflight,
)


class ChecksumError(Exception):
    """Raised when a checksum verification fails after rsync."""


class PushError(Exception):
    """A fatal push precondition failure with a user-facing message."""


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


# -- Push helpers --------------------------------------------------------------


def _verify_push_session(session_dir: Path) -> PullManifest:
    """Read and verify the session's pull manifest, sidecar, and raw volume.

    The trust chain runs sidecar -> manifest -> raw volume: the sidecar
    (written by pull, locked read-only) anchors the manifest, and the
    manifest's checksum anchors the raw volume.  A broken link anywhere
    means the session is not a faithful record of what was pulled — an
    edited manifest could smuggle wrong spatial metadata past pre-flight
    validation, and a modified raw volume means the annotation was drawn
    over data the server never served (stale or tampered pull).

    Returns
    -------
    PullManifest
        The verified manifest.

    Raises
    ------
    PushError
        With a user-facing message naming the broken link and the
        recovery path (re-pull).
    """
    try:
        manifest = PullManifest.read(session_dir)
    except FileNotFoundError as exc:
        raise PushError(
            f'{exc}; not a pull session directory — run '
            f"'voxhub pull' first, then annotate inside the session dir."
        ) from exc
    except ManifestError as exc:
        raise PushError(
            f'pull manifest unreadable or malformed: {exc}; re-pull the store.'
        ) from exc

    if manifest.protocol_version != PROTOCOL_VERSION:
        raise PushError(
            f'pull manifest has protocol version {manifest.protocol_version}, '
            f'this client expects {PROTOCOL_VERSION} — update voxhub on both '
            f'sides and re-pull with a current client.'
        )

    sidecar_path = session_dir / '.voxhub_pull.sha256'
    if not sidecar_path.is_file():
        raise PushError(
            f'trust sidecar missing: {sidecar_path}; the session was not '
            f'completed by a successful pull — re-pull the store.'
        )
    manifest_digest = _compute_sha256(session_dir / '.voxhub_pull.json')
    if sidecar_path.read_text().strip() != manifest_digest:
        raise PushError(
            'pull manifest does not match its trust sidecar — the manifest '
            'has been modified since pull; re-pull the store.'
        )

    raw_path = session_dir / manifest.raw_name
    if not raw_path.is_file():
        raise PushError(
            f'raw volume missing: {raw_path}; re-pull the store before pushing.'
        )
    if _compute_sha256(raw_path) != manifest.raw_checksum:
        raise PushError(
            f'{manifest.raw_name} does not match the pull manifest checksum — '
            f'the raw volume was modified after pull (stale or tampered '
            f'session); re-pull the store and re-export the annotation.'
        )

    return manifest


def _discover_annotation_files(session_dir: Path) -> list[Path]:
    """Find the annotation files to push inside a session directory.

    Scans recursively for ``*.seg.nrrd`` / ``*.mrk.json``, excluding the
    pulled ``reference/`` directory (server-exported reference
    annotations must never be re-pushed; the raw volume never matches
    either glob).  The server integrates at most one segmentation and
    one landmark file per store, so more than one of a kind is an
    error, not a silent pick.

    Raises
    ------
    PushError
        No annotation files, more than one of a kind, or a symlinked
        annotation (push uploads with ``--no-links``, so a symlink would
        silently not arrive).
    """
    reference_dir = session_dir / 'reference'
    discovered: list[Path] = []
    for pattern in ('*.seg.nrrd', '*.mrk.json'):
        for path in sorted(session_dir.rglob(pattern)):
            if reference_dir in path.parents:
                continue
            if path.is_symlink():
                raise PushError(
                    f'annotation file is a symlink: {path}; push uploads '
                    f'with --no-links, so symlinks cannot be pushed — '
                    f'replace it with the real file.'
                )
            if path.is_file():
                discovered.append(path)

    if not discovered:
        raise PushError(
            f'no annotation files (*.seg.nrrd, *.mrk.json) found in '
            f'{session_dir} (the pulled reference/ directory is excluded); '
            f'export your annotation from 3D Slicer into the session '
            f'directory first.'
        )

    for kind, suffix in (('segmentation', '.seg.nrrd'), ('landmark', '.mrk.json')):
        matches = [p for p in discovered if p.name.endswith(suffix)]
        if len(matches) > 1:
            listing = ', '.join(str(p) for p in matches)
            raise PushError(
                f'found {len(matches)} {kind} files ({listing}); a push '
                f'session integrates at most one {kind} file — remove the '
                f'extras or push them from separate sessions.'
            )

    return discovered


def _resolve_ontology_declaration(
    ontology_flags: list[str] | None,
    unconstrained: bool,
    manifest: PullManifest,
) -> tuple[list[str], bool]:
    """Resolve the ontology declaration for this push.

    Mirrors the server's strict policy (exactly one of declared /
    unconstrained): explicit ``--ontology`` / ``--unconstrained`` flags
    win; otherwise the ontologies of the reference annotations recorded
    in the pull manifest are the declared expectation.  A session with
    neither flags nor manifest ontologies must state intent explicitly —
    silent fallback to unconstrained would corrupt provenance.

    Returns
    -------
    tuple[list[str], bool]
        ``(declared_ontologies, unconstrained)``.

    Raises
    ------
    PushError
        On a contradictory or missing declaration.
    """
    declared = list(ontology_flags or [])
    if declared and unconstrained:
        raise PushError(
            '--ontology and --unconstrained are mutually exclusive; '
            'pass one or the other.'
        )
    if unconstrained:
        return [], True
    if declared:
        return declared, False

    from_manifest: list[str] = []
    for entry in manifest.annotations:
        if entry.ontology and entry.ontology not in from_manifest:
            from_manifest.append(entry.ontology)
    if not from_manifest:
        raise PushError(
            'no ontology declared: the pull manifest records no reference '
            'annotations to derive one from — pass --ontology <name> '
            '(repeatable) for enforced integration, or --unconstrained to '
            'explicitly opt out.'
        )
    return from_manifest, False


def _match_files_to_ontologies(
    files: list[Path],
    declared: list[str],
    unconstrained: bool,
) -> dict[Path, Ontology | None]:
    """Match each annotation file to the ontology it must validate against.

    Mirrors the server's resolution in ``_run_integrate_annotations``:
    segmentations take the first declared segmentation-type ontology,
    landmarks the first landmarks-type one; under ``--unconstrained``,
    segmentations validate against the shipped ``unconstrained``
    ontology (structural constraints) and landmarks against ``None``.

    Raises
    ------
    PushError
        When a declared ontology cannot be loaded, or a file's
        annotation type has no matching declared ontology (the server
        would fail that store — surface it before any upload).
    """
    if unconstrained:
        return {
            f: (UNCONSTRAINED_SEGMENTATION if f.name.endswith('.seg.nrrd') else None)
            for f in files
        }

    ontologies: list[Ontology] = []
    for name in declared:
        try:
            ontologies.append(load_ontology(name))
        except FileNotFoundError as exc:
            raise PushError(
                f'unknown ontology {name!r}: not shipped with this voxhub-schema version.'
            ) from exc

    matched: dict[Path, Ontology | None] = {}
    for path in files:
        ann_type = 'segmentation' if path.name.endswith('.seg.nrrd') else 'landmarks'
        candidates = [o for o in ontologies if o.type == ann_type]
        if not candidates:
            raise PushError(
                f'{path.name} matches no declared ontology: declared '
                f'{declared!r} contain no {ann_type}-type ontology — the '
                f'server would refuse this store. Add a matching --ontology '
                f'or pass --unconstrained to opt out explicitly.'
            )
        matched[path] = candidates[0]
    return matched


def _preflight_validate(
    matched: dict[Path, Ontology | None],
    manifest: PullManifest,
) -> dict[Path, list[IssueRecord]]:
    """Run the canonical pre-flight validators over each annotation file.

    Uses the same ``voxhub_schema.validation`` functions the server runs
    at integrate time, against the spatial metadata recorded in the
    (verified) pull manifest, so a clean local pass predicts a clean
    server-side pass.
    """
    manifest_entry = {
        'shape': manifest.shape,
        'origin_lps': manifest.origin_lps,
        'space_directions': manifest.space_directions,
        'spacing_mm': manifest.spacing_mm,
    }
    results: dict[Path, list[IssueRecord]] = {}
    for path, ontology in matched.items():
        if path.name.endswith('.seg.nrrd'):
            results[path] = validate_seg_preflight(path, manifest_entry, ontology)
        else:
            results[path] = validate_lmk_preflight(path, manifest_entry, ontology)
    return results


def _render_validation_results(
    console: Console,
    results: dict[Path, list[IssueRecord]],
) -> tuple[int, int]:
    """Render per-file validation issues; return ``(n_errors, n_warnings)``."""
    n_errors = 0
    n_warnings = 0
    for path, issues in results.items():
        if not issues:
            console.print(f'[green]ok[/green]       {path.name}')
            continue
        lines: list[str] = []
        for issue in issues:
            if issue.severity == 'error':
                n_errors += 1
                lines.append(f'[red]error[/red]    {issue.message}')
            else:
                n_warnings += 1
                lines.append(f'[yellow]warning[/yellow]  {issue.message}')
        has_error = any(i.severity == 'error' for i in issues)
        console.print(
            Panel(
                '\n'.join(lines),
                title=path.name,
                border_style='red' if has_error else 'yellow',
            )
        )
    return n_errors, n_warnings


def _push_checksum_entries(files: list[Path], store_name: str) -> list[ChecksumEntry]:
    """Build staging-dir-relative checksum entries for the upload.

    Uploads land at ``<staging>/<store_name>/<basename>``, so the entry
    path is exactly ``<store_name>/<basename>`` (POSIX separators).
    ``ChecksumEntry`` carries the *bare* 64-hex digest, so the
    ``'sha256:'`` prefix produced by :func:`_compute_sha256` is stripped
    deliberately here — the wire contract owns the format.
    """
    return [
        ChecksumEntry(
            path=f'{store_name}/{f.name}',
            sha256=_compute_sha256(f).removeprefix('sha256:'),
        )
        for f in files
    ]


def _render_integrate_results(
    console: Console,
    integrate: IntegrateResponse,
) -> bool:
    """Render per-store integration outcomes; return whether any failed."""
    any_failed = False
    for store_name, result in sorted(integrate.stores.items()):
        ok = result.status == 'integrated'
        any_failed = any_failed or not ok

        table = Table(title=f'Store {store_name!r}', show_header=False)
        table.add_column(style='bold')
        table.add_column()
        status = '[green]integrated[/green]' if ok else '[red]failed[/red]'
        if result.code:
            status += f' [red]({result.code})[/red]'
        table.add_row('Status', status)
        for ann in result.annotations:
            table.add_row(
                'Annotation',
                f'{ann.path} ({ann.ontology} v{ann.ontology_version})',
            )
        console.print(table)

        if result.issues:
            lines = [
                (
                    f'[red]error[/red]    {i.message}'
                    if i.severity == 'error'
                    else f'[yellow]warning[/yellow]  {i.message}'
                )
                for i in result.issues
            ]
            console.print(
                Panel(
                    '\n'.join(lines),
                    title=f'[bold]{store_name}[/bold] issues',
                    border_style='red' if not ok else 'yellow',
                )
            )
    return any_failed


def _flip_local_manifest(
    session_dir: Path,
    pull_manifest: PullManifest,
    integrate: IntegrateResponse,
    *,
    declared_ontologies: list[str],
    unconstrained: bool,
    push_session_id: str,
) -> None:
    """Record ``integrated`` status in the client-owned local manifest.

    The pull flow writes only the server-authoritative
    ``.voxhub_pull.json``; the client-owned workflow-state manifest
    (``.voxhub_manifest.json``, :class:`RemoteManifest`) is created here
    on the first successful push and updated in place afterwards.
    ``pull_session_id`` records the *push* staging session's ID — the
    same value the server stamps as ``pull_session_id`` in its
    provenance line — so the two records reconcile.

    Raises
    ------
    OSError, ManifestError
        Propagated to the caller, which treats a failed status flip as
        a warning (the annotation is already safely integrated).
    """
    integrated = [
        name for name, result in integrate.stores.items() if result.status == 'integrated'
    ]
    if not integrated:
        return

    try:
        local = RemoteManifest.read(session_dir)
    except FileNotFoundError:
        local = RemoteManifest(
            server_host=pull_manifest.server_host,
            server_stores_dir=pull_manifest.server_stores_dir,
            protocol_version=PROTOCOL_VERSION,
            pull_session_id=push_session_id,
            pulled_at=pull_manifest.prepared_at,
            stores={},
        )

    expected = declared_ontologies or (['unconstrained'] if unconstrained else [])
    for store_name in integrated:
        entry = local.stores.get(store_name)
        if entry is None:
            local.stores[store_name] = RemoteManifestEntry(
                status='integrated',
                raw_checksum=pull_manifest.raw_checksum,
                shape=list(pull_manifest.shape),
                spacing_mm=list(pull_manifest.spacing_mm),
                origin_lps=list(pull_manifest.origin_lps),
                space_directions=[list(r) for r in pull_manifest.space_directions],
                expected_ontologies=list(expected),
                included_annotations=[],
            )
        else:
            entry.status = 'integrated'
    local.write(session_dir)


def _run_push(args: argparse.Namespace) -> None:
    """Push a session's annotation files back to the remote server.

    Steps (launch plan 4.2, adapted to the rpc wire contract):

    1.  Load identity — friendly abort if unconfigured.
    2.  Verify the session: pull manifest + trust sidecar + raw volume
        checksum (stale/tampered sessions abort before any upload).
    3.  Resolve the ontology declaration (flags, else manifest).
    4.  Discover annotation files (``*.seg.nrrd`` / ``*.mrk.json``,
        excluding ``reference/``); match each to a declared ontology.
    5.  Pre-flight validation via ``voxhub_schema.validation``.
        Errors ALWAYS abort — the server enforces the same rule, so no
        client flag can bypass it; warnings are printed and do not
        abort.  ``--validate-only`` stops here with zero SSH calls.
    6.  Compute staging-relative checksums.
    7.  ``prepare-push`` -> server-issued staging dir; rsync the files
        into ``<staging>/<store_name>/`` (addressed by basename — the
        rrsync root-relative contract, see ``_run_pull`` step 5).
    8.  ``integrate-annotations`` (``--force`` forwards for the
        provenance stamp on accepted warnings); render per-store
        results; any failed store makes the exit code non-zero.
    9.  ``cleanup`` — non-fatal (GC backstop).
    10. Flip the local manifest status to ``integrated`` — non-fatal.
    """
    console = Console()
    err_console = Console(stderr=True)

    # -- 1. identity --------------------------------------------------------
    try:
        identity = get_identity()
    except FileNotFoundError as exc:
        err_console.print(f'[red]{exc}[/red]')
        sys.exit(1)

    session_dir = Path(args.session_dir)
    if not session_dir.is_dir():
        err_console.print(f'[red]session directory not found:[/red] {session_dir}')
        sys.exit(1)

    # -- 2.-4. session verification, ontology policy, discovery -------------
    try:
        manifest = _verify_push_session(session_dir)
        declared, unconstrained = _resolve_ontology_declaration(
            args.ontology, bool(args.unconstrained), manifest
        )
        files = _discover_annotation_files(session_dir)
        matched = _match_files_to_ontologies(files, declared, unconstrained)
    except PushError as exc:
        err_console.print(f'[red]push aborted:[/red] {exc}')
        sys.exit(1)

    store_name = manifest.store_name
    ontology_note = (
        'unconstrained (explicit opt-out)' if unconstrained else ', '.join(declared)
    )
    console.print(
        f'Pushing [bold]{len(files)}[/bold] annotation file(s) for store '
        f'[cyan]{store_name}[/cyan] (ontology: {ontology_note})'
    )

    # -- 5. pre-flight validation -------------------------------------------
    results = _preflight_validate(matched, manifest)
    n_errors, n_warnings = _render_validation_results(console, results)

    if n_errors:
        # Error-severity issues can NEVER integrate: the server refuses
        # them regardless of client flags (--force only accepts
        # warnings), so pushing would just fail remotely after a full
        # upload.  Abort here with the rendering above as the guide.
        err_console.print(
            f'[red]push aborted:[/red] {n_errors} validation error(s) — '
            f'errors always abort (the server enforces the same rule; '
            f'--force only accepts warnings). Fix the files and retry.'
        )
        sys.exit(1)
    if n_warnings:
        note = (
            'accepted explicitly (--force is recorded in provenance)'
            if args.force
            else 'the server integrates warnings by default; pass --force '
            'to record explicit acceptance in provenance'
        )
        console.print(f'[yellow]{n_warnings} warning(s)[/yellow] — {note}')

    if args.validate_only:
        console.print('[green]validation passed[/green] (--validate-only: stopping)')
        return

    # -- server + transport (constructed only past --validate-only) ---------
    try:
        server = get_server()
    except FileNotFoundError as exc:
        err_console.print(f'[red]{exc}[/red]')
        sys.exit(1)

    target = server.to_ssh_target()
    runner = SshRunner(target=target)
    transfer = RsyncTransfer(target=target)

    # -- 6. checksums --------------------------------------------------------
    checksums = _push_checksum_entries(files, store_name)

    # -- 7. prepare-push + rsync upload --------------------------------------
    try:
        response = runner.run('prepare-push', {})
    except RemoteError as exc:
        err_console.print(f'[red]prepare-push failed:[/red] {exc}')
        sys.exit(1)

    try:
        prepare = PreparePushResponse.from_dict(response)
    except (KeyError, TypeError, ValueError) as exc:
        err_console.print(
            f'[red]malformed prepare-push response:[/red] {exc!r}; '
            f'server and client schema versions have likely drifted.'
        )
        sys.exit(1)

    staging_dir_remote = prepare.staging_dir
    # Same rrsync contract as _run_pull step 5: the transport addresses
    # the issued staging dir by its staging-root-relative basename; the
    # absolute path stays on the RPC surface (integrate-annotations and
    # cleanup take it verbatim).
    staging_rsync_name = PurePosixPath(staging_dir_remote).name

    # Mirror the upload layout locally (<store_name>/<basename>) so one
    # rsync call lands everything where integrate-annotations iterates
    # (per-store subdirectories of the staging dir).
    with tempfile.TemporaryDirectory(prefix='voxhub-push-') as tmp:
        upload_root = Path(tmp)
        store_dir = upload_root / store_name
        store_dir.mkdir()
        for f in files:
            shutil.copy2(f, store_dir / f.name)

        try:
            transfer.push(str(upload_root), staging_rsync_name)
        except subprocess.CalledProcessError as exc:
            err_console.print(
                f'[red]rsync upload failed[/red] (exit {exc.returncode}); '
                f'nothing was integrated; server staging dir left in place, '
                f'GC will reap.'
            )
            sys.exit(1)

    # -- 8. integrate-annotations --------------------------------------------
    request = IntegrateRequest(
        staging_dir=staging_dir_remote,
        annotator_id=identity.annotator_id,
        machine_id=identity.machine_id,
        nano_id=identity.nano_id,
        checksums=checksums,
        expected_ontology=declared,
        unconstrained=unconstrained,
        force=bool(args.force),
    )

    try:
        response = runner.run('integrate-annotations', attrs.asdict(request))
    except RemoteError as exc:
        err_console.print(
            f'[red]integrate-annotations failed:[/red] {exc}; '
            f'server staging dir left in place for diagnosis, GC will reap.'
        )
        sys.exit(1)

    try:
        integrate = IntegrateResponse.from_dict(response)
    except (KeyError, TypeError, ValueError) as exc:
        err_console.print(
            f'[red]malformed integrate-annotations response:[/red] {exc!r}; '
            f'server and client schema versions have likely drifted.'
        )
        sys.exit(1)

    any_failed = _render_integrate_results(console, integrate)

    # -- 9. cleanup ACK (non-fatal) -------------------------------------------
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

    # -- 10. local manifest status (non-fatal) --------------------------------
    try:
        _flip_local_manifest(
            session_dir,
            manifest,
            integrate,
            declared_ontologies=declared,
            unconstrained=unconstrained,
            push_session_id=staging_rsync_name,
        )
    except (OSError, ManifestError) as exc:
        err_console.print(
            f'[yellow]warning: could not update local manifest status: {exc}[/yellow]'
        )

    if any_failed:
        err_console.print(
            '[red]push finished with failed store(s)[/red] — see the '
            'issues above; nothing from a failed store was integrated.'
        )
        sys.exit(1)

    console.print('[green]push complete[/green]')


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

    # push
    push = subparsers.add_parser(
        'push',
        help='Push session annotations back to the remote server.',
    )
    push.add_argument(
        'session_dir',
        help='Local pull-session directory containing the annotation files.',
    )
    push.add_argument(
        '--ontology',
        action='append',
        default=None,
        metavar='NAME',
        help=(
            'Ontology declared for this push (repeatable). Defaults to the '
            'ontologies of the reference annotations recorded in the pull '
            'manifest; required when the manifest records none. Mutually '
            'exclusive with --unconstrained.'
        ),
    )
    push.add_argument(
        '--unconstrained',
        action='store_true',
        help=(
            'Explicitly opt out of ontology enforcement (structural checks '
            'still apply). Mutually exclusive with --ontology.'
        ),
    )
    push.add_argument(
        '--validate-only',
        action='store_true',
        dest='validate_only',
        help='Run client-side pre-flight validation and stop — no server contact.',
    )
    push.add_argument(
        '--force',
        action='store_true',
        help=(
            'Accept validation WARNINGS explicitly; recorded in server '
            'provenance ("forced"). Errors always abort — no flag bypasses '
            'them.'
        ),
    )
    push.set_defaults(func=_run_push)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    args.func(args)

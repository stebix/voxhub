"""SSH-invoked server CLI.

Annotator-facing transport: the single ``rpc`` subcommand (wire
protocol v3) reads one JSON request object from stdin —
``{"protocol_version": 3, "method": ..., "params": {...}}`` — and
writes one JSON response to stdout.  The per-method subcommands
(list-stores, prepare-pull, integrate-annotations, cleanup,
healthcheck) remain for one release as deprecated shims over the same
dispatch; ``prepare-push`` is rpc-only (new surface, no legacy shim).
Operator commands (gc, catalog, validate-attributes) stay plain
subcommands and are not reachable via ``rpc``.

Every invocation writes a single JSON object to stdout and exits.
Structured errors use the ``ServerError`` envelope.  Logs go to
stderr via structlog.
"""

import argparse
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

import zarr

from voxhub_core.attributes import (
    get_dataset_attributes,
    validate_dataset_attributes,
)
from voxhub_core.catalog import discover_zarr_stores
from voxhub_core.extraction import (
    ExtractionError,
    compute_sha256,
    extract_landmarks,
    extract_segmentation,
    extract_spatial_metadata,
    extract_volume,
)
from voxhub_core.integrate import (
    find_annotation_files,
    parse_mrk_json,
    parse_seg_nrrd,
    write_landmarks_to_zarr,
    write_segmentation_to_zarr,
)
from voxhub_core.memory_budget import (
    MemoryBudget,
    MemoryBudgetError,
    MemoryWarning,
)
from voxhub_core.memory_budget import (
    check as check_memory_budget,
)
from voxhub_core.server import catalog_cache
from voxhub_core.server.locks import store_lock
from voxhub_core.server.logging import configure_logging, get_logger
from voxhub_core.server.provenance import (
    record_provenance,
    validate_provenance_jsonl,
)
from voxhub_core.server.settings import (
    MemorySettings,
    SettingsError,
    load_settings,
)
from voxhub_core.slicer import estimate_seg_nrrd_ram_bytes
from voxhub_schema import (
    PROTOCOL_VERSION,
    UNCONSTRAINED_SEGMENTATION,
    AnnotatorSlugError,
    ChecksumEntry,
    CleanupResponse,
    IntegrateRequest,
    IntegrateResponse,
    IntegrateResult,
    IssueRecord,
    ManifestError,
    Ontology,
    PreparePushResponse,
    PrepareRequest,
    PrepareResponse,
    PullAnnotationEntry,
    PullManifest,
    ServerError,
    generate_nano_id,
    load_ontology,
    parse_annotator_slug,
    serialize,
    validate_lmk_preflight,
    validate_seg_preflight,
)

STAGING_DIR_PREFIX: str = 'vxhb-staging-'

_SHA256_HEX64 = re.compile(r'[0-9a-f]{64}')
"""Bare sha256 digest shape used by legacy ``--checksums`` translation."""

# Segmentation extraction holds an extra transient copy compared to the raw
# volume path (label_map + pynrrd internal buffer + astype).  Bump the
# safety factor locally so the low-memory check reflects that worst case.
_SEGMENTATION_SAFETY_BUMP: float = 1.5

# Refuse to stage or integrate when a target filesystem is this full.  A
# nearly-full disk risks torn writes (an annotation committed to zarr with
# no room left for its provenance line) and orphaned staging dirs.
_DISK_FULL_THRESHOLD: float = 0.90


def _check_disk_space(paths: list[Path]) -> str | None:
    """Return a message if any of ``paths`` sits on a >=90%-full filesystem.

    Parameters
    ----------
    paths : list[Path]
        Target directories to probe with :func:`shutil.disk_usage`.
        Unreadable or zero-total filesystems are skipped.

    Returns
    -------
    str | None
        A human-readable refusal message for the first filesystem over the
        :data:`_DISK_FULL_THRESHOLD`, or ``None`` when every probed path has
        headroom.  Callers convert the message into a ``disk_full``
        ``ServerError`` envelope and exit without touching anything.
    """
    for path in paths:
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        if usage.total <= 0:
            continue
        used_fraction = usage.used / usage.total
        if used_fraction >= _DISK_FULL_THRESHOLD:
            return (
                f'filesystem at {path} is {used_fraction * 100:.1f}% full '
                f'(>= {_DISK_FULL_THRESHOLD * 100:.0f}% threshold); refusing '
                'to write'
            )
    return None


def _memory_budget(
    memory: MemorySettings | None,
    *,
    extra_safety: float = 1.0,
) -> MemoryBudget:
    """Build a :class:`MemoryBudget` from server settings.

    ``memory`` is ``None`` in the rare case where ``args.memory_settings``
    was not attached (e.g. unit tests that invoke a handler directly);
    the returned budget falls back to warn-only with the default
    threshold.  ``extra_safety`` is multiplied into ``safety_factor`` to
    accommodate paths with additional transient copies (segmentation).
    """
    if memory is None:
        return MemoryBudget.warn_only()
    return MemoryBudget(
        warn_threshold_bytes=memory.max_safe_volume_mb * 1024 * 1024,
        refuse_when_low=memory.refuse_when_low_memory,
        safety_factor=memory.safety_factor * extra_safety,
    )


def _log_memory_warnings(
    log: Any,
    warnings: list[MemoryWarning],
    *,
    store: str,
    phase: str,
) -> None:
    """Emit one structured log line per warning so ops sees them in journal."""
    for w in warnings:
        log.warning(
            'memory_advisory',
            phase=phase,
            store=store,
            code=w.code,
            volume_bytes=w.volume_bytes,
            available_bytes=w.available_bytes,
            threshold_bytes=w.threshold_bytes,
            message=w.message,
        )


def _validate_echoed_staging_dir(raw: str, staging_root: Path) -> Path:
    """Confine a client-echoed staging_dir to the operator-configured root.

    ``cleanup`` and ``integrate-annotations`` accept a ``staging_dir``
    path that the client echoes back from a prior ``prepare-pull``
    response.  A malicious or buggy SSH principal can craft any path;
    without confinement ``cleanup`` would rmtree arbitrary locations and
    ``integrate-annotations`` would treat attacker-chosen files as
    annotation sources.

    Returns the resolved path if it lives under ``staging_root`` and its
    basename starts with :data:`STAGING_DIR_PREFIX`.  Raises ``ValueError``
    otherwise; callers convert that into an ``invalid_staging_dir``
    ``ServerError`` envelope.
    """
    candidate = Path(raw).resolve()
    root = staging_root.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f'staging_dir {raw!r} is outside the configured staging root {root}'
        ) from exc
    if not candidate.name.startswith(STAGING_DIR_PREFIX):
        raise ValueError(
            f'staging_dir {raw!r} does not carry the {STAGING_DIR_PREFIX!r} prefix'
        )
    return candidate


def _validate_include_annotation_path(raw: str) -> str:
    """Validate a client-supplied ``--include-existing-annotations`` path.

    Reference-annotation paths address a group inside the store's own zarr
    hierarchy, so they must match the ``annotations/<slug>/<instance>``
    shape exactly: a relative POSIX path of three segments with no parent
    (``..``) components.  Relying on zarr's incidental key validation is not
    enough — a crafted ``../../etc`` could otherwise escape the store.

    Parameters
    ----------
    raw : str
        The client-supplied path.

    Returns
    -------
    str
        ``raw`` unchanged when valid.

    Raises
    ------
    ValueError
        When the path is absolute, contains ``..``, or does not have the
        expected three-segment ``annotations/<slug>/<instance>`` shape.
        Callers convert this into an ``invalid_annotation_path`` envelope.
    """
    candidate = PurePosixPath(raw)
    if candidate.is_absolute():
        raise ValueError(f'annotation path {raw!r} must be relative')
    parts = candidate.parts
    if '..' in parts:
        raise ValueError(f'annotation path {raw!r} must not contain ".."')
    if len(parts) != 3 or parts[0] != 'annotations':
        raise ValueError(
            f'annotation path {raw!r} must have the shape annotations/<slug>/<instance>'
        )
    return raw


def _write_json(obj: object) -> None:
    """Write an attrs instance as JSON to stdout."""
    sys.stdout.write(serialize(obj) + '\n')
    sys.stdout.flush()


def _write_dict(d: dict[str, Any]) -> None:
    """Write a plain dict as JSON to stdout."""
    sys.stdout.write(json.dumps(d) + '\n')
    sys.stdout.flush()


def _write_error(code: str, message: str) -> None:
    """Write a structured error envelope to stdout."""
    _write_json(
        ServerError(
            protocol_version=PROTOCOL_VERSION,
            error=True,
            code=code,
            message=message,
        )
    )


# -- annotator identity ------------------------------------------------------


class _IdentityMismatchError(Exception):
    """The SSH-key-bound annotator disagrees with the client-sent flag."""


def _resolve_annotator_identity(
    flag_annotator_id: str | None,
    *,
    log: Any,
) -> tuple[str | None, str]:
    """Resolve the effective annotator identity and record its source.

    The ``VOXHUB_ANNOTATOR`` environment variable is injected by sshd from
    the connecting key's ``environment="VOXHUB_ANNOTATOR=<name>"`` option
    (see ``scripts/deploy/add-annotator.sh`` and the ``PermitUserEnvironment
    VOXHUB_ANNOTATOR`` line in the sshd ``Match User voxhub`` block).  When
    present it is **authoritative**: it overrides any client-sent
    ``--annotator-id`` so provenance is cryptographically anchored to the
    key, not to a self-reported flag.  A disagreement between the two is a
    misconfigured client worth surfacing rather than silently papering over,
    so it fails the request.  When the variable is absent -- local/dev use
    and loopback tests that never traverse sshd -- the client flag is used
    exactly as before.

    Parameters
    ----------
    flag_annotator_id : str | None
        The client-sent ``--annotator-id`` value, or ``None`` when the
        caller does not supply one.
    log
        structlog logger; the resolved source is logged for the audit trail.

    Returns
    -------
    tuple[str | None, str]
        ``(annotator_id, identity_source)``.  ``identity_source`` is
        ``'ssh_key'`` (key-bound), ``'flag'`` (client-supplied), or
        ``'none'`` (neither available -- only reachable from an anonymous
        local ``prepare-pull``).

    Raises
    ------
    _IdentityMismatchError
        If the key-bound identity and the client flag are both present and
        disagree.
    """
    env_annotator = (os.environ.get('VOXHUB_ANNOTATOR') or '').strip()
    if env_annotator:
        if flag_annotator_id is not None and flag_annotator_id != env_annotator:
            raise _IdentityMismatchError(
                f'SSH key is bound to annotator {env_annotator!r} but the '
                f'client sent --annotator-id {flag_annotator_id!r}; refusing '
                'to record a mismatched identity.'
            )
        log.info(
            'identity_resolved',
            annotator_id=env_annotator,
            identity_source='ssh_key',
            flag_annotator_id=flag_annotator_id,
        )
        return env_annotator, 'ssh_key'
    if flag_annotator_id is not None:
        log.info(
            'identity_resolved',
            annotator_id=flag_annotator_id,
            identity_source='flag',
        )
        return flag_annotator_id, 'flag'
    log.info('identity_resolved', annotator_id=None, identity_source='none')
    return None, 'none'


# -- list-stores -------------------------------------------------------------


def _run_list_stores(args: argparse.Namespace) -> None:
    log = get_logger(command='list-stores')
    t0 = time.monotonic()

    stores_dir = Path(args.stores_dir)
    if_version: int | None = getattr(args, 'if_version', None)
    log.info(
        'list_stores_started',
        stores_dir=str(stores_dir),
        if_version=if_version,
    )

    snapshot = catalog_cache.read_catalog(stores_dir)

    # Client short-circuit: if the caller already holds catalog_version N
    # and the server still serves N, skip the payload entirely. The cache
    # read is unavoidable -- we have to know the current version before we
    # can decide to short-circuit.
    if if_version is not None and if_version == snapshot.catalog_version:
        duration = time.monotonic() - t0
        log.info(
            'list_stores_unchanged',
            catalog_version=snapshot.catalog_version,
            cache_age_s=round(catalog_cache.age_seconds(snapshot.built_at), 3),
            duration_s=round(duration, 3),
        )
        _write_dict(
            {
                'protocol_version': PROTOCOL_VERSION,
                'catalog_version': snapshot.catalog_version,
                'unchanged': True,
            }
        )
        return

    duration = time.monotonic() - t0
    log.info(
        'list_stores_completed',
        store_count=len(snapshot.stores),
        catalog_version=snapshot.catalog_version,
        cache_age_s=round(catalog_cache.age_seconds(snapshot.built_at), 3),
        duration_s=round(duration, 3),
    )

    _write_dict(
        {
            'protocol_version': PROTOCOL_VERSION,
            'catalog_version': snapshot.catalog_version,
            'stores': list(snapshot.stores.values()),
        }
    )


# -- prepare-pull ------------------------------------------------------------


def _extract_reference_annotations(
    zarr_path: Path,
    staging_dir: Path,
    include_annotations: list[str],
    log: Any,
    *,
    budget: MemoryBudget | None = None,
    warnings_out: list[MemoryWarning] | None = None,
) -> tuple[list[PullAnnotationEntry], list[dict[str, str]]]:
    """Extract requested annotations to ``<staging_dir>/reference/``.

    Returns ``(manifest_entries, skipped)``.  Failures on individual
    annotations are non-fatal: they are appended to ``skipped`` with a
    human-readable reason and the remaining annotations continue.

    Annotation-level :class:`MemoryBudgetError` (refusal under tight RAM)
    is treated the same way as :class:`ExtractionError`: the offending
    annotation is skipped, the rest still extract.  Soft warnings are
    appended to ``warnings_out`` if provided.
    """
    manifest_entries: list[PullAnnotationEntry] = []
    skipped: list[dict[str, str]] = []

    if not include_annotations:
        return manifest_entries, skipped

    ref_dir = staging_dir / 'reference'

    for ann_path in include_annotations:
        src_array = zarr_path / ann_path / 'data'
        if not src_array.exists():
            skipped.append({'path': ann_path, 'reason': 'array not found in store'})
            log.warning('annotation_missing', path=ann_path)
            continue

        try:
            grp = zarr.open_group(zarr_path / ann_path, mode='r')
            arr = grp['data']
            a = dict(arr.attrs)
            kind = 'segmentation' if 'segments' in a else 'landmarks'
        except Exception as exc:
            skipped.append({'path': ann_path, 'reason': f'could not open: {exc}'})
            log.warning('annotation_open_failed', path=ann_path, error=str(exc))
            continue

        instance_name = Path(ann_path).name
        annotator_slug = Path(ann_path).parts[-2]
        try:
            annotator_id, _nano_id = parse_annotator_slug(annotator_slug)
        except AnnotatorSlugError as exc:
            skipped.append(
                {'path': ann_path, 'reason': f'malformed annotator slug: {exc}'}
            )
            log.warning('malformed_slug', path=ann_path, error=str(exc))
            continue

        ext = '.seg.nrrd' if kind == 'segmentation' else '.mrk.json'
        ref_filename = f'{annotator_slug}_{instance_name}{ext}'
        ref_dir.mkdir(exist_ok=True)
        ref_dest = ref_dir / ref_filename

        try:
            array_zarr_path = str(Path(ann_path) / 'data')
            if kind == 'segmentation':
                ann_warnings: list[MemoryWarning] = []
                checksum = extract_segmentation(
                    zarr_path,
                    array_zarr_path,
                    ref_dest,
                    budget=budget,
                    warnings_out=ann_warnings,
                )
                if warnings_out is not None:
                    warnings_out.extend(ann_warnings)
                _log_memory_warnings(
                    log, ann_warnings, store=zarr_path.name, phase='reference_seg'
                )
            else:
                checksum = extract_landmarks(zarr_path, array_zarr_path, ref_dest)
        except MemoryBudgetError as exc:
            ref_dest.unlink(missing_ok=True)
            log.warning(
                'annotation_extraction_refused_low_memory',
                path=ann_path,
                error=str(exc),
            )
            skipped.append({'path': ann_path, 'reason': str(exc)})
            continue
        except ExtractionError as exc:
            # Extraction may have written partial bytes before failing;
            # remove any orphan so the session dir only contains files
            # the manifest will list.
            ref_dest.unlink(missing_ok=True)
            log.warning('annotation_extraction_failed', path=ann_path, error=str(exc))
            skipped.append({'path': ann_path, 'reason': str(exc)})
            continue

        manifest_entries.append(
            PullAnnotationEntry(
                zarr_source_path=ann_path,
                kind=kind,
                ontology=str(a.get('ontology', '')),
                ontology_version=int(a.get('ontology_version', 0) or 0),
                annotator_id=annotator_id,
                integrated_at=str(a.get('integrated_at', '')),
                reference_filename=ref_filename,
                reference_checksum=checksum,
            )
        )

    return manifest_entries, skipped


def _run_prepare_pull(args: argparse.Namespace) -> None:
    log = get_logger(command='prepare-pull')
    t0 = time.monotonic()

    stores_dir = Path(args.stores_dir)
    staging_root = Path(args.staging_root)
    store_name: str = args.store
    compress: bool = args.compress
    include_annotations: list[str] = args.include_existing_annotations or []

    # Resolve who is pulling from the SSH-key-bound identity (falling back to
    # the optional flag for local/dev).  The staging dir minted below names
    # the pull_session_id later stamped into integrate provenance, so binding
    # the identity here makes that session attributable to the connecting key.
    try:
        pulled_by, identity_source = _resolve_annotator_identity(
            getattr(args, 'annotator_id', None), log=log
        )
    except _IdentityMismatchError as exc:
        log.error('identity_mismatch', error=str(exc))
        _write_error('identity_mismatch', str(exc))
        sys.exit(1)

    log.info(
        'prepare_pull_started',
        stores_dir=str(stores_dir),
        staging_root=str(staging_root),
        store=store_name,
        include_annotations=include_annotations,
        annotator_id=pulled_by,
        identity_source=identity_source,
    )

    # -- Disk-full precondition (before anything is touched) ----------------
    disk_full = _check_disk_space([staging_root])
    if disk_full is not None:
        log.error('disk_full', staging_root=str(staging_root), detail=disk_full)
        _write_error('disk_full', disk_full)
        sys.exit(1)

    # Validate client-supplied reference-annotation paths (task 2.8) before
    # touching the filesystem: they must match annotations/<slug>/<instance>
    # exactly, never escaping the store via ``..``.
    for ann_path in include_annotations:
        try:
            _validate_include_annotation_path(ann_path)
        except ValueError as exc:
            log.error('invalid_annotation_path', path=ann_path, error=str(exc))
            _write_error('invalid_annotation_path', str(exc))
            sys.exit(1)

    # -- Task 1: upfront store validation (before mkdtemp) ------------------
    zarr_path = stores_dir / f'{store_name}.zarr'
    if not zarr_path.is_dir():
        log.error('store_not_found', store=store_name, stores_dir=str(stores_dir))
        _write_error('store_not_found', f'Store not found: {store_name!r}')
        sys.exit(1)

    # Staging dir path is server-authoritative: no client-supplied override.
    # ``tempfile.mkdtemp(dir=staging_root)`` yields a collision-free sibling
    # under the operator-configured root.  The nano_id in the prefix makes
    # the path human-greppable in logs; the mkdtemp suffix guarantees
    # uniqueness even under concurrent invocations.
    session_id = generate_nano_id()
    staging_dir = Path(
        tempfile.mkdtemp(
            prefix=f'{STAGING_DIR_PREFIX}{session_id}-',
            dir=staging_root,
        )
    )

    # -- Task 2a: extract raw volume at <staging_dir>/raw.nrrd --------------
    memory_settings: MemorySettings | None = getattr(args, 'memory_settings', None)
    raw_budget = _memory_budget(memory_settings)
    seg_budget = _memory_budget(memory_settings, extra_safety=_SEGMENTATION_SAFETY_BUMP)
    memory_warnings: list[MemoryWarning] = []
    try:
        meta = extract_volume(
            zarr_path,
            staging_dir / 'raw.nrrd',
            compress=compress,
            budget=raw_budget,
        )
    except MemoryBudgetError as exc:
        log.error(
            'prepare_pull_refused_low_memory',
            store=store_name,
            error=str(exc),
            volume_bytes=exc.warning.volume_bytes,
            available_bytes=exc.warning.available_bytes,
        )
        # Clean up the freshly-created staging dir instead of leaving it for
        # gc (task 2.8).
        shutil.rmtree(staging_dir, ignore_errors=True)
        _write_error('insufficient_memory', str(exc))
        sys.exit(1)
    except Exception as exc:
        log.error('prepare_pull_failed', error=str(exc))
        shutil.rmtree(staging_dir, ignore_errors=True)
        _write_error('prepare_pull_failed', str(exc))
        sys.exit(1)

    raw_warnings: list[MemoryWarning] = list(meta.pop('warnings', []))
    memory_warnings.extend(raw_warnings)
    _log_memory_warnings(log, raw_warnings, store=store_name, phase='raw_volume')

    # -- Task 2b: extract reference annotations -----------------------------
    ann_entries, skipped_annotations = _extract_reference_annotations(
        zarr_path,
        staging_dir,
        include_annotations,
        log,
        budget=seg_budget,
        warnings_out=memory_warnings,
    )

    # -- Task 3: write PullManifest to staging dir --------------------------
    try:
        PullManifest(
            protocol_version=PROTOCOL_VERSION,
            prepared_at=datetime.now(UTC).isoformat(),
            server_host=socket.getfqdn(),
            server_stores_dir=str(stores_dir),
            store_name=store_name,
            raw_name='raw.nrrd',
            raw_checksum=meta['raw_checksum'],
            shape=meta['shape'],
            spacing_mm=meta['spacing_mm'],
            origin_lps=meta['origin_lps'],
            space_directions=meta['space_directions'],
            annotations=ann_entries,
        ).write(staging_dir)
    except OSError as exc:
        log.error('manifest_write_failed', error=str(exc))
        shutil.rmtree(staging_dir, ignore_errors=True)
        _write_error('prepare_pull_failed', f'failed to write pull manifest: {exc}')
        sys.exit(1)

    duration = time.monotonic() - t0
    log.info(
        'prepare_pull_completed',
        store=store_name,
        staging_dir=str(staging_dir),
        extracted_annotations=len(ann_entries),
        skipped_annotations=len(skipped_annotations),
        memory_warning_count=len(memory_warnings),
        duration_s=round(duration, 3),
        annotator_id=pulled_by,
        identity_source=identity_source,
    )

    _write_json(
        PrepareResponse(
            protocol_version=PROTOCOL_VERSION,
            staging_dir=str(staging_dir),
            server_host=socket.getfqdn(),
            server_stores_dir=str(stores_dir),
            store_name=store_name,
            raw_name='raw.nrrd',
            raw_checksum=meta['raw_checksum'],
            shape=meta['shape'],
            spacing_mm=meta['spacing_mm'],
            origin_lps=meta['origin_lps'],
            space_directions=meta['space_directions'],
            skipped_annotations=skipped_annotations,
            memory_warnings=[w.to_dict() for w in memory_warnings],
        )
    )


# -- prepare-push --------------------------------------------------------------


def _run_prepare_push(args: argparse.Namespace) -> None:
    """Mint a server-authoritative staging dir for an upcoming push.

    Mirrors ``prepare-pull``'s staging creation and confinement: the
    directory is a ``mkdtemp`` child of the operator-configured staging
    root carrying the :data:`STAGING_DIR_PREFIX`, so the echoed-path
    validator accepts it for the follow-up ``integrate-annotations`` /
    ``cleanup`` calls and rrsync (rooted at the staging root) can
    address it by basename.  The old client-side ``mktemp`` push
    staging is dead — the server is authoritative over the path.
    """
    log = get_logger(command='prepare-push')

    staging_root = Path(args.staging_root)

    # Resolve who is pushing from the SSH-key-bound identity (falling back
    # to the optional param for local/dev), mirroring prepare-pull: the
    # minted dir names the session later stamped into integrate provenance.
    try:
        pushed_by, identity_source = _resolve_annotator_identity(
            getattr(args, 'annotator_id', None), log=log
        )
    except _IdentityMismatchError as exc:
        log.error('identity_mismatch', error=str(exc))
        _write_error('identity_mismatch', str(exc))
        sys.exit(1)

    log.info(
        'prepare_push_started',
        staging_root=str(staging_root),
        annotator_id=pushed_by,
        identity_source=identity_source,
    )

    # Disk-full precondition (before anything is touched) — same guard as
    # prepare-pull: refuse to mint a staging dir the upload would then
    # fill on an essentially-full filesystem.
    disk_full = _check_disk_space([staging_root])
    if disk_full is not None:
        log.error('disk_full', staging_root=str(staging_root), detail=disk_full)
        _write_error('disk_full', disk_full)
        sys.exit(1)

    session_id = generate_nano_id()
    staging_dir = Path(
        tempfile.mkdtemp(
            prefix=f'{STAGING_DIR_PREFIX}{session_id}-',
            dir=staging_root,
        )
    )

    log.info(
        'prepare_push_completed',
        staging_dir=str(staging_dir),
        annotator_id=pushed_by,
        identity_source=identity_source,
    )

    _write_json(
        PreparePushResponse(
            protocol_version=PROTOCOL_VERSION,
            staging_dir=str(staging_dir),
        )
    )


# -- integrate-annotations ---------------------------------------------------


def _resolve_ontologies(
    expected_ontologies: list[str],
    annotation_type: str,
    log: Any,
) -> tuple[list[Ontology], list[IssueRecord]]:
    """Load ontology objects for the expected ontology names.

    Parameters
    ----------
    expected_ontologies : list[str]
        Ontology names the client declared for this integration via
        ``--expected-ontology``.
    annotation_type : str
        ``'segmentation'`` or ``'landmarks'`` -- used to filter.
    log
        structlog logger.

    Returns
    -------
    tuple[list[Ontology], list[IssueRecord]]
        Loaded ontologies matching ``annotation_type`` and any issues
        encountered during loading.  A named-but-unfindable ontology
        becomes a ``warning`` issue (the client declared intent; the
        server reports the resolution failure); it is the caller's
        responsibility to error if the resulting list is empty when one
        was expected.
    """
    ontologies: list[Ontology] = []
    issues: list[IssueRecord] = []

    for ont_name in expected_ontologies:
        try:
            ont = load_ontology(ont_name)
        except FileNotFoundError:
            issues.append(
                IssueRecord(
                    severity='warning',
                    message=(f'Expected ontology {ont_name!r} not found on server'),
                )
            )
            log.warning('ontology_not_found', ontology=ont_name)
            continue

        if ont.type == annotation_type:
            ontologies.append(ont)

    return ontologies, issues


def _find_invalid_staging_entries(store_dir: Path, staging_dir: Path) -> list[str]:
    """Detect symlinks and staging-escaping paths in a staged store dir.

    The push transport is writable (rrsync without ``-ro``, launch 4.3)
    and rsync ``-a`` preserves symlinks: a symlink in staging pointing at
    ``stores_dir`` or ``.meta/provenance.jsonl`` could otherwise fool the
    file reads below (checksum, parse, provenance).  The voxhub client
    uploads with ``--no-links``, so any symlink arriving here is at best
    a non-voxhub client and at worst an attack — the owning store is
    refused wholesale (``code='invalid_staging_content'``) before any
    staged file is read.

    Checks the store dir itself plus every entry beneath it: an entry is
    an offender when it ``is_symlink()`` or when its ``resolve()``
    escapes the staging dir (belt-and-braces for resolution tricks a
    plain symlink check might miss).

    Returns
    -------
    list[str]
        Human-readable offender descriptions; empty means clean.
    """
    root = staging_dir.resolve()
    offenders: list[str] = []
    for entry in (store_dir, *sorted(store_dir.rglob('*'))):
        rel = entry.relative_to(staging_dir)
        if entry.is_symlink():
            offenders.append(f'{rel} is a symlink')
            continue
        try:
            resolved = entry.resolve()
        except OSError as exc:
            offenders.append(f'{rel} could not be resolved: {exc}')
            continue
        if not resolved.is_relative_to(root):
            offenders.append(f'{rel} resolves outside the staging dir ({resolved})')
    return offenders


def _rollback_annotation_group(zarr_path: Path, data_path: str) -> None:
    """Delete a just-written annotation group after a post-write failure.

    Upholds the invariant that *an annotation exists in a zarr store iff its
    provenance line exists*: when ``record_provenance`` fails after the array
    write has already committed, the orphaned annotation group must be
    removed before the store is reported failed — otherwise a client retry
    creates a duplicate.

    Parameters
    ----------
    zarr_path : Path
        Path to the ``.zarr`` store.
    data_path : str
        Array path of the just-written annotation
        (``annotations/<slug>/<instance>/data``).  Its parent — the instance
        group — is deleted.

    Notes
    -----
    The caller must hold the per-store lock.  Deletion failures are
    swallowed: the store is already being reported failed, and re-raising
    here would mask the original provenance error.
    """
    instance_parts = data_path.strip('/').split('/')[:-1]
    if not instance_parts:
        return
    try:
        root = zarr.open_group(zarr_path, mode='r+')
        parent = root
        for part in instance_parts[:-1]:
            parent = parent[part]
        del parent[instance_parts[-1]]
    except (KeyError, OSError):
        pass


def _run_integrate_annotations(args: argparse.Namespace) -> None:
    log = get_logger(command='integrate-annotations')
    t0 = time.monotonic()

    stores_dir = Path(args.stores_dir)
    staging_root = Path(args.staging_root)
    try:
        staging_dir = _validate_echoed_staging_dir(args.staging_dir, staging_root)
    except ValueError as exc:
        log.error('invalid_staging_dir', staging_dir=args.staging_dir, error=str(exc))
        _write_error('invalid_staging_dir', str(exc))
        sys.exit(1)
    # Resolve the effective annotator identity.  Over SSH the key-bound
    # VOXHUB_ANNOTATOR env var overrides the client flag so provenance cannot
    # record an identity other than the one bound to the connecting key; a
    # flag that disagrees with the key fails the request.  Locally (no env
    # var) the required --annotator-id flag is used exactly as before.
    try:
        resolved_annotator, identity_source = _resolve_annotator_identity(
            args.annotator_id, log=log
        )
    except _IdentityMismatchError as exc:
        log.error('identity_mismatch', error=str(exc))
        _write_error('identity_mismatch', str(exc))
        sys.exit(1)
    # --annotator-id is required for integrate, so resolution never yields None.
    assert resolved_annotator is not None
    annotator_id = resolved_annotator
    machine_id = args.machine_id
    nano_id = args.nano_id
    force = args.force
    checksums: list[ChecksumEntry] = args.checksums or []
    # Staging-dir-relative POSIX paths covered by a malformed legacy
    # ``--checksums`` token.  Only ever non-empty on the deprecated
    # subcommand path: the rpc path rejects malformed entries wholesale at
    # deserialization (``ChecksumEntry.from_dict``).
    malformed_checksum_paths: set[str] = set(
        getattr(args, 'malformed_checksum_paths', None) or ()
    )
    declared_ontologies: list[str] = list(args.expected_ontology or [])
    unconstrained: bool = bool(args.unconstrained)

    # Disk-full precondition: refuse before writing anything if either the
    # staging filesystem or the stores filesystem is essentially full.
    disk_full = _check_disk_space([staging_root, stores_dir])
    if disk_full is not None:
        log.error(
            'disk_full',
            staging_root=str(staging_root),
            stores_dir=str(stores_dir),
            detail=disk_full,
        )
        _write_error('disk_full', disk_full)
        sys.exit(1)

    # Segmentation parse is gated on available RAM (task 2.6): a pushed
    # .seg.nrrd is materialized into memory, so use its file size as a cheap
    # upper-bound estimate and keep the segmentation safety factor.
    memory_settings: MemorySettings | None = getattr(args, 'memory_settings', None)
    seg_budget = _memory_budget(memory_settings, extra_safety=_SEGMENTATION_SAFETY_BUMP)

    # Ontology policy: the client must state intent.  Silent fallback to
    # "unconstrained" would corrupt the downstream ground-truth story,
    # since a missing declaration is indistinguishable from a deliberate
    # no-ontology integration in the provenance record.  Require exactly
    # one of --expected-ontology / --unconstrained.
    if not declared_ontologies and not unconstrained:
        msg = (
            'integrate-annotations requires an ontology declaration: pass '
            '--expected-ontology <name> (repeatable) for enforced '
            'integration, or --unconstrained to explicitly opt out.'
        )
        log.error('ontology_not_declared')
        _write_error('ontology_not_declared', msg)
        sys.exit(1)
    if declared_ontologies and unconstrained:
        msg = (
            '--expected-ontology and --unconstrained are mutually '
            'exclusive; pass one or the other.'
        )
        log.error('ambiguous_ontology_spec')
        _write_error('ambiguous_ontology_spec', msg)
        sys.exit(1)

    log.info(
        'integrate_started',
        stores_dir=str(stores_dir),
        staging_dir=str(staging_dir),
        annotator_id=annotator_id,
        identity_source=identity_source,
        ontology_policy='unconstrained' if unconstrained else 'declared',
        declared_ontologies=declared_ontologies,
    )

    # Expected checksums keyed by staging-dir-relative POSIX path
    # (``<store-dir>/<filename>``).  Path keys keep equal basenames in
    # different store subdirectories distinct (triage 2026-07-11 P1); the
    # values are bare 64-hex sha256 digests.
    expected_checksums: dict[str, str] = {e.path: e.sha256 for e in checksums}

    date_str = datetime.now(UTC).strftime('%Y%m%d')
    annotator_dir = f'{annotator_id}-{nano_id}'
    pull_session_id = staging_dir.name

    stores_result: dict[str, dict[str, Any]] = {}

    for store_dir in sorted(staging_dir.iterdir()):
        if not store_dir.is_dir() or store_dir.name.startswith('.'):
            continue
        # Pulled reference annotations live under <staging>/reference/; that
        # subdir must never be discovered as a store and re-integrated
        # (task 2.8).
        if store_dir.name == 'reference':
            continue

        store_name = store_dir.name
        zarr_path = stores_dir / f'{store_name}.zarr'

        if not zarr_path.is_dir():
            continue

        # Symlink / escape defense (task 4.3): refuse the store before
        # reading ANY staged file — checksum verification of a symlink's
        # target would otherwise "succeed" and launder outside content.
        invalid_entries = _find_invalid_staging_entries(store_dir, staging_dir)
        if invalid_entries:
            detail = '; '.join(invalid_entries)
            log.error('invalid_staging_content', store=store_name, detail=detail)
            stores_result[store_name] = {
                'status': 'failed',
                'annotations': [],
                'issues': [
                    {
                        'severity': 'error',
                        'message': (
                            f'Invalid staging content: {detail}. Symlinks '
                            f'and paths escaping the staging dir are '
                            f'refused; nothing from this store was '
                            f'integrated.'
                        ),
                    }
                ],
                'code': 'invalid_staging_content',
            }
            continue

        seg_file, lmk_file = find_annotation_files(store_dir)
        if seg_file is None and lmk_file is None:
            continue

        # Fail-closed checksum verification (task 2.5).  When the client
        # supplies any checksums it is asserting the integrity of every
        # uploaded file; a file missing from the set, or a malformed entry,
        # would silently skip its integrity check.  Refuse the store instead.
        # Files are addressed by staging-dir-relative POSIX path so equal
        # basenames across store subdirectories stay distinct.
        if expected_checksums or malformed_checksum_paths:
            checksum_issue: str | None = None
            for ann_file in (seg_file, lmk_file):
                if ann_file is None:
                    continue
                rel_path = ann_file.relative_to(staging_dir).as_posix()
                if rel_path in malformed_checksum_paths:
                    checksum_issue = (
                        f'Malformed checksum entry for {rel_path}; '
                        'refusing to integrate an unverified file.'
                    )
                elif rel_path not in expected_checksums:
                    checksum_issue = (
                        f'No checksum provided for {rel_path}; '
                        'refusing to integrate an unverified file.'
                    )
                if checksum_issue is not None:
                    log.error(
                        'checksum_verification_failed',
                        store=store_name,
                        file=rel_path,
                    )
                    break
            if checksum_issue is not None:
                stores_result[store_name] = {
                    'status': 'failed',
                    'annotations': [],
                    'issues': [{'severity': 'error', 'message': checksum_issue}],
                }
                continue

        # Verify checksums.  A mismatch fails this store only (task 2.8):
        # earlier stores may already have committed, so never sys.exit
        # mid-loop — record the failure and continue so the response reports
        # every store's outcome.
        if expected_checksums:
            checksum_mismatch: str | None = None
            for ann_file in (seg_file, lmk_file):
                if ann_file is None:
                    continue
                rel_path = ann_file.relative_to(staging_dir).as_posix()
                actual = compute_sha256(ann_file)
                expected = expected_checksums.get(rel_path)
                if expected and actual != f'sha256:{expected}':
                    checksum_mismatch = (
                        f'Checksum mismatch for {rel_path}: '
                        f'expected sha256:{expected}, got {actual}'
                    )
                    log.error(
                        'checksum_mismatch',
                        store=store_name,
                        file=rel_path,
                    )
                    break
            if checksum_mismatch is not None:
                stores_result[store_name] = {
                    'status': 'failed',
                    'annotations': [],
                    'issues': [{'severity': 'error', 'message': checksum_mismatch}],
                }
                continue

        # Read volume metadata.
        root = zarr.open_group(zarr_path, mode='r')
        arr = root['raw']['full']
        vol_attrs = dict(arr.attrs)
        origin, space_directions, spacing_mm = extract_spatial_metadata(vol_attrs)
        manifest_entry = {
            'shape': list(arr.shape),
            'origin_lps': origin.tolist(),
            'space_directions': space_directions.tolist(),
            'spacing_mm': spacing_mm,
        }

        # Resolve the declared ontologies per annotation type.  Skipped
        # under the explicit --unconstrained policy (seg/lmk stay None).
        if unconstrained:
            seg_ontologies: list[Ontology] = []
            lmk_ontologies: list[Ontology] = []
            seg_ont_issues: list[IssueRecord] = []
            lmk_ont_issues: list[IssueRecord] = []
        else:
            seg_ontologies, seg_ont_issues = _resolve_ontologies(
                declared_ontologies, 'segmentation', log
            )
            lmk_ontologies, lmk_ont_issues = _resolve_ontologies(
                declared_ontologies, 'landmarks', log
            )

        issues: list[IssueRecord] = []
        issues.extend(seg_ont_issues)
        issues.extend(lmk_ont_issues)
        annotations_written: list[dict[str, Any]] = []
        # Set when error-severity validation issues block a write.  Errors
        # always fail the store — ``--force`` may only accept warnings
        # (arch plan A.2, decision 3) — and the failure is reported with
        # ``code='validation_failed'`` so the client can render it.
        validation_failed = False

        with store_lock(zarr_path):
            # Integrate segmentation.
            if seg_file is not None:
                # Under --unconstrained, seg_ontology is None by design.
                # Under declared, we require at least one matching
                # ontology — silent-fallback to unconstrained would
                # misrepresent the provenance record.
                if not unconstrained and not seg_ontologies:
                    issues.append(
                        IssueRecord(
                            severity='error',
                            message=(
                                'No declared ontology matches annotation '
                                'type segmentation. Declared: '
                                f'{declared_ontologies!r}. Re-run with a '
                                'matching --expected-ontology or pass '
                                '--unconstrained to opt out explicitly.'
                            ),
                        )
                    )
                    log.error(
                        'seg_ontology_type_mismatch',
                        store=store_name,
                        declared=declared_ontologies,
                    )
                else:
                    # Under --unconstrained, validate against the shipped
                    # ``unconstrained`` ontology so its structural constraints
                    # are enforced on the live path (they were skipped when a
                    # bare ``None`` was passed).
                    if unconstrained:
                        seg_ontology = UNCONSTRAINED_SEGMENTATION
                    else:
                        seg_ontology = seg_ontologies[0] if seg_ontologies else None
                    try:
                        # Gate the in-RAM parse on available memory (task
                        # 2.6): validate_seg_preflight materializes the full
                        # .seg.nrrd into memory to validate it (and the write
                        # path below re-parses it once more), so refuse this
                        # store rather than risk an OOM kill.  Budget on the
                        # DECOMPRESSED size estimated from the NRRD header
                        # (shape x itemsize) — Slicer writes gzip NRRD and
                        # label maps compress 20-100x, so the on-disk file
                        # size wildly under-estimates the parse cost.  A
                        # header that cannot be read raises (malformed NRRD)
                        # and fails this store closed via the handler below.
                        seg_mem_warnings = check_memory_budget(
                            estimate_seg_nrrd_ram_bytes(seg_file),
                            budget=seg_budget,
                            context=store_name,
                        )
                        _log_memory_warnings(
                            log,
                            seg_mem_warnings,
                            store=store_name,
                            phase='integrate_seg',
                        )
                        seg_issues = validate_seg_preflight(
                            seg_file,
                            manifest_entry,
                            seg_ontology,
                        )
                        issues.extend(seg_issues)

                        errors = [i for i in seg_issues if i.severity == 'error']
                        if errors:
                            # Error-severity issues always fail the store:
                            # no client flag combination (including
                            # --force) can override them (arch plan A.2).
                            validation_failed = True
                            log.warning(
                                'seg_validation_errors',
                                store=store_name,
                                errors=[i.message for i in errors],
                                force_requested=force,
                            )
                        else:
                            seg_warnings = [
                                i for i in seg_issues if i.severity == 'warning'
                            ]
                            # ``force`` was exercised iff it accepted
                            # warnings; stamp that in provenance + zarr
                            # attrs so audits can find force-accepted
                            # annotations.
                            seg_forced = force and bool(seg_warnings)
                            seg_data = parse_seg_nrrd(seg_file)
                            ont_name = (
                                seg_ontology.name if seg_ontology else 'unconstrained'
                            )
                            ont_version = seg_ontology.version if seg_ontology else 1

                            short_random = generate_nano_id(size=4)
                            instance_dir = f'{ont_name}-{date_str}-{short_random}'
                            seg_path = f'annotations/{annotator_dir}/{instance_dir}/data'

                            write_segmentation_to_zarr(
                                zarr_path,
                                seg_data,
                                seg_path,
                                ontology=seg_ontology,
                                force=force,
                                forced=seg_forced,
                            )

                            seg_checksum = compute_sha256(seg_file)
                            # Invariant: an annotation exists in zarr iff its
                            # provenance line exists.  The array write has
                            # committed; if provenance recording fails, roll
                            # back the just-created group (the per-store lock
                            # is still held) before reporting the store failed,
                            # so a client retry cannot create a duplicate.
                            try:
                                record_provenance(
                                    stores_dir,
                                    store_name,
                                    seg_path,
                                    annotator_id=annotator_id,
                                    machine_id=machine_id,
                                    nano_id=nano_id,
                                    pull_session_id=pull_session_id,
                                    ontology=ont_name,
                                    ontology_version=ont_version,
                                    source_nrrd_checksum=seg_checksum,
                                    source_file=seg_file.name,
                                    identity_source=identity_source,
                                    issues=seg_warnings,
                                    forced=seg_forced,
                                )
                            except Exception:
                                _rollback_annotation_group(zarr_path, seg_path)
                                raise

                            annotations_written.append(
                                {
                                    'path': seg_path,
                                    'ontology': ont_name,
                                    'ontology_version': ont_version,
                                }
                            )

                    except MemoryBudgetError as exc:
                        issues.append(
                            IssueRecord(
                                severity='error',
                                message=(
                                    f'Insufficient memory to integrate '
                                    f'segmentation: {exc}'
                                ),
                            )
                        )
                        log.error(
                            'integrate_refused_low_memory',
                            store=store_name,
                            error=str(exc),
                        )
                    except Exception as exc:
                        issues.append(
                            IssueRecord(
                                severity='error',
                                message=(f'Segmentation integration failed: {exc}'),
                            )
                        )
                        log.error(
                            'seg_integrate_failed',
                            store=store_name,
                            error=str(exc),
                        )

            # Integrate landmarks.
            if lmk_file is not None:
                if not unconstrained and not lmk_ontologies:
                    issues.append(
                        IssueRecord(
                            severity='error',
                            message=(
                                'No declared ontology matches annotation '
                                'type landmarks. Declared: '
                                f'{declared_ontologies!r}. Re-run with a '
                                'matching --expected-ontology or pass '
                                '--unconstrained to opt out explicitly.'
                            ),
                        )
                    )
                    log.error(
                        'lmk_ontology_type_mismatch',
                        store=store_name,
                        declared=declared_ontologies,
                    )
                else:
                    # No unconstrained landmark ontology exists, so under
                    # --unconstrained landmarks validate with ontology=None
                    # (structural checks only).
                    lmk_ontology = lmk_ontologies[0] if lmk_ontologies else None
                    try:
                        lmk_issues = validate_lmk_preflight(
                            lmk_file,
                            manifest_entry,
                            lmk_ontology,
                        )
                        issues.extend(lmk_issues)

                        errors = [i for i in lmk_issues if i.severity == 'error']
                        if errors:
                            # Error-severity issues always fail the store:
                            # no client flag combination (including
                            # --force) can override them (arch plan A.2).
                            validation_failed = True
                            log.warning(
                                'lmk_validation_errors',
                                store=store_name,
                                errors=[i.message for i in errors],
                                force_requested=force,
                            )
                        else:
                            lmk_warnings = [
                                i for i in lmk_issues if i.severity == 'warning'
                            ]
                            # ``force`` was exercised iff it accepted
                            # warnings; stamp that in provenance + zarr
                            # attrs so audits can find force-accepted
                            # annotations.
                            lmk_forced = force and bool(lmk_warnings)
                            lmk_data = parse_mrk_json(lmk_file)
                            ont_name = (
                                lmk_ontology.name if lmk_ontology else 'unconstrained'
                            )
                            ont_version = lmk_ontology.version if lmk_ontology else 1

                            short_random = generate_nano_id(size=4)
                            instance_dir = f'{ont_name}-{date_str}-{short_random}'
                            lmk_path = f'annotations/{annotator_dir}/{instance_dir}/data'

                            write_landmarks_to_zarr(
                                zarr_path,
                                lmk_data,
                                lmk_path,
                                ontology=lmk_ontology,
                                force=force,
                                forced=lmk_forced,
                            )

                            lmk_checksum = compute_sha256(lmk_file)
                            # Invariant: an annotation exists in zarr iff its
                            # provenance line exists.  The array write has
                            # committed; if provenance recording fails, roll
                            # back the just-created group (the per-store lock
                            # is still held) before reporting the store failed,
                            # so a client retry cannot create a duplicate.
                            try:
                                record_provenance(
                                    stores_dir,
                                    store_name,
                                    lmk_path,
                                    annotator_id=annotator_id,
                                    machine_id=machine_id,
                                    nano_id=nano_id,
                                    pull_session_id=pull_session_id,
                                    ontology=ont_name,
                                    ontology_version=ont_version,
                                    source_nrrd_checksum=lmk_checksum,
                                    source_file=lmk_file.name,
                                    identity_source=identity_source,
                                    issues=lmk_warnings,
                                    forced=lmk_forced,
                                )
                            except Exception:
                                _rollback_annotation_group(zarr_path, lmk_path)
                                raise

                            annotations_written.append(
                                {
                                    'path': lmk_path,
                                    'ontology': ont_name,
                                    'ontology_version': ont_version,
                                }
                            )

                    except Exception as exc:
                        issues.append(
                            IssueRecord(
                                severity='error',
                                message=f'Landmark integration failed: {exc}',
                            )
                        )
                        log.error(
                            'lmk_integrate_failed',
                            store=store_name,
                            error=str(exc),
                        )

        # Invalidate the catalog cache outside store_lock: the zarr write has
        # already committed, and holding store_lock while taking the catalog
        # lock would invert the lock order. A failure here must not fail the
        # push -- the TTL + fingerprint path reconciles on the next read.
        if annotations_written:
            try:
                catalog_cache.invalidate_store(stores_dir, store_name)
            except Exception as exc:
                log.warning(
                    'catalog_invalidate_failed',
                    store=store_name,
                    error=str(exc),
                )

        store_result: dict[str, Any] = {
            'status': ('integrated' if annotations_written else 'failed'),
            'annotations': annotations_written,
            'issues': [{'severity': i.severity, 'message': i.message} for i in issues],
        }
        if store_result['status'] == 'failed' and validation_failed:
            store_result['code'] = 'validation_failed'
        stores_result[store_name] = store_result

    duration = time.monotonic() - t0
    any_failed = any(r['status'] != 'integrated' for r in stores_result.values())
    log.info(
        'integrate_completed',
        stores=list(stores_result.keys()),
        any_failed=any_failed,
        duration_s=round(duration, 3),
    )

    # Always emit the full per-store JSON so the client can reconcile every
    # store's outcome, then signal partial or total failure via a non-zero
    # exit (task 2.8).  An empty batch (no stores discovered) is a success.
    # Routing through IntegrateResponse keeps the wire shape pinned to the
    # schema model.
    _write_json(
        IntegrateResponse(
            protocol_version=PROTOCOL_VERSION,
            stores={
                name: IntegrateResult.from_dict(info)
                for name, info in stores_result.items()
            },
        )
    )

    if any_failed:
        sys.exit(1)


# -- cleanup -----------------------------------------------------------------


def _read_pull_manifest_safely(staging_dir: Path) -> PullManifest | None:
    """Best-effort read of the staging dir's ``.voxhub_pull.json``.

    Used purely for observability — never raises.  Returns ``None`` if
    the manifest is absent (prepare-pull crashed before writing it) or
    unparseable.
    """
    try:
        return PullManifest.read(staging_dir)
    except (FileNotFoundError, OSError, ManifestError):
        return None


def _run_cleanup(args: argparse.Namespace) -> None:
    log = get_logger(command='cleanup')
    staging_root = Path(args.staging_root)
    try:
        staging_dir = _validate_echoed_staging_dir(args.staging_dir, staging_root)
    except ValueError as exc:
        log.error('invalid_staging_dir', staging_dir=args.staging_dir, error=str(exc))
        _write_error('invalid_staging_dir', str(exc))
        sys.exit(1)

    log.info('cleanup_started', staging_dir=str(staging_dir))

    if staging_dir.is_dir():
        manifest = _read_pull_manifest_safely(staging_dir)
        try:
            age_seconds = int(time.time() - staging_dir.stat().st_mtime)
        except OSError:
            age_seconds = -1
        shutil.rmtree(staging_dir)
        log.info(
            'staging_dir_reaped',
            staging_dir=str(staging_dir),
            age_seconds=age_seconds,
            store_name=manifest.store_name if manifest else None,
            had_manifest=manifest is not None,
            reason='client_ack',
        )
    else:
        log.warning('cleanup_not_found', staging_dir=str(staging_dir))

    _write_json(CleanupResponse(protocol_version=PROTOCOL_VERSION, status='ok'))


# -- gc ----------------------------------------------------------------------


def _run_gc(args: argparse.Namespace) -> None:
    log = get_logger(command='gc')
    ttl_hours = args.ttl_hours

    tmp_root = Path(args.staging_root)
    log.info('gc_started', ttl_hours=ttl_hours, staging_root=str(tmp_root))

    cutoff = time.time() - (ttl_hours * 3600)

    removed: list[str] = []
    for entry in tmp_root.iterdir():
        if not entry.is_dir():
            continue
        if not entry.name.startswith(STAGING_DIR_PREFIX):
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            manifest = _read_pull_manifest_safely(entry)
            age_seconds = int(time.time() - mtime)
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(str(entry))
            log.info(
                'staging_dir_reaped',
                staging_dir=str(entry),
                age_seconds=age_seconds,
                store_name=manifest.store_name if manifest else None,
                had_manifest=manifest is not None,
                reason='gc_unacked',
            )

    log.info('gc_completed', removed_count=len(removed))

    _write_dict(
        {
            'protocol_version': PROTOCOL_VERSION,
            'removed': removed,
            'count': len(removed),
        }
    )


# -- validate-attributes -----------------------------------------------------


def _run_validate_attributes(args: argparse.Namespace) -> None:
    log = get_logger(command='validate-attributes')
    t0 = time.monotonic()

    stores_dir = Path(args.stores_dir)
    selected_stores: list[str] | None = args.stores

    log.info(
        'validate_attributes_started',
        stores_dir=str(stores_dir),
        stores=selected_stores,
    )

    entries = discover_zarr_stores(stores_dir)

    results: dict[str, dict[str, Any]] = {}
    for entry in entries:
        store_name = entry.path.name.removesuffix('.zarr')
        if selected_stores and store_name not in selected_stores:
            continue

        da = get_dataset_attributes(entry.path)
        if da is None:
            results[store_name] = {'status': 'missing', 'issues': []}
            continue

        issues = validate_dataset_attributes(entry.path)
        if issues:
            results[store_name] = {
                'status': 'warning',
                'issues': [
                    {
                        'field': i.field,
                        'declared': i.declared,
                        'actual': i.actual,
                        'message': i.message,
                    }
                    for i in issues
                ],
            }
        else:
            results[store_name] = {'status': 'ok', 'issues': []}

    duration = time.monotonic() - t0
    log.info(
        'validate_attributes_completed',
        store_count=len(results),
        duration_s=round(duration, 3),
    )

    _write_dict(
        {
            'protocol_version': PROTOCOL_VERSION,
            'results': results,
        }
    )


# -- catalog -----------------------------------------------------------------


def _run_catalog_refresh(args: argparse.Namespace) -> None:
    log = get_logger(command='catalog-refresh')
    stores_dir = Path(args.stores_dir)
    store_name: str | None = args.store

    if store_name is not None:
        # Guard against typos: only accept names that exist on disk or in
        # the current cache. Allowing in-cache-only names preserves the
        # "drop stale entry" use case.
        zarr_path = stores_dir / f'{store_name}.zarr'
        in_cache = False
        existing = catalog_cache.try_load(stores_dir)
        if existing is not None and store_name in existing.stores:
            in_cache = True
        if not zarr_path.is_dir() and not in_cache:
            msg = (
                f'Store {store_name!r} not found on disk and not present '
                f'in the catalog cache under {stores_dir}'
            )
            log.error('catalog_store_not_found', store=store_name)
            _write_error('store_not_found', msg)
            sys.exit(1)

        snapshot = catalog_cache.invalidate_store(stores_dir, store_name)
        log.info(
            'catalog_store_refreshed',
            store=store_name,
            catalog_version=snapshot.catalog_version,
        )
    else:
        snapshot = catalog_cache.rebuild(stores_dir)
        log.info(
            'catalog_rebuilt',
            store_count=len(snapshot.stores),
            catalog_version=snapshot.catalog_version,
        )

    _write_dict(
        {
            'protocol_version': PROTOCOL_VERSION,
            'catalog_version': snapshot.catalog_version,
            'store_count': len(snapshot.stores),
            'built_at': snapshot.built_at,
        }
    )


def _run_catalog_show(args: argparse.Namespace) -> None:
    log = get_logger(command='catalog-show')
    stores_dir = Path(args.stores_dir)
    snapshot = catalog_cache.read_catalog(stores_dir)
    log.info(
        'catalog_show_completed',
        store_count=len(snapshot.stores),
        catalog_version=snapshot.catalog_version,
    )

    _write_dict(
        {
            'protocol_version': PROTOCOL_VERSION,
            'catalog_version': snapshot.catalog_version,
            'built_at': snapshot.built_at,
            'stores_dir_fingerprint': snapshot.stores_dir_fingerprint,
            'stores': list(snapshot.stores.values()),
        }
    )


def _run_catalog_stats(args: argparse.Namespace) -> None:
    log = get_logger(command='catalog-stats')
    stores_dir = Path(args.stores_dir)
    stats = catalog_cache.peek_stats(stores_dir)
    log.info('catalog_stats_completed', status=stats['status'])

    _write_dict(
        {
            'protocol_version': PROTOCOL_VERSION,
            **stats,
        }
    )


# -- healthcheck -------------------------------------------------------------


def _check_python_version() -> dict[str, str]:
    """Check that the Python version is 3.12+."""
    version = sys.version.split()[0]
    major, minor = sys.version_info[:2]
    if (major, minor) >= (3, 12):
        return {'name': 'python_version', 'status': 'ok', 'detail': version}
    return {
        'name': 'python_version',
        'status': 'fail',
        'detail': f'{version} (requires >= 3.12)',
    }


def _check_packages() -> dict[str, str]:
    """Check that required voxhub packages are importable."""
    missing: list[str] = []
    for pkg in ('voxhub_schema', 'voxhub_core', 'voxhub_client'):
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)

    if not missing:
        return {'name': 'packages', 'status': 'ok', 'detail': 'all importable'}
    return {
        'name': 'packages',
        'status': 'fail',
        'detail': f'missing: {", ".join(missing)}',
    }


def _check_rsync() -> dict[str, str]:
    """Check that rsync is available on PATH."""
    import shutil as _shutil

    rsync = _shutil.which('rsync')
    if rsync:
        return {'name': 'rsync', 'status': 'ok', 'detail': rsync}
    return {'name': 'rsync', 'status': 'fail', 'detail': 'not found on PATH'}


def _check_stores_dir(stores_dir: Path) -> dict[str, str]:
    """Check that the stores directory exists and is writable."""
    if not stores_dir.is_dir():
        return {
            'name': 'stores_dir',
            'status': 'fail',
            'detail': f'{stores_dir} is not a directory',
        }
    if not os.access(stores_dir, os.R_OK | os.W_OK):
        return {
            'name': 'stores_dir',
            'status': 'fail',
            'detail': f'{stores_dir} is not readable/writable',
        }
    return {'name': 'stores_dir', 'status': 'ok', 'detail': str(stores_dir)}


def _check_stores(stores_dir: Path) -> dict[str, str]:
    """Discover zarr stores and report their status."""
    entries = discover_zarr_stores(stores_dir)
    errored = [e for e in entries if e.error]
    total = len(entries)

    if not entries:
        return {'name': 'stores', 'status': 'ok', 'detail': 'no stores found'}
    if errored:
        names = ', '.join(e.path.name.removesuffix('.zarr') for e in errored)
        return {
            'name': 'stores',
            'status': 'fail',
            'detail': f'{len(errored)}/{total} stores have errors: {names}',
        }
    return {
        'name': 'stores',
        'status': 'ok',
        'detail': f'{total} stores healthy',
    }


def _check_provenance(stores_dir: Path) -> dict[str, str]:
    """Validate all provenance JSONL files under the stores directory."""
    meta_dir = stores_dir / '.meta'
    jsonl_path = meta_dir / 'provenance.jsonl'

    if not jsonl_path.is_file():
        return {
            'name': 'provenance',
            'status': 'ok',
            'detail': 'no provenance file yet',
        }

    errors = validate_provenance_jsonl(jsonl_path)
    if errors:
        return {
            'name': 'provenance',
            'status': 'fail',
            'detail': f'{len(errors)} malformed entries',
        }
    return {'name': 'provenance', 'status': 'ok', 'detail': 'valid'}


def _run_healthcheck(args: argparse.Namespace) -> None:
    log = get_logger(command='healthcheck')
    t0 = time.monotonic()

    stores_dir = Path(args.stores_dir)
    log.info('healthcheck_started', stores_dir=str(stores_dir))

    checks = [
        _check_python_version(),
        _check_packages(),
        _check_rsync(),
        _check_stores_dir(stores_dir),
    ]

    # Only run store/provenance checks if stores_dir is accessible.
    if checks[-1]['status'] == 'ok':
        checks.append(_check_stores(stores_dir))
        checks.append(_check_provenance(stores_dir))

    any_failed = any(c['status'] == 'fail' for c in checks)
    status = 'degraded' if any_failed else 'healthy'

    duration = time.monotonic() - t0
    log.info(
        'healthcheck_completed',
        status=status,
        duration_s=round(duration, 3),
    )

    _write_dict(
        {
            'protocol_version': PROTOCOL_VERSION,
            'status': status,
            'python_version': sys.version.split()[0],
            'checks': checks,
        }
    )

    if any_failed:
        sys.exit(1)


# -- rpc dispatch -------------------------------------------------------------
#
# The annotator-facing wire protocol (v3) is a single JSON object on stdin:
# ``{"protocol_version": 3, "method": <name>, "params": {...}}``.  ``params``
# is deserialized through the schema request models so the models are the
# actual contract; the deprecated per-method subcommands are shims that build
# the same params dict and go through the same ``_dispatch``, so the two
# surfaces cannot drift.


def _params_list_stores(params: dict[str, Any]) -> dict[str, Any]:
    """Map ``list-stores`` params (no request model) to handler fields."""
    return {'if_version': params.get('if_version')}


def _params_prepare_pull(params: dict[str, Any]) -> dict[str, Any]:
    """Deserialize ``prepare-pull`` params via :class:`PrepareRequest`."""
    req = PrepareRequest.from_dict(params)
    return {
        'store': req.store_name,
        'include_existing_annotations': req.include_existing_annotations,
        'compress': req.compress,
        'annotator_id': req.annotator_id,
    }


def _params_integrate_annotations(params: dict[str, Any]) -> dict[str, Any]:
    """Deserialize ``integrate-annotations`` params via :class:`IntegrateRequest`."""
    req = IntegrateRequest.from_dict(params)
    return {
        'staging_dir': req.staging_dir,
        'annotator_id': req.annotator_id,
        'machine_id': req.machine_id,
        'nano_id': req.nano_id,
        'checksums': req.checksums,
        'expected_ontology': req.expected_ontology,
        'unconstrained': req.unconstrained,
        'force': req.force,
        # Malformed legacy tokens only exist on the shim path; the rpc path
        # rejects them wholesale in ChecksumEntry.from_dict.
        'malformed_checksum_paths': set(),
    }


def _params_prepare_push(params: dict[str, Any]) -> dict[str, Any]:
    """Validate ``prepare-push`` params (no request model).

    Minimal by design: the server mints the staging dir itself, so the
    only accepted field is the optional local/dev ``annotator_id``
    (over SSH the key-bound ``VOXHUB_ANNOTATOR`` env var is
    authoritative, exactly as for ``prepare-pull``).
    """
    annotator_id = params.get('annotator_id')
    if annotator_id is not None and not isinstance(annotator_id, str):
        raise TypeError(f'Expected string for annotator_id, got {type(annotator_id)}')
    return {'annotator_id': annotator_id}


def _params_cleanup(params: dict[str, Any]) -> dict[str, Any]:
    """Validate ``cleanup`` params (``{"staging_dir": str}``, no model)."""
    staging_dir = params['staging_dir']
    if not isinstance(staging_dir, str):
        raise TypeError(f'Expected string for staging_dir, got {type(staging_dir)}')
    return {'staging_dir': staging_dir}


def _params_healthcheck(params: dict[str, Any]) -> dict[str, Any]:
    """``healthcheck`` takes no params."""
    return {}


type _MethodSpec = tuple['Callable[[dict[str, Any]], dict[str, Any]]', str]

# Annotator-reachable method surface.  Operator commands (gc, catalog,
# validate-attributes) are deliberately absent: they stay plain
# subcommands and are unreachable via ``rpc`` (and therefore unreachable
# over annotator SSH, whose forced command only allows ``rpc``).
#
# Handlers are referenced by NAME and resolved at dispatch time so tests
# (and future instrumentation) can monkeypatch the module attribute.
_RPC_METHODS: dict[str, _MethodSpec] = {
    'list-stores': (_params_list_stores, '_run_list_stores'),
    'prepare-pull': (_params_prepare_pull, '_run_prepare_pull'),
    # rpc-only (protocol v3, launch plan 4.1): new surface, no legacy shim.
    'prepare-push': (_params_prepare_push, '_run_prepare_push'),
    'integrate-annotations': (
        _params_integrate_annotations,
        '_run_integrate_annotations',
    ),
    'cleanup': (_params_cleanup, '_run_cleanup'),
    'healthcheck': (_params_healthcheck, '_run_healthcheck'),
}


def _dispatch(
    method: str,
    params: dict[str, Any],
    base_args: argparse.Namespace,
    *,
    overrides: dict[str, Any] | None = None,
) -> None:
    """Dispatch one method call to its ``_run_*`` handler.

    Both the ``rpc`` subcommand and the deprecated per-method shims funnel
    through here, so their behaviour is identical by construction.

    Parameters
    ----------
    method : str
        Method name; must be a key of :data:`_RPC_METHODS`, otherwise an
        ``unknown_method`` envelope is written and the process exits 1.
    params : dict[str, Any]
        Raw request params, deserialized through the method's schema
        request model.  Model errors become an ``invalid_params`` envelope.
    base_args : argparse.Namespace
        The parsed invocation namespace; supplies the settings-injected
        attributes (``stores_dir``, ``staging_root``, ``memory_settings``).
    overrides : dict[str, Any] | None
        Extra handler fields applied after params deserialization.  Used by
        the legacy integrate shim to carry malformed-checksum bookkeeping.
    """
    spec = _RPC_METHODS.get(method)
    if spec is None:
        _write_error(
            'unknown_method',
            f'unknown method {method!r}; expected one of {sorted(_RPC_METHODS)}',
        )
        sys.exit(1)
    build_fields, handler_name = spec
    handler: Callable[[argparse.Namespace], None] = globals()[handler_name]

    try:
        fields = build_fields(params)
    except (KeyError, TypeError, ValueError) as exc:
        detail = f'missing field {exc}' if isinstance(exc, KeyError) else str(exc)
        _write_error('invalid_params', f'invalid params for {method!r}: {detail}')
        sys.exit(1)

    ns = argparse.Namespace(**vars(base_args))
    for key, value in fields.items():
        setattr(ns, key, value)
    for key, value in (overrides or {}).items():
        setattr(ns, key, value)
    handler(ns)


def _run_rpc(args: argparse.Namespace) -> None:
    """Serve a single JSON request from stdin (wire protocol v3).

    Reads stdin to EOF, parses exactly one JSON object, validates
    ``protocol_version`` bidirectionally, and dispatches on ``method``.
    Every failure is a structured ``ServerError`` envelope on stdout with
    exit code 1 — never a traceback.
    """
    log = get_logger(command='rpc')
    raw = sys.stdin.read()

    try:
        request = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.error('rpc_malformed_request', error=str(exc))
        _write_error(
            'malformed_request',
            f'stdin must carry exactly one JSON object: {exc}',
        )
        sys.exit(1)
    if not isinstance(request, dict):
        log.error('rpc_malformed_request', error='not a JSON object')
        _write_error(
            'malformed_request',
            f'request must be a JSON object, got {type(request).__name__}',
        )
        sys.exit(1)

    declared = request.get('protocol_version')
    if declared != PROTOCOL_VERSION:
        log.error('rpc_protocol_mismatch', declared=declared)
        _write_error(
            'protocol_mismatch',
            f'server speaks protocol_version {PROTOCOL_VERSION}, request '
            f'declared {declared!r}; upgrade the client or server so the '
            'versions match',
        )
        sys.exit(1)

    method = request.get('method')
    if not isinstance(method, str) or not method:
        log.error('rpc_malformed_request', error='missing method')
        _write_error('malformed_request', 'request is missing a string "method"')
        sys.exit(1)

    params = request.get('params', {})
    if not isinstance(params, dict):
        log.error('rpc_malformed_request', error='params not an object')
        _write_error(
            'malformed_request',
            f'"params" must be a JSON object, got {type(params).__name__}',
        )
        sys.exit(1)

    log.info('rpc_request', method=method)
    _dispatch(method, params, args)


# -- deprecated per-method shims ----------------------------------------------
#
# Kept for one release (operator muscle memory + rollout overlap), then
# deleted — see docs/issues.md.  Each shim builds the params dict its rpc
# equivalent would receive and calls the same _dispatch.

_DEPRECATION_NOTE = (
    'Deprecated compatibility shim over `voxhub-server rpc`; '
    'will be removed one release after protocol v2 ships (docs/issues.md).'
)


def _shim_list_stores(args: argparse.Namespace) -> None:
    """Deprecated ``list-stores`` subcommand — shim over ``rpc``."""
    params: dict[str, Any] = {}
    if getattr(args, 'if_version', None) is not None:
        params['if_version'] = args.if_version
    _dispatch('list-stores', params, args)


def _shim_prepare_pull(args: argparse.Namespace) -> None:
    """Deprecated ``prepare-pull`` subcommand — shim over ``rpc``."""
    params: dict[str, Any] = {
        'store_name': args.store,
        'include_existing_annotations': args.include_existing_annotations,
        'compress': args.compress,
        'annotator_id': args.annotator_id,
    }
    _dispatch('prepare-pull', params, args)


def _translate_legacy_checksums(
    tokens: list[str],
    staging_dir: Path,
) -> tuple[list[dict[str, str]], set[str]]:
    """Translate legacy ``<filename>:sha256:<hex>`` tokens to path entries.

    Legacy tokens are keyed by basename; verification is keyed by
    staging-dir-relative POSIX path.  Each well-formed token is expanded to
    every staged file carrying that basename — reproducing the legacy
    (deliberately ambiguous) semantics under the one internal format.
    Tokens that are not well-formed ``<filename>:sha256:<64 lowercase hex>``
    mark their matching files malformed instead, so the owning store fails
    closed exactly as before (task 2.5).

    Parameters
    ----------
    tokens : list[str]
        Raw ``--checksums`` values.
    staging_dir : Path
        The already-validated staging directory to expand basenames against.

    Returns
    -------
    tuple[list[dict[str, str]], set[str]]
        ``(entries, malformed_paths)`` — serialized :class:`ChecksumEntry`
        dicts and the relative paths covered by malformed tokens.
    """
    staged_files = [p for p in sorted(staging_dir.rglob('*')) if p.is_file()]
    entries: list[dict[str, str]] = []
    malformed_paths: set[str] = set()
    for token in tokens:
        parts = token.split(':', 2)
        name = parts[0]
        well_formed = (
            len(parts) == 3
            and parts[1] == 'sha256'
            and _SHA256_HEX64.fullmatch(parts[2]) is not None
        )
        for staged in staged_files:
            if staged.name != name:
                continue
            rel_path = staged.relative_to(staging_dir).as_posix()
            if well_formed:
                entries.append({'path': rel_path, 'sha256': parts[2]})
            else:
                malformed_paths.add(rel_path)
    return entries, malformed_paths


def _shim_integrate_annotations(args: argparse.Namespace) -> None:
    """Deprecated ``integrate-annotations`` subcommand — shim over ``rpc``.

    Translates the legacy ``--checksums`` string format into path-keyed
    entries at this boundary so verification internals see exactly one
    format.
    """
    tokens: list[str] = args.checksums or []
    entries: list[dict[str, str]] = []
    malformed_paths: set[str] = set()
    if tokens:
        try:
            staging_dir = _validate_echoed_staging_dir(
                args.staging_dir, Path(args.staging_root)
            )
        except ValueError as exc:
            _write_error('invalid_staging_dir', str(exc))
            sys.exit(1)
        entries, malformed_paths = _translate_legacy_checksums(tokens, staging_dir)
    params: dict[str, Any] = {
        'staging_dir': args.staging_dir,
        'annotator_id': args.annotator_id,
        'machine_id': args.machine_id,
        'nano_id': args.nano_id,
        'checksums': entries,
        'expected_ontology': list(args.expected_ontology or []),
        'unconstrained': bool(args.unconstrained),
        'force': bool(args.force),
    }
    _dispatch(
        'integrate-annotations',
        params,
        args,
        overrides={'malformed_checksum_paths': malformed_paths},
    )


def _shim_cleanup(args: argparse.Namespace) -> None:
    """Deprecated ``cleanup`` subcommand — shim over ``rpc``."""
    _dispatch('cleanup', {'staging_dir': args.staging_dir}, args)


def _shim_healthcheck(args: argparse.Namespace) -> None:
    """Deprecated ``healthcheck`` subcommand — shim over ``rpc``."""
    _dispatch('healthcheck', {}, args)


# -- Parser ------------------------------------------------------------------


def main() -> None:
    """Entry point for the ``voxhub-server`` CLI."""
    try:
        settings = load_settings()
    except SettingsError as exc:
        _write_error('storage_misconfigured', str(exc))
        sys.exit(1)
    configure_logging(settings.logging)

    parser = argparse.ArgumentParser(
        prog='voxhub-server',
        description='voxhub server (SSH-invoked)',
    )
    subparsers = parser.add_subparsers(dest='command')

    # rpc — the annotator-facing transport (wire protocol v3).
    rpc = subparsers.add_parser(
        'rpc',
        help='Serve one JSON request from stdin (wire protocol v3).',
        description=(
            'Read a single JSON object {"protocol_version": 3, "method": '
            '<name>, "params": {...}} from stdin (to EOF), dispatch, and '
            'write a single JSON response to stdout.'
        ),
    )
    rpc.set_defaults(func=_run_rpc)

    # list-stores (deprecated shim)
    ls = subparsers.add_parser(
        'list-stores',
        help=f'[deprecated] List zarr stores. {_DEPRECATION_NOTE}',
        description=_DEPRECATION_NOTE,
    )
    ls.add_argument(
        '--if-version',
        type=int,
        default=None,
        help=(
            'Client cache hint: if the server still serves this catalog '
            'version, reply with {"unchanged": true} instead of the full '
            'payload. Optimisation only -- omitting it always returns the '
            'full catalog.'
        ),
    )
    ls.set_defaults(func=_shim_list_stores)

    # prepare-pull (single-store, deprecated shim)
    # Note: ``--staging-dir`` used to be a client-controllable override but
    # was removed for security — the server is now authoritative over the
    # staging path.  Operators redirect staging via ``[storage].staging_dir``
    # in server.toml.
    pp = subparsers.add_parser(
        'prepare-pull',
        help=f'[deprecated] Stage a store for pull. {_DEPRECATION_NOTE}',
        description=_DEPRECATION_NOTE,
    )
    pp.add_argument('--store', required=True, help='Store name to pull.')
    pp.add_argument('--include-existing-annotations', nargs='*')
    pp.add_argument('--compress', action='store_true')
    pp.add_argument(
        '--annotator-id',
        default=None,
        help=(
            'Optional annotator identity for local/dev use. Over SSH the '
            'key-bound VOXHUB_ANNOTATOR env var is authoritative and '
            'overrides this flag; a disagreement fails the request.'
        ),
    )
    pp.set_defaults(func=_shim_prepare_pull)

    # integrate-annotations (deprecated shim)
    ia = subparsers.add_parser(
        'integrate-annotations',
        help=f'[deprecated] Integrate pushed annotations. {_DEPRECATION_NOTE}',
        description=_DEPRECATION_NOTE,
    )
    ia.add_argument('staging_dir')
    ia.add_argument('--annotator-id', required=True)
    ia.add_argument('--machine-id', required=True)
    ia.add_argument('--nano-id', required=True)
    ia.add_argument('--checksums', nargs='*')
    ia.add_argument('--force', action='store_true')
    ia.add_argument(
        '--expected-ontology',
        action='append',
        default=[],
        metavar='NAME',
        help=(
            'Ontology name the client declares for this integration. '
            'Repeatable for multi-ontology sessions. Mutually exclusive '
            'with --unconstrained; exactly one must be specified.'
        ),
    )
    ia.add_argument(
        '--unconstrained',
        action='store_true',
        help=(
            'Explicit opt-in to unconstrained integration (no ontology '
            'enforcement). Mutually exclusive with --expected-ontology; '
            'exactly one must be specified.'
        ),
    )
    ia.set_defaults(func=_shim_integrate_annotations)

    # cleanup (deprecated shim)
    cl = subparsers.add_parser(
        'cleanup',
        help=f'[deprecated] Remove a staging dir. {_DEPRECATION_NOTE}',
        description=_DEPRECATION_NOTE,
    )
    cl.add_argument('staging_dir')
    cl.set_defaults(func=_shim_cleanup)

    # gc
    gc = subparsers.add_parser('gc')
    gc.add_argument('--ttl-hours', type=float, default=24.0)
    gc.set_defaults(func=_run_gc)

    # validate-attributes
    va = subparsers.add_parser('validate-attributes')
    va.add_argument('--stores', nargs='*')
    va.set_defaults(func=_run_validate_attributes)

    # healthcheck (deprecated shim)
    hc = subparsers.add_parser(
        'healthcheck',
        help=f'[deprecated] Server self-check. {_DEPRECATION_NOTE}',
        description=_DEPRECATION_NOTE,
    )
    hc.set_defaults(func=_shim_healthcheck)

    # catalog
    cat = subparsers.add_parser(
        'catalog',
        help='Inspect and refresh the list-stores catalog cache.',
    )
    cat_sub = cat.add_subparsers(dest='catalog_action')

    cat_refresh = cat_sub.add_parser(
        'refresh',
        help='Rebuild the catalog cache, or re-probe a single store.',
    )
    cat_refresh.add_argument(
        '--store',
        default=None,
        help='Name of a single store to re-probe (without .zarr suffix).',
    )
    cat_refresh.set_defaults(func=_run_catalog_refresh)

    cat_show = cat_sub.add_parser(
        'show',
        help='Pretty-print the current catalog snapshot.',
    )
    cat_show.set_defaults(func=_run_catalog_show)

    cat_stats = cat_sub.add_parser(
        'stats',
        help='Report cache file age, fingerprint match, and store count.',
    )
    cat_stats.set_defaults(func=_run_catalog_stats)

    # `voxhub-server catalog` with no action → print help for the catalog
    # subparser and exit cleanly, mirroring the top-level behaviour below.
    def _catalog_help(_args: argparse.Namespace) -> None:
        cat.print_help()

    cat.set_defaults(func=_catalog_help, catalog_action=None)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    args.stores_dir = str(settings.storage.stores_dir)
    args.staging_root = str(settings.storage.staging_dir)
    args.memory_settings = settings.memory

    try:
        args.func(args)
    except Exception as exc:
        log = get_logger(command=args.command)
        log.error('unhandled_exception', error=str(exc), exc_info=True)
        _write_error('internal_error', str(exc))
        sys.exit(1)


if __name__ == '__main__':
    main()

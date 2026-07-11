"""SSH-invoked server CLI.

Commands: list-stores, prepare-pull, integrate-annotations, cleanup, gc,
validate-attributes, healthcheck.

Every command writes a single JSON object to stdout and exits.
Structured errors use the ``ServerError`` envelope.  Logs go to
stderr via structlog.
"""

import argparse
import json
import os
import shutil
import socket
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
    validate_landmarks,
    validate_segmentation,
    write_landmarks_to_zarr,
    write_segmentation_to_zarr,
)
from voxhub_core.memory_budget import (
    MemoryBudget,
    MemoryBudgetError,
    MemoryWarning,
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
from voxhub_schema import (
    PROTOCOL_VERSION,
    AnnotatorSlugError,
    IssueRecord,
    ManifestError,
    Ontology,
    PullAnnotationEntry,
    PullManifest,
    ServerError,
    generate_nano_id,
    load_ontology,
    parse_annotator_slug,
    serialize,
)

STAGING_DIR_PREFIX: str = 'vxhb-staging-'

# Segmentation extraction holds an extra transient copy compared to the raw
# volume path (label_map + pynrrd internal buffer + astype).  Bump the
# safety factor locally so the low-memory check reflects that worst case.
_SEGMENTATION_SAFETY_BUMP: float = 1.5


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
        _write_error('insufficient_memory', str(exc))
        sys.exit(1)
    except Exception as exc:
        log.error('prepare_pull_failed', error=str(exc))
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

    _write_dict(
        {
            'protocol_version': PROTOCOL_VERSION,
            'staging_dir': str(staging_dir),
            'server_host': socket.getfqdn(),
            'server_stores_dir': str(stores_dir),
            'store_name': store_name,
            'raw_name': 'raw.nrrd',
            'raw_checksum': meta['raw_checksum'],
            'shape': meta['shape'],
            'spacing_mm': meta['spacing_mm'],
            'origin_lps': meta['origin_lps'],
            'space_directions': meta['space_directions'],
            'skipped_annotations': skipped_annotations,
            'memory_warnings': [w.to_dict() for w in memory_warnings],
        }
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
    checksums = args.checksums or []
    declared_ontologies: list[str] = list(args.expected_ontology or [])
    unconstrained: bool = bool(args.unconstrained)

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

    # Parse expected checksums.
    expected_checksums: dict[str, str] = {}
    for entry in checksums:
        parts = entry.split(':', 2)
        if len(parts) == 3:
            filename = parts[0]
            checksum = f'{parts[1]}:{parts[2]}'
            expected_checksums[filename] = checksum

    date_str = datetime.now(UTC).strftime('%Y%m%d')
    annotator_dir = f'{annotator_id}-{nano_id}'
    pull_session_id = staging_dir.name

    stores_result: dict[str, dict[str, Any]] = {}

    for store_dir in sorted(staging_dir.iterdir()):
        if not store_dir.is_dir() or store_dir.name.startswith('.'):
            continue

        store_name = store_dir.name
        zarr_path = stores_dir / f'{store_name}.zarr'

        if not zarr_path.is_dir():
            continue

        seg_file, lmk_file = find_annotation_files(store_dir)
        if seg_file is None and lmk_file is None:
            continue

        # Verify checksums.
        if expected_checksums:
            for ann_file in [seg_file, lmk_file]:
                if ann_file is None:
                    continue
                actual = compute_sha256(ann_file)
                expected = expected_checksums.get(ann_file.name)
                if expected and actual != expected:
                    msg = (
                        f'Checksum mismatch for {ann_file.name}: '
                        f'expected {expected}, got {actual}'
                    )
                    log.error(
                        'checksum_mismatch',
                        store=store_name,
                        file=ann_file.name,
                    )
                    _write_error('checksum_mismatch', msg)
                    sys.exit(1)

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
                    seg_ontology = seg_ontologies[0] if seg_ontologies else None
                    try:
                        seg_data = parse_seg_nrrd(seg_file)
                        seg_issues = validate_segmentation(
                            seg_data,
                            manifest_entry,
                            ontology=seg_ontology,
                        )
                        issues.extend(seg_issues)

                        errors = [i for i in seg_issues if i.severity == 'error']
                        if errors and not force:
                            log.warning(
                                'seg_validation_errors',
                                store=store_name,
                                errors=[i.message for i in errors],
                            )
                        else:
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
                            )

                            seg_checksum = compute_sha256(seg_file)
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
                                issues=[i for i in seg_issues if i.severity == 'warning'],
                            )

                            annotations_written.append(
                                {
                                    'path': seg_path,
                                    'ontology': ont_name,
                                    'ontology_version': ont_version,
                                }
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
                    lmk_ontology = lmk_ontologies[0] if lmk_ontologies else None
                    try:
                        lmk_data = parse_mrk_json(lmk_file)
                        lmk_issues = validate_landmarks(
                            lmk_data,
                            manifest_entry,
                            ontology=lmk_ontology,
                        )
                        issues.extend(lmk_issues)

                        errors = [i for i in lmk_issues if i.severity == 'error']
                        if errors and not force:
                            log.warning(
                                'lmk_validation_errors',
                                store=store_name,
                                errors=[i.message for i in errors],
                            )
                        else:
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
                            )

                            lmk_checksum = compute_sha256(lmk_file)
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
                                issues=[i for i in lmk_issues if i.severity == 'warning'],
                            )

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

        stores_result[store_name] = {
            'status': ('integrated' if annotations_written else 'failed'),
            'annotations': annotations_written,
            'issues': [{'severity': i.severity, 'message': i.message} for i in issues],
        }

    duration = time.monotonic() - t0
    log.info(
        'integrate_completed',
        stores=list(stores_result.keys()),
        duration_s=round(duration, 3),
    )

    _write_dict(
        {
            'protocol_version': PROTOCOL_VERSION,
            'stores': stores_result,
        }
    )


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

    _write_dict(
        {
            'protocol_version': PROTOCOL_VERSION,
            'status': 'ok',
        }
    )


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

    # list-stores
    ls = subparsers.add_parser('list-stores')
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
    ls.set_defaults(func=_run_list_stores)

    # prepare-pull (single-store)
    # Note: ``--staging-dir`` used to be a client-controllable override but
    # was removed for security — the server is now authoritative over the
    # staging path.  Operators redirect staging via ``[storage].staging_dir``
    # in server.toml.
    pp = subparsers.add_parser('prepare-pull')
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
    pp.set_defaults(func=_run_prepare_pull)

    # integrate-annotations
    ia = subparsers.add_parser('integrate-annotations')
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
    ia.set_defaults(func=_run_integrate_annotations)

    # cleanup
    cl = subparsers.add_parser('cleanup')
    cl.add_argument('staging_dir')
    cl.set_defaults(func=_run_cleanup)

    # gc
    gc = subparsers.add_parser('gc')
    gc.add_argument('--ttl-hours', type=float, default=24.0)
    gc.set_defaults(func=_run_gc)

    # validate-attributes
    va = subparsers.add_parser('validate-attributes')
    va.add_argument('--stores', nargs='*')
    va.set_defaults(func=_run_validate_attributes)

    # healthcheck
    hc = subparsers.add_parser('healthcheck')
    hc.set_defaults(func=_run_healthcheck)

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

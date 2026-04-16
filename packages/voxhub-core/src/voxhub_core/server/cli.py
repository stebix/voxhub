"""SSH-invoked server CLI.

Commands: list-stores, prepare-pull, integrate-annotations, cleanup, gc,
validate-attributes, healthcheck.

Every command writes a single JSON object to stdout and exits.
Structured errors use the ``ServerError`` envelope.  Logs go to
stderr via structlog.
"""

import argparse
import hashlib
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
    DATASET_ATTRIBUTES_KEY,
    get_dataset_attributes,
    validate_dataset_attributes,
)
from voxhub_core.catalog import discover_zarr_stores
from voxhub_core.extraction import (
    ExtractionError,
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
from voxhub_core.server.locks import store_lock
from voxhub_core.server.logging import configure_logging, get_logger
from voxhub_core.server.provenance import (
    record_provenance,
    validate_provenance_jsonl,
)
from voxhub_core.server.settings import SettingsError, load_settings
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


def _compute_sha256(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return f'sha256:{h.hexdigest()}'


# -- list-stores -------------------------------------------------------------


def _run_list_stores(args: argparse.Namespace) -> None:
    log = get_logger(command='list-stores')
    t0 = time.monotonic()

    stores_dir = Path(args.stores_dir)
    log.info('list_stores_started', stores_dir=str(stores_dir))

    entries = discover_zarr_stores(stores_dir)

    stores: list[dict[str, Any]] = []
    for entry in entries:
        store_name = entry.path.name.removesuffix('.zarr')
        annotations = [
            {
                'path': a.path,
                'ontology': a.ontology,
                'ontology_version': a.ontology_version,
                'annotator_id': a.annotator_id,
                'integrated_at': a.integrated_at,
            }
            for a in entry.annotations
        ]

        if entry.error:
            stores.append(
                {
                    'name': store_name,
                    'shape': [],
                    'dtype': '',
                    'origin_lps': [],
                    'spacing_mm': [],
                    'space_directions': [],
                    'annotations': [],
                    'error': entry.error,
                    'dataset_attributes': None,
                }
            )
            continue

        root = zarr.open_group(entry.path, mode='r')
        arr = root['raw']['full']
        a = dict(arr.attrs)

        try:
            origin, space_directions, spacing_mm = extract_spatial_metadata(a)
        except KeyError:
            stores.append(
                {
                    'name': store_name,
                    'shape': list(entry.shape or []),
                    'dtype': entry.dtype or '',
                    'origin_lps': [],
                    'spacing_mm': [],
                    'space_directions': [],
                    'annotations': annotations,
                    'error': 'Missing spatial metadata',
                    'dataset_attributes': None,
                }
            )
            continue

        da_raw = dict(root.attrs).get(DATASET_ATTRIBUTES_KEY)
        stores.append(
            {
                'name': store_name,
                'shape': list(arr.shape),
                'dtype': str(arr.dtype),
                'origin_lps': origin.tolist(),
                'spacing_mm': spacing_mm,
                'space_directions': space_directions.tolist(),
                'annotations': annotations,
                'error': None,
                'dataset_attributes': da_raw,
            }
        )

    duration = time.monotonic() - t0
    log.info(
        'list_stores_completed',
        store_count=len(stores),
        duration_s=round(duration, 3),
    )

    _write_dict(
        {
            'protocol_version': PROTOCOL_VERSION,
            'stores': stores,
        }
    )


# -- prepare-pull ------------------------------------------------------------


def _extract_reference_annotations(
    zarr_path: Path,
    staging_dir: Path,
    include_annotations: list[str],
    log: Any,
) -> tuple[list[PullAnnotationEntry], list[dict[str, str]]]:
    """Extract requested annotations to ``<staging_dir>/reference/``.

    Returns ``(manifest_entries, skipped)``.  Failures on individual
    annotations are non-fatal: they are appended to ``skipped`` with a
    human-readable reason and the remaining annotations continue.
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
                checksum = extract_segmentation(zarr_path, array_zarr_path, ref_dest)
            else:
                checksum = extract_landmarks(zarr_path, array_zarr_path, ref_dest)
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
    store_name: str = args.store
    compress: bool = args.compress
    include_annotations: list[str] = args.include_existing_annotations or []

    log.info(
        'prepare_pull_started',
        stores_dir=str(stores_dir),
        store=store_name,
        include_annotations=include_annotations,
    )

    # -- Task 1: upfront store validation (before mkdtemp) ------------------
    zarr_path = stores_dir / f'{store_name}.zarr'
    if not zarr_path.is_dir():
        log.error('store_not_found', store=store_name, stores_dir=str(stores_dir))
        _write_error('store_not_found', f'Store not found: {store_name!r}')
        sys.exit(1)

    # Create a temp dir for staging.
    session_id = generate_nano_id()
    if args.staging_dir:
        staging_dir = Path(args.staging_dir)
        staging_dir.mkdir(parents=True, exist_ok=True)
    else:
        staging_dir = Path(
            tempfile.mkdtemp(
                prefix=f'{STAGING_DIR_PREFIX}{session_id}-',
            )
        )

    # Sanitise prior prepare-pull output under ``--staging-dir``:
    # ``reference/`` files from an earlier run are not touched by the
    # extraction step and would otherwise rsync down as orphans that
    # are absent from the fresh manifest.  ``raw.nrrd`` and the prior
    # ``.voxhub_pull.json`` get overwritten downstream, but we clear
    # them explicitly so the session dir is in a known state before
    # we write anything.  All removals use ``missing_ok``-style
    # semantics.
    ref_dir = staging_dir / 'reference'
    if ref_dir.exists():
        shutil.rmtree(ref_dir)
    (staging_dir / 'raw.nrrd').unlink(missing_ok=True)
    (staging_dir / '.voxhub_pull.json').unlink(missing_ok=True)

    # -- Task 2a: extract raw volume at <staging_dir>/raw.nrrd --------------
    try:
        meta = extract_volume(zarr_path, staging_dir / 'raw.nrrd', compress=compress)
    except Exception as exc:
        log.error('prepare_pull_failed', error=str(exc))
        _write_error('prepare_pull_failed', str(exc))
        sys.exit(1)

    # -- Task 2b: extract reference annotations -----------------------------
    ann_entries, skipped_annotations = _extract_reference_annotations(
        zarr_path, staging_dir, include_annotations, log
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
        duration_s=round(duration, 3),
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
    staging_dir = Path(args.staging_dir)
    annotator_id = args.annotator_id
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
                actual = _compute_sha256(ann_file)
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

                            seg_checksum = _compute_sha256(seg_file)
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

                            lmk_checksum = _compute_sha256(lmk_file)
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
    staging_dir = Path(args.staging_dir)

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

    log.info('gc_started', ttl_hours=ttl_hours)

    tmp_root = Path(tempfile.gettempdir())
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
    ls.set_defaults(func=_run_list_stores)

    # prepare-pull (single-store)
    pp = subparsers.add_parser('prepare-pull')
    pp.add_argument('--store', required=True, help='Store name to pull.')
    pp.add_argument('--staging-dir', default=None)
    pp.add_argument('--include-existing-annotations', nargs='*')
    pp.add_argument('--compress', action='store_true')
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

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    args.stores_dir = str(settings.storage.stores_dir)

    try:
        args.func(args)
    except Exception as exc:
        log = get_logger(command=args.command)
        log.error('unhandled_exception', error=str(exc), exc_info=True)
        _write_error('internal_error', str(exc))
        sys.exit(1)


if __name__ == '__main__':
    main()

"""SSH-invoked server CLI.

Commands: list-stores, prepare-pull, integrate-annotations, cleanup, gc,
healthcheck.

Every command writes a single JSON object to stdout and exits.
Structured errors use the ``ServerError`` envelope.  Logs go to
stderr via structlog.
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import zarr
from rich.console import Console

from voxhub_core.catalog import discover_zarr_stores
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
from voxhub_core.server.settings import load_settings
from voxhub_core.staging import extract_spatial_metadata, stage
from voxhub_schema import (
    PROTOCOL_VERSION,
    IssueRecord,
    ServerError,
    generate_nano_id,
    load_ontology,
    serialize,
)


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

    zarr_root = Path(args.zarr_root)
    log.info('list_stores_started', zarr_root=str(zarr_root))

    entries = discover_zarr_stores(zarr_root)

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
                }
            )
            continue

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


def _run_prepare_pull(args: argparse.Namespace) -> None:
    log = get_logger(command='prepare-pull')
    t0 = time.monotonic()

    zarr_root = Path(args.zarr_root)
    store_names = args.stores
    ontologies = args.ontologies or []
    compress = args.compress
    include_annotations = args.include_existing_annotations or []

    log.info(
        'prepare_pull_started',
        zarr_root=str(zarr_root),
        stores=store_names,
        ontologies=ontologies,
    )

    # Create a temp dir for staging.
    session_id = generate_nano_id()
    if args.wip_dir:
        wip_dir = Path(args.wip_dir)
    else:
        wip_dir = Path(
            tempfile.mkdtemp(
                prefix=f'dt-pull-{session_id}-',
            )
        )

    try:
        console = Console(stderr=True, quiet=True)
        store_metadata = stage(
            zarr_root,
            wip_dir,
            store_names=store_names,
            compress=compress,
            force=True,
            console=console,
        )
    except Exception as exc:
        log.error('prepare_pull_failed', error=str(exc))
        _write_error('prepare_pull_failed', str(exc))
        sys.exit(1)

    # Copy existing annotations if requested.
    for ann_path in include_annotations:
        for store_name in store_metadata:
            zarr_path = zarr_root / f'{store_name}.zarr'
            src = zarr_path / ann_path
            if src.exists():
                dst = wip_dir / store_name / ann_path
                dst.parent.mkdir(parents=True, exist_ok=True)
                if src.is_dir():
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, dst)

    # Build response.
    stores_response: dict[str, dict[str, Any]] = {}
    for store_name, meta in store_metadata.items():
        stores_response[store_name] = {
            'raw_checksum': meta['raw_checksum'],
            'shape': meta['shape'],
            'spacing_mm': meta['spacing_mm'],
            'origin_lps': meta['origin_lps'],
            'space_directions': meta['space_directions'],
            'expected_ontologies': ontologies,
            'included_annotations': include_annotations,
        }

    duration = time.monotonic() - t0
    log.info(
        'prepare_pull_completed',
        stores=list(store_metadata.keys()),
        wip_dir=str(wip_dir),
        duration_s=round(duration, 3),
    )

    _write_dict(
        {
            'protocol_version': PROTOCOL_VERSION,
            'wip_dir': str(wip_dir),
            'stores': stores_response,
        }
    )


# -- integrate-annotations ---------------------------------------------------


def _run_integrate_annotations(args: argparse.Namespace) -> None:
    log = get_logger(command='integrate-annotations')
    t0 = time.monotonic()

    zarr_root = Path(args.zarr_root)
    wip_dir = Path(args.wip_dir)
    annotator_id = args.annotator_id
    machine_id = args.machine_id
    nano_id = args.nano_id
    force = args.force
    checksums = args.checksums or []

    log.info(
        'integrate_started',
        zarr_root=str(zarr_root),
        wip_dir=str(wip_dir),
        annotator_id=annotator_id,
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

    stores_result: dict[str, dict[str, Any]] = {}

    for store_dir in sorted(wip_dir.iterdir()):
        if not store_dir.is_dir() or store_dir.name.startswith('.'):
            continue

        store_name = store_dir.name
        zarr_path = zarr_root / f'{store_name}.zarr'

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

        issues: list[IssueRecord] = []
        annotations_written: list[dict[str, Any]] = []

        with store_lock(zarr_path):
            # Integrate segmentation.
            if seg_file is not None:
                try:
                    seg_data = parse_seg_nrrd(seg_file)
                    seg_issues = validate_segmentation(seg_data, manifest_entry)
                    issues.extend(seg_issues)

                    errors = [i for i in seg_issues if i.severity == 'error']
                    if errors and not force:
                        log.warning(
                            'seg_validation_errors',
                            store=store_name,
                            errors=[i.message for i in errors],
                        )
                    else:
                        # Determine ontology from segments or manifest.
                        ont_name = 'unconstrained'
                        ont_version = 1
                        ontology = None
                        try:
                            # Try to infer ontology from file metadata
                            # or use default.
                            ontology = load_ontology(ont_name)
                            ont_version = ontology.version
                        except FileNotFoundError:
                            pass

                        short_random = generate_nano_id(size=4)
                        instance_dir = f'{ont_name}-{date_str}-{short_random}'
                        seg_path = f'annotations/{annotator_dir}/{instance_dir}/data'

                        write_segmentation_to_zarr(
                            zarr_path,
                            seg_data,
                            seg_path,
                            ontology=ontology,
                            force=force,
                        )

                        seg_checksum = _compute_sha256(seg_file)
                        record_provenance(
                            zarr_root,
                            store_name,
                            seg_path,
                            annotator_id=annotator_id,
                            machine_id=machine_id,
                            nano_id=nano_id,
                            pull_session_id=wip_dir.name,
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
                            message=f'Segmentation integration failed: {exc}',
                        )
                    )
                    log.error(
                        'seg_integrate_failed',
                        store=store_name,
                        error=str(exc),
                    )

            # Integrate landmarks.
            if lmk_file is not None:
                try:
                    lmk_data = parse_mrk_json(lmk_file)
                    lmk_issues = validate_landmarks(lmk_data, manifest_entry)
                    issues.extend(lmk_issues)

                    errors = [i for i in lmk_issues if i.severity == 'error']
                    if errors and not force:
                        log.warning(
                            'lmk_validation_errors',
                            store=store_name,
                            errors=[i.message for i in errors],
                        )
                    else:
                        ont_name = 'landmarks'
                        ont_version = 1

                        short_random = generate_nano_id(size=4)
                        instance_dir = f'{ont_name}-{date_str}-{short_random}'
                        lmk_path = f'annotations/{annotator_dir}/{instance_dir}/data'

                        write_landmarks_to_zarr(
                            zarr_path,
                            lmk_data,
                            lmk_path,
                            force=force,
                        )

                        lmk_checksum = _compute_sha256(lmk_file)
                        record_provenance(
                            zarr_root,
                            store_name,
                            lmk_path,
                            annotator_id=annotator_id,
                            machine_id=machine_id,
                            nano_id=nano_id,
                            pull_session_id=wip_dir.name,
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


def _run_cleanup(args: argparse.Namespace) -> None:
    log = get_logger(command='cleanup')
    wip_dir = Path(args.wip_dir)

    log.info('cleanup_started', wip_dir=str(wip_dir))

    if wip_dir.is_dir():
        shutil.rmtree(wip_dir)
        log.info('cleanup_completed', wip_dir=str(wip_dir))
    else:
        log.warning('cleanup_not_found', wip_dir=str(wip_dir))

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
        if not entry.name.startswith('dt-'):
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(str(entry))
            log.info('gc_removed', path=str(entry))

    log.info('gc_completed', removed_count=len(removed))

    _write_dict(
        {
            'protocol_version': PROTOCOL_VERSION,
            'removed': removed,
            'count': len(removed),
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


def _check_zarr_root(zarr_root: Path) -> dict[str, str]:
    """Check that the zarr root directory exists and is writable."""
    if not zarr_root.is_dir():
        return {
            'name': 'zarr_root',
            'status': 'fail',
            'detail': f'{zarr_root} is not a directory',
        }
    if not os.access(zarr_root, os.R_OK | os.W_OK):
        return {
            'name': 'zarr_root',
            'status': 'fail',
            'detail': f'{zarr_root} is not readable/writable',
        }
    return {'name': 'zarr_root', 'status': 'ok', 'detail': str(zarr_root)}


def _check_stores(zarr_root: Path) -> dict[str, str]:
    """Discover zarr stores and report their status."""
    entries = discover_zarr_stores(zarr_root)
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


def _check_provenance(zarr_root: Path) -> dict[str, str]:
    """Validate all provenance JSONL files under the zarr root."""
    meta_dir = zarr_root / '.meta'
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

    zarr_root = Path(args.zarr_root)
    log.info('healthcheck_started', zarr_root=str(zarr_root))

    checks = [
        _check_python_version(),
        _check_packages(),
        _check_rsync(),
        _check_zarr_root(zarr_root),
    ]

    # Only run store/provenance checks if zarr_root is accessible.
    if checks[-1]['status'] == 'ok':
        checks.append(_check_stores(zarr_root))
        checks.append(_check_provenance(zarr_root))

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
    settings = load_settings()
    configure_logging(settings.logging)

    parser = argparse.ArgumentParser(
        prog='voxhub-server',
        description='voxhub server (SSH-invoked)',
    )
    subparsers = parser.add_subparsers(dest='command')

    # list-stores
    ls = subparsers.add_parser('list-stores')
    ls.add_argument('zarr_root')
    ls.set_defaults(func=_run_list_stores)

    # prepare-pull
    pp = subparsers.add_parser('prepare-pull')
    pp.add_argument('zarr_root')
    pp.add_argument('--stores', nargs='*')
    pp.add_argument('--ontologies', nargs='*')
    pp.add_argument('--wip-dir', default=None)
    pp.add_argument('--include-existing-annotations', nargs='*')
    pp.add_argument('--compress', action='store_true')
    pp.set_defaults(func=_run_prepare_pull)

    # integrate-annotations
    ia = subparsers.add_parser('integrate-annotations')
    ia.add_argument('zarr_root')
    ia.add_argument('wip_dir')
    ia.add_argument('--annotator-id', required=True)
    ia.add_argument('--machine-id', required=True)
    ia.add_argument('--nano-id', required=True)
    ia.add_argument('--checksums', nargs='*')
    ia.add_argument('--force', action='store_true')
    ia.set_defaults(func=_run_integrate_annotations)

    # cleanup
    cl = subparsers.add_parser('cleanup')
    cl.add_argument('wip_dir')
    cl.set_defaults(func=_run_cleanup)

    # gc
    gc = subparsers.add_parser('gc')
    gc.add_argument('--ttl-hours', type=float, default=24.0)
    gc.set_defaults(func=_run_gc)

    # healthcheck
    hc = subparsers.add_parser('healthcheck')
    hc.add_argument('zarr_root')
    hc.set_defaults(func=_run_healthcheck)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    try:
        args.func(args)
    except Exception as exc:
        log = get_logger(command=args.command)
        log.error('unhandled_exception', error=str(exc), exc_info=True)
        _write_error('internal_error', str(exc))
        sys.exit(1)

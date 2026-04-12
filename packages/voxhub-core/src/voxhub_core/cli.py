"""Local CLI for voxhub-core.

Commands: export, catalog, stage, integrate, audit, emit-nrrd.
"""

import argparse
import sys
from pathlib import Path

from voxhub_core.audit import audit
from voxhub_core.catalog import catalog
from voxhub_core.dicom import actualize, parse_dicom_tree
from voxhub_core.export import (
    export_zarr_collection,
    export_zarr_collection_parallel,
    flatten_to_volumes,
)
from voxhub_core.integrate import integrate
from voxhub_core.staging import stage
from voxhub_schema import load_ontology


def _build_export_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    parser = subparsers.add_parser(
        'export',
        help='Export a DICOM directory tree to zarr stores.',
    )
    parser.add_argument(
        'input_dir',
        type=Path,
        help='Root DICOM directory.',
    )
    parser.add_argument(
        'output_dir',
        type=Path,
        help='Output directory for zarr stores.',
    )
    parser.add_argument(
        '--seed',
        type=int,
        default=None,
        help='RNG seed for reproducible name generation.',
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='Overwrite existing output files.',
    )
    parser.add_argument(
        '--workers',
        type=int,
        default=None,
        help='Max parallel workers.',
    )
    parser.add_argument(
        '--no-parallel',
        action='store_true',
        help='Disable parallel export.',
    )
    parser.add_argument(
        '--verbose',
        action='store_true',
        help='Verbose output.',
    )
    parser.set_defaults(func=_run_export)


def _build_catalog_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    parser = subparsers.add_parser(
        'catalog',
        help='Discover and display zarr stores.',
    )
    parser.add_argument(
        'root',
        type=Path,
        help='Directory to search for .zarr stores.',
    )
    parser.add_argument(
        '--table',
        action='store_true',
        help='Also print a summary table.',
    )
    parser.set_defaults(func=_run_catalog)


def _build_stage_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    parser = subparsers.add_parser(
        'stage',
        help='Export zarr volumes as NRRD for annotation.',
    )
    parser.add_argument(
        'zarr_root',
        type=Path,
        help='Directory containing .zarr stores.',
    )
    parser.add_argument(
        'staging_dir',
        type=Path,
        help='Target staging directory.',
    )
    parser.add_argument(
        '--stores',
        nargs='*',
        default=None,
        help='Specific store names to stage.',
    )
    parser.add_argument(
        '--compress',
        action='store_true',
        help='Gzip compress NRRD files.',
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='Overwrite existing staging directory.',
    )
    parser.set_defaults(func=_run_stage)


def _build_integrate_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    parser = subparsers.add_parser(
        'integrate',
        help='Integrate annotations from staging directory into zarr.',
    )
    parser.add_argument(
        'staging_dir',
        type=Path,
        help='staging directory with annotations.',
    )
    parser.add_argument(
        'zarr_root',
        type=Path,
        help='Directory containing .zarr stores.',
    )
    parser.add_argument(
        '--annotator-id',
        required=True,
        help='Annotator identifier.',
    )
    parser.add_argument(
        '--nano-id',
        required=True,
        help='8-char nano-ID for the annotator.',
    )
    parser.add_argument(
        '--ontology',
        default=None,
        help='Ontology name to validate against.',
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='Overwrite existing annotations.',
    )
    parser.add_argument(
        '--validate-only',
        action='store_true',
        help='Only validate, do not write.',
    )
    parser.set_defaults(func=_run_integrate)


def _build_audit_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    parser = subparsers.add_parser(
        'audit',
        help='Audit cross-store annotation coherence.',
    )
    parser.add_argument(
        'zarr_root',
        type=Path,
        help='Directory containing .zarr stores.',
    )
    parser.add_argument(
        '--ontology',
        default=None,
        help='Filter by ontology name.',
    )
    parser.add_argument(
        '--stores',
        nargs='*',
        default=None,
        help='Specific store names to audit.',
    )
    parser.set_defaults(func=_run_audit)


def _run_export(args: argparse.Namespace) -> None:
    try:
        if args.no_parallel:
            tree = parse_dicom_tree(args.input_dir, verbose=args.verbose)
            atree = actualize(tree, max_workers=args.workers)
            volumes = flatten_to_volumes(atree)
            mapping = export_zarr_collection(
                volumes,
                args.output_dir,
                seed=args.seed,
                force_write=args.force,
            )
        else:
            tree = parse_dicom_tree(args.input_dir, verbose=args.verbose)
            mapping = export_zarr_collection_parallel(
                tree,
                args.output_dir,
                seed=args.seed,
                force_write=args.force,
                max_workers=args.workers,
                verbose=args.verbose,
            )
        print(f'Exported {len(mapping)} stores.')
    except (FileExistsError, FileNotFoundError, ValueError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        sys.exit(1)


def _run_catalog(args: argparse.Namespace) -> None:
    catalog(args.root, show_table=args.table)


def _run_stage(args: argparse.Namespace) -> None:
    try:
        stage(
            args.zarr_root,
            args.staging_dir,
            store_names=args.stores,
            compress=args.compress,
            force=args.force,
        )
    except (FileExistsError, FileNotFoundError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        sys.exit(1)


def _run_integrate(args: argparse.Namespace) -> None:
    ontology = None
    if args.ontology:
        ontology = load_ontology(args.ontology)

    try:
        integrate(
            args.staging_dir,
            args.zarr_root,
            annotator_id=args.annotator_id,
            nano_id=args.nano_id,
            ontology=ontology,
            force=args.force,
            validate_only=args.validate_only,
        )
    except (RuntimeError, FileNotFoundError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        sys.exit(1)


def _run_audit(args: argparse.Namespace) -> None:
    issues = audit(
        args.zarr_root,
        ontology_filter=args.ontology,
        store_names=args.stores,
    )
    if any(i.severity == 'error' for i in issues):
        sys.exit(1)


def main() -> None:
    """Entry point for the ``voxhub`` local CLI."""
    parser = argparse.ArgumentParser(
        prog='voxhub',
        description='voxhub: collaborative volumetric annotation',
    )
    subparsers = parser.add_subparsers(dest='command')

    _build_export_parser(subparsers)
    _build_catalog_parser(subparsers)
    _build_stage_parser(subparsers)
    _build_integrate_parser(subparsers)
    _build_audit_parser(subparsers)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    args.func(args)

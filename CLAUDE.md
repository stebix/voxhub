# CLAUDE.md

## Project Overview

voxhub is a collaborative volumetric annotation system. Zarr stores live on a remote
server; annotators pull data locally, annotate in 3D Slicer, and push annotations back.
The server integrates annotations into zarr with full provenance. Transport is SSH+rsync.

Full architecture: `docs/architecture.md`

## Monorepo Structure

uv workspace with three packages:

- **voxhub-schema** (`packages/voxhub-schema/`): Protocol models, ontology definitions
  (versioned YAML), pre-flight validation. Dependencies: attrs, numpy, pynrrd.
- **voxhub-core** (`packages/voxhub-core/`): Domain library + server entrypoint. DICOM
  pipeline, zarr I/O, staging, integration, auditing. Dependencies: zarr, pydicom,
  structlog, + voxhub-schema.
- **voxhub-client** (`packages/voxhub-client/`): User-facing CLI for remote workflows.
  SSH/rsync transport, identity management. Dependencies: rich, + voxhub-schema.

Dependency DAG: `core -> schema <- client` (core and client never depend on each other).

## Commands

- **Install**: `uv sync --group dev`
- **Lint**: `uv run ruff check packages/`
- **Format**: `uv run ruff format packages/`
- **Type check**: `uv run pyright`
- **Test**: `uv run pytest`

## Key Architecture Rules

1. **Server/local hard wall**: Local modules in `voxhub_core/` (staging, integrate, etc.)
   must never take server-specific parameters (annotator_id, session_id, lock). Server
   wrappers in `voxhub_core/server/` call local functions then add provenance on top.

2. **Annotator-scoped storage**: Annotations live at
   `annotations/<annotator_id>-<nano_id>/<ontology>-<date>-<short_random>/` inside each
   zarr store. This enables concurrent multi-annotator writes without conflicts.

3. **Ontology enforcement**: Every annotation must declare and conform to a versioned
   ontology. Ontologies are YAML files shipped with voxhub-schema (versioned filenames:
   `inner-ear-structures-v1.yaml`). Validation happens client-side pre-push.

4. **Protocol versioning**: Every SSH server response includes `protocol_version`. Client
   errors on mismatch.

5. **Provenance**: Push events recorded in `.meta/provenance.jsonl` at the zarr root.
   Every annotation array carries full provenance metadata in zarr attrs.

6. **Local workflow parity**: The local CLI enforces the same strict metadata as the
   remote workflow (annotator ID, ontology required). `pull_session_id` uses `"local"`
   sentinel.

## Code Quality Standards

- Python 3.12+, uses `type` statement for type aliases
- **Formatting**: ruff format, single quotes, 90-char line length
- **Linting**: ruff check with E, W, F, I, N, UP, B, SIM, TCH, RUF, C4, PT, ANN rules
- **Type checking**: pyright in standard mode; type hints on all public APIs and
  non-trivial internal functions
- **Docstrings**: numpy convention, required on public modules/classes/functions
- **Data classes**: `attrs` (`@attrs.define`), not dataclasses
- **Array annotations**: `jaxtyping` for shape annotations (in core)
- **Serialization**: `json.dumps(attrs.asdict(obj))`, manual `from_dict()` classmethods,
  no cattrs

## Conventions

- `pydicom` for DICOM I/O, `zarr` v3 for storage, `pynrrd` for NRRD, `rich` for
  terminal output, `structlog` for server-side logging
- Build system: `hatchling` per package, `uv` workspace at root
- Nano-IDs: 8-char from alphabet `_-0123456789abcdefghijklmnpqrstuvwxyz` (no 'o')
- Coordinate system: LPS preferred, RAS auto-converted on write
- No backward compatibility with `dicom-transducer` — greenfield

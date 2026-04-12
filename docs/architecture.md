# Feature: Remote Annotation Architecture

## Status: Design / Iteration 4

## Overview

A greenfield monorepo for collaborative, network-capable annotation of volumetric
imaging data (primarily CT, not restricted to it). Zarr stores live on a remote server;
annotators pull data locally, annotate in 3D Slicer, and push annotations back. The
server integrates annotations into zarr and records full provenance.

Not bound by backward compatibility with `dicom-transducer`. Existing data can be
re-exported from basal DICOM data. The main goal is a robust, secure, and user-friendly
system that is nicely queryable for dataset instances — i.e. (volume, label) pairs that
adhere to a defined ontology.

Transport is plain SSH + rsync — no HTTP server, no open ports, no new infrastructure
beyond SSH key setup.

Target deployment: cloud VM (e.g. Hetzner 4vCPU) with attached volume, SSH access,
standard Unix environment. Expected annotator count: 2–5 concurrent.

---

## Project Name: `voxhub`

- CLI: `voxhub pull`, `voxhub push`, `voxhub catalog`
- Server CLI: `voxhub-server list-stores`, `voxhub-server integrate-annotations`
- Packages: `voxhub-core`, `voxhub-schema`, `voxhub-client`
- Python imports: `voxhub_core`, `voxhub_schema`, `voxhub_client`

---

## Monorepo Layout

```
voxhub/                             ← uv workspace root
├── pyproject.toml                  ← [tool.uv.workspace] members = ["packages/*"]
│
└── packages/
    ├── voxhub-core/                ← domain library + server entrypoint
    ├── voxhub-schema/              ← protocol contract + ontology + validation
    └── voxhub-client/              ← user-facing CLI + SSH/rsync mechanics
```

---

## Package: `voxhub-core`

### Role

All domain logic: zarr I/O, NRRD generation, staging, integration, auditing, naming.
Code is inspired by and may reuse parts of `dicom-transducer`, but is not a mechanical
port — redesigned where the new requirements (ontology, multi-annotator, provenance)
demand it. Also ships the SSH-invoked server CLI as a subpackage.

### Entrypoints

```toml
[project.scripts]
voxhub        = "voxhub_core.cli:main"          # local interactive use
voxhub-server = "voxhub_core.server.cli:main"   # SSH-invoked, prints JSON to stdout
```

### Dependencies

```
attrs, zarr, numpy, pydicom, pynrrd, rich, filelock, structlog,
voxhub-schema
```

### Directory structure

```
voxhub_core/
├── __init__.py
│
├── dicom/                          ← DICOM parsing + loading
│   ├── __init__.py
│   ├── parsing.py
│   ├── loading.py
│   ├── geometry.py
│   └── types.py
│
├── export.py                       ← zarr v3 writing, parallel pipeline, name generation
├── staging.py                      ← stage NRRDs for annotation
├── integrate.py                    ← write annotations into zarr (annotator-scoped paths)
├── audit.py                        ← cross-store ontology-based coherence checks
├── catalog.py                      ← zarr store discovery + rich tree/table
├── slicer.py                       ← Slicer format parsers + writers
├── naming.py                       ← adjective-noun name generation
├── visualization.py                ← matplotlib notebook helpers
├── cli.py                          ← local CLI: export, catalog, stage, integrate, audit, emit-nrrd
│
└── server/                         ← server subpackage; hard wall — local modules never import this
    ├── __init__.py
    ├── cli.py                      ← SSH-invoked CLI: list-stores, prepare-pull,
    │                                  integrate-annotations, cleanup, gc
    ├── provenance.py               ← writes to zarr attrs + .meta/provenance.jsonl
    ├── locks.py                    ← per-store filelock for concurrent push safety
    └── logging.py                  ← structlog configuration + context binding
```

### Design constraint

Local modules (`staging.py`, `integrate.py`, etc.) must never take server-specific
parameters (`annotator_id`, `session_id`, `lock`). Server wrappers in `server/cli.py`
call local functions then add provenance on top. The boundary is a hard wall.

The local CLI (`cli.py`) enforces the same strict metadata requirements as the remote
workflow — annotator ID, ontology, etc. must be provided. For remote-only fields
(`pull_session_id`), local workflow uses a `"local"` sentinel value.

---

## Package: `voxhub-schema`

### Role

Heavyweight shared package. Contains:
- SSH protocol data models (every JSON message that crosses the SSH pipe)
- Remote manifest schema
- Ontology definitions (versioned YAML data files) and loading logic
- Pre-flight validation logic (spatial checks, ontology conformance)

The client enforces ontology conformance pre-push using this package. The server (core)
uses ontology information to create the annotator-scoped hierarchy with correct metadata.

### Rationale for separate package

- Makes breaking protocol changes visible: bumping `voxhub-schema` signals both sides
  need updating before deploy
- Both `voxhub-core` and `voxhub-client` depend on it; neither depends on the other
- Client gets full validation capabilities without depending on zarr/pydicom

### Dependencies

```
attrs, numpy, pynrrd
```

### Directory structure

```
voxhub_schema/
├── __init__.py
│
├── models.py                       ← SSH protocol data models (all attrs classes)
│   # PROTOCOL_VERSION: int         ← current protocol version constant
│   # StoreInfo
│   # PrepareRequest, PrepareResponse, PreparedStore
│   # IntegrateRequest, IntegrateResponse, IntegrateResult
│   # CleanupResponse
│   # IssueRecord
│   # ServerError                   ← structured error envelope
│
├── manifest.py                     ← remote manifest schema
│   # RemoteManifest
│   # RemoteManifestEntry
│   # ManifestStatus: "pulled" | "pushed" | "integrated"
│
├── ontology.py                     ← ontology definitions + loading
│   # Ontology                      ← attrs class: name, version, type, labels/points
│   # OntologyLabel                 ← attrs class: value (int), name (str)
│   # OntologyType: "segmentation" | "landmarks"
│   # load_ontology(name, version=None) → Ontology
│   # list_ontologies() → list[Ontology]
│   # UNCONSTRAINED_SEGMENTATION    ← generic fallback ontology
│
├── ontologies/                     ← versioned YAML data files (shipped with package)
│   ├── inner-ear-total-fluid-space-v1.yaml
│   ├── inner-ear-structures-v1.yaml
│   ├── inner-ear-landmarks-v1.yaml
│   ├── unconstrained-v1.yaml
│   └── ...
│
├── nano_id.py                      ← nano-id generation utility
│   # ALPHABET: str                 ← '_-0123456789abcdefghijklmnpqrstuvwxyz' (no 'o')
│   # DEFAULT_SIZE: int             ← 8
│   # generate_nano_id(alphabet, size) → str
│
└── validation.py                   ← client-side pre-flight checks
    # validate_seg_preflight(seg_path, manifest_entry, ontology) → list[IssueRecord]
    #   - shape match against manifest
    #   - spatial consistency (space_origin, space_directions within tolerance)
    #   - label integrity (non-negative integers, sequential)
    #   - ontology conformance: exact label set matches ontology definition
    # validate_lmk_preflight(lmk_path, manifest_entry, ontology) → list[IssueRecord]
    #   - coordinate system known (LPS or RAS)
    #   - label uniqueness
    #   - ontology conformance: expected point names present
```

### Ontology YAML format

Version is encoded in the filename (`<ontology-name>-v<N>.yaml`).

```yaml
# inner-ear-structures-v1.yaml
name: inner-ear-structures
version: 1
type: segmentation
channel: multi
labels:
  0: background
  1: cochlea
  2: vestibule
  3: semicircular canals
```

```yaml
# inner-ear-landmarks-v1.yaml
name: inner-ear-landmarks
version: 1
type: landmarks
coordinate_system: LPS
points:
  - round window
  - oval window
  - cochlear apex
```

```yaml
# unconstrained-v1.yaml — generic fallback
name: unconstrained
version: 1
type: segmentation
channel: multi
labels: null
constraints:
  - non_negative_integers
  - sequential_from_zero
  - background_at_zero
```

When an ontology is extended (e.g. adding label 4 to `inner-ear-structures`), a new file
`inner-ear-structures-v2.yaml` is published alongside v1. `load_ontology("inner-ear-structures")`
returns the latest version; `load_ontology("inner-ear-structures", version=1)` returns a
specific version.

### Ontology versioning

Ontologies carry a `version` field (integer, monotonically increasing). Annotations
record the ontology version they were validated against (stored in zarr attrs).

Version mismatch handling:
- At push time: client validates against the ontology version shipped with its schema
  package.
- At query time: annotations with `ontology_version: 1` and `ontology_version: 2` of
  the same ontology are distinguishable. Consumers can filter by version.
- On mismatch: clear error message naming the versions involved.

### Nano-id generation

Shared utility used by both client (identity setup) and core (annotation path creation).

```python
ALPHABET: str = '_-0123456789abcdefghijklmnpqrstuvwxyz'  # 37 chars, no 'o' (avoids 0/o confusion)
DEFAULT_SIZE: int = 8  # 37^8 ≈ 3.5 trillion possible IDs


def generate_nano_id(alphabet: str = ALPHABET, size: int = DEFAULT_SIZE) -> str:
    alphabet_len = len(alphabet)
    nano_id = ''
    for _ in range(size):
        nano_id += alphabet[int(random() * alphabet_len) | 0]
    return nano_id
```

### Serialization convention

All models use `attrs`. Serialization is `json.dumps(attrs.asdict(obj))`. Deserialization
is manual `from_dict(d: dict)` classmethods. No `cattrs` dependency.

---

## Package: `voxhub-client`

### Role

User-facing CLI for remote workflows. Knows how to talk to a server over SSH, transfer
files with rsync, and manage local staging directories. Has no zarr dependency. Its only
awareness of data shapes comes through `voxhub-schema`.

### Entrypoint

```toml
[project.scripts]
voxhub = "voxhub_client.cli:main"
```

### Dependencies

```
voxhub-schema, rich
```

System tools invoked via subprocess: `ssh`, `rsync` (fallback: `scp`). No Python SSH
library — subprocess + system SSH is more reliable and handles key management, jump
hosts, and SSH config transparently.

### Directory structure

```
voxhub_client/
├── __init__.py
│
├── cli.py                          ← argparse: pull, push, remote-catalog, whoami, set-identity
│
├── identity.py
│   # get_identity() → Identity     ← reads from ~/.config/voxhub/identity.json
│   # set_identity(name)            ← generates nano_id, computes machine_id, writes config
│   # get_machine_id() → str        ← opaque hash of hardware identifiers
│   # Identity                      ← annotator_id + machine_id + nano_id
│
├── ssh.py
│   # SshTarget                     ← parses "user@host" (or bare host), holds user/host/port
│   # SshRunner                     ← runs ssh commands, parses JSON stdout, raises RemoteError
│   #   .run(*args, timeout=None) → dict   ← checks protocol_version in response
│   #   .mktemp() → str             ← creates temp dir on server, returns path
│
├── transfer.py
│   # RsyncTransfer                 ← rsync pull and push with progress
│   #   .pull(remote_path, local_path)
│   #   .push(local_path, remote_path)
│   # ScpFallback                   ← used when rsync unavailable
│
└── manifest.py
    # read_manifest(staging_dir) → RemoteManifest
    # write_manifest(staging_dir, manifest)
    # update_manifest_status(staging_dir, store_name, status)
```

---

## SSH Protocol

### Invocation convention

```
ssh [opts] user@host -- voxhub-server <subcommand> [args]
```

`voxhub-server` must be on PATH for non-interactive SSH sessions (installed via
`uv tool install` or into a virtualenv activated in `.bashrc` / `.profile`). If PATH
is not set up, the client falls back to a configurable `remote_python` stored in the
manifest.

### Output contract

- **stdout**: single JSON object per command (success or structured error)
- **stderr**: human-readable tracebacks/warnings (not parsed by client)
- **exit code**: 0 on success, non-zero on failure

Every server response includes a `protocol_version` field:

```json
{
  "protocol_version": 1,
  "stores": [...]
}
```

Client checks `protocol_version` in every response. On mismatch, the client errors
with a clear message: "Server protocol version N, client expects M. Update voxhub-schema
on both sides."

Structured error envelope (written to stdout on unexpected failure):

```json
{"protocol_version": 1, "error": true, "code": "integrate_failed", "message": "..."}
```

### Commands

#### `list-stores`

Reads the operator-configured stores directory from
``[storage].stores_dir`` in the server's TOML config (pointed at by
``VOXHUB_SERVER_CONFIG``).  The client supplies nothing.

Response:

```json
{
  "protocol_version": 1,
  "stores": [
    {
      "name": "gallivanting-groundhog",
      "shape": [128, 512, 512],
      "dtype": "float64",
      "origin_lps": [-249.5, -249.5, -319.0],
      "spacing_mm": [0.488, 0.488, 0.625],
      "space_directions": [[...], [...], [...]],
      "annotations": [
        {
          "path": "annotations/alice-x7f2kp01/inner-ear-structures-20260331-a3f1",
          "ontology": "inner-ear-structures",
          "ontology_version": 1,
          "annotator_id": "alice",
          "integrated_at": "2026-03-31T15:00:00+00:00"
        }
      ],
      "error": null
    }
  ]
}
```

#### `prepare-pull [--stores n1 n2] [--ontologies o1 o2] [--staging-dir /tmp/...] [--include-existing-annotations path1 path2] [--compress]`

Calls `core.staging.stage()` into a server-side temp dir. Computes SHA-256 of each
generated NRRD.

`--include-existing-annotations` requires fully explicit annotation paths (e.g.
`annotations/alice-x7f2kp01/inner-ear-structures-20260331-a3f1`). No auto-discovery —
heterogeneous annotation sets across stores and annotators make implicit selection
ambiguous.

The `--ontologies` parameter records expected ontologies in the pull manifest. These
guide the annotator (what to produce) and are enforced at push time.

All prepare-pull events are logged via structlog on the server.

Response:

```json
{
  "protocol_version": 1,
  "staging_dir": "/tmp/dt-pull-abc123",
  "server_stores_dir": "/srv/voxhub/zarr",
  "stores": {
    "gallivanting-groundhog": {
      "raw_checksum": "sha256:a3f1...",
      "shape": [128, 512, 512],
      "spacing_mm": [0.488, 0.488, 0.625],
      "origin_lps": [-249.5, -249.5, -319.0],
      "space_directions": [[...], [...], [...]],
      "expected_ontologies": ["inner-ear-structures", "inner-ear-landmarks"],
      "included_annotations": []
    }
  }
}
```

#### `integrate-annotations <staging_dir> --annotator-id <id> --machine-id <id> --nano-id <id> [--checksums file:sha256:... ...] [--force]`

Calls `core.integrate.integrate()` then `server.provenance.record()`. Holds a per-store
filelock during zarr writes. Verifies annotation file checksums before integrating.

Server determines annotation paths and ontology from the annotation file metadata
and the pull manifest. Creates annotator-scoped subgroups as needed.

All integrate events are logged via structlog on the server.

Response:

```json
{
  "protocol_version": 1,
  "stores": {
    "gallivanting-groundhog": {
      "status": "integrated",
      "annotations": [
        {
          "path": "annotations/alice-x7f2kp01/inner-ear-structures-20260331-a3f1",
          "ontology": "inner-ear-structures",
          "ontology_version": 1
        }
      ],
      "issues": [
        {"severity": "warning", "message": "Gap in label sequence: missing label 3"}
      ]
    }
  }
}
```

#### `cleanup <staging_dir>`

Removes server-side temp directory. Logged via structlog.

Response: `{"protocol_version": 1, "status": "ok"}`

#### `gc [--ttl-hours N]`

Garbage-collects stale server-side temp directories older than TTL (default: 24h).
Temp dirs use a naming convention with timestamps (e.g. `dt-push-20260331T150000-a3f1`).
Logged via structlog.

Response: `{"protocol_version": 1, "removed": [...], "count": N}`

---

## Client Workflows

### `pull user@host ./local_staging [--stores ...] [--ontologies ...] [--include-existing-annotations path1 path2] [--compress]`

The SSH target is just ``user@host`` (or a bare ``host``).  The server
owns its stores directory — the client never supplies one.

```
1. SshRunner.run("list-stores")
       → StoresResponse: store metadata + existing annotations

2. SshRunner.run("prepare-pull", "--stores", ..., "--ontologies", ...,
       "--include-existing-annotations", ...)
       → PrepareResponse: staging_dir on server, server_stores_dir,
         per-store checksums, expected ontologies

3. RsyncTransfer.pull(server:staging_dir/, local_staging/)
       rsync -az --progress user@host:/tmp/dt-abc/ ./local_staging/

4. SshRunner.run("cleanup", staging_dir)

5. write_manifest(local_staging, RemoteManifest(
       server_host=..., server_stores_dir=<from PrepareResponse>,
       stores={name: RemoteManifestEntry(
           status="pulled", raw_checksum=...,
           expected_ontologies=[...], ...
       )}
   ))
```

The manifest records expected ontologies from the pull request. These are used during
push to validate that the annotator produced conformant annotations.

Checksum caching: on re-pull, if local NRRD exists and SHA-256 matches
`PreparedStore.raw_checksum`, skip rsync for that store.

### `push ./local_staging [--validate-only] [--force]`

```
1. get_identity() → Identity (annotator_id + machine_id + nano_id)
       Abort if identity not configured (prompt user to run set-identity)

2. read_manifest(local_staging) → RemoteManifest
       Extract expected_ontologies per store

3. Discover annotation files in local_staging (*.seg.nrrd, *.mrk.json)
       Match each to its expected ontology from the manifest

4. Pre-flight validation (schema.validation):
       For each annotation file:
           load_ontology(expected_ontology) → Ontology
           validate_seg_preflight(seg_path, manifest_entry, ontology) → issues
           validate_lmk_preflight(lmk_path, manifest_entry, ontology) → issues
       Abort on any errors (unless --force)

5. Compute checksums of annotation files

6. SshRunner.mktemp() → server_staging

7. RsyncTransfer.push(local_staging/, server:server_staging/)
       rsync -az --progress ./local_staging/ user@host:/tmp/dt-push-xyz/

8. SshRunner.run("integrate-annotations", server_staging,
       "--annotator-id", identity.annotator_id,
       "--machine-id", identity.machine_id,
       "--nano-id", identity.nano_id,
       "--checksums", ...)
       → IntegrateResponse

9. SshRunner.run("cleanup", server_staging)

10. update_manifest_status(local_staging, store_name, "integrated")
```

### `remote-catalog user@host [--ontology ...]`

```
1. SshRunner.run("list-stores") → StoresResponse
2. Optionally filter by ontology
3. Render locally with rich (tree + table, showing per-annotator annotation status)
```

### `whoami`

```
1. get_identity() → Identity
2. Print annotator_id, machine_id, nano_id
```

### `set-identity <annotator_name>`

```
1. Generate nano_id (8-char, stable for this identity)
2. Compute machine_id
3. Write to ~/.config/voxhub/identity.json
```

---

## Annotator-Scoped Storage Model

### Motivation

Multiple annotators may annotate the same store. Storing all annotations at a single
fixed path creates write conflicts and loses annotator attribution at the data level.
Each annotation lives in an annotator-scoped subgroup with ontology-tagged instances.

The primary query pattern is: "give me all (volume, annotation) pairs for ontology X
across all stores." The storage layout must make this efficient.

### Zarr store structure

```
gallivanting-groundhog.zarr/
├── raw/
│   └── full                                            ← volume data (unchanged)
│
└── annotations/
    ├── alice-x7f2kp01/                                 ← annotator "alice", nano-id "x7f2kp01"
    │   ├── inner-ear-structures-20260331-a3f1/         ← segmentation instance
    │   │   └── (zarr array)
    │   │       attrs: {ontology: "inner-ear-structures", ontology_version: 1,
    │   │               segments: [...], coordinate_system: "LPS",
    │   │               integrated_at: "...", annotator_id: "alice", ...}
    │   │
    │   └── inner-ear-landmarks-20260331-b2c3/          ← landmark instance
    │       └── (zarr array)
    │           attrs: {ontology: "inner-ear-landmarks", ontology_version: 1,
    │                   labels: [...], coordinate_system: "LPS", ...}
    │
    └── bob-k9m1r5tz/                                   ← annotator "bob"
        └── inner-ear-structures-20260401-c4d5/
            └── (zarr array)
```

### Path convention

`annotations/<annotator_id>-<nano_id>/<ontology_name>-<date>-<short_random>/`

- `annotator_id`: from client identity (e.g. `alice`)
- `nano_id`: 8-char, stable per identity, generated once at `set-identity`
- `ontology_name`: the ontology this annotation conforms to
- `date`: `YYYYMMDD` of integration
- `short_random`: 4-char nano-id suffix for uniqueness on same-day re-annotation

### Queryability

**"All instances of ontology X across stores"** — two paths, neither requires opening
zarr metadata:

1. **Provenance index**: `grep '"ontology": "inner-ear-structures"' .meta/provenance.jsonl`
   → annotation paths for that ontology across all stores in the root

2. **Filesystem glob**: `*/annotations/*/inner-ear-structures-*/` across all zarr stores
   → direct path enumeration

Both are O(1) in zarr metadata reads. The provenance index is authoritative; the
filesystem glob is a convenience for scripting.

**"All of alice's annotations"**: `*/annotations/alice-*/`

**"All annotations for a specific store"**: list `<store>.zarr/annotations/`

### Locking implications

With annotator-scoped subgroups, concurrent writes target different zarr groups. The
main contention point is creating the shared `annotations/` parent group. With 2–5
annotators, filelock is sufficient as a safety net. In practice, contention will be
rare because different annotators write to different subgroups.

---

## Annotator Identity

### Design

Upon first use, the client CLI prompts the user to set an annotator ID (e.g. their name
as a slug). This generates a stable 8-char nano-id and computes a machine-specific ID.
All three are stored in `~/.config/voxhub/identity.json`.

**No overrides allowed.** The configured identity is used for all pushes. This ensures
consistent provenance.

### Identity file

```json
{
  "annotator_id": "alice",
  "nano_id": "x7f2kp01",
  "machine_id": "a3f1c9..."
}
```

If the file does not exist, the client aborts with a message to run `set-identity`.

### Edge cases

- **Shared workstations**: Each person has their own system user account with their own
  identity file. Machine ID is the same; annotator ID discriminates.
- **New machine**: Annotator ID provides continuity. Nano-id regenerates if
  `set-identity` is re-run, but the annotator_id is the primary key for human
  identification. Machine ID change is visible in provenance.
- **Machine ID computation**: Hash of hardware identifiers (MAC address, CPU ID,
  hostname). Stable across reboots.

---

## Provenance Design

### Zarr attribute schema (on each annotation array)

Every array written by the server carries:

```json
{
  "segments": [...],
  "coordinate_system": "LPS",
  "integrated_at": "2026-03-31T15:00:00+00:00",
  "annotator_id": "alice",
  "machine_id": "a3f1c9...",
  "nano_id": "x7f2kp01",
  "pull_session_id": "dt-pull-abc123",
  "source_nrrd_checksum": "sha256:a3f1...",
  "source_file": "segmentation.seg.nrrd",
  "ontology": "inner-ear-structures",
  "ontology_version": 1
}
```

### Provenance index

`.meta/provenance.jsonl` at the zarr root. Records push (mutating) events only. Pull
events belong in server logs.

```
{"event": "push", "session_id": "dt-push-xyz789", "pull_session_id": "dt-pull-abc123", "store": "gallivanting-groundhog", "annotation_path": "annotations/alice-x7f2kp01/inner-ear-structures-20260331-a3f1", "annotator_id": "alice", "machine_id": "a3f1c9...", "timestamp": "2026-03-31T15:00:00+00:00", "ontology": "inner-ear-structures", "ontology_version": 1, "issues": []}
```

At the expected scale (low two-digit annotations), a single JSONL file is sufficient.
The `.meta/` directory separates operational data from research data.

---

## Ontology System

### Motivation

Annotations without semantic constraints are difficult to use downstream. The ontology
system provides predefined label schemas that annotators must conform to, validated at
push time (client-side pre-flight) to keep the store clean.

### Ontology types

Three annotation modalities:

1. **Single-channel segmentation**: Fixed label→structure mapping
   ```yaml
   # inner-ear-total-fluid-space-v1.yaml
   name: inner-ear-total-fluid-space
   version: 1
   type: segmentation
   channel: single
   labels:
     0: background
     1: inner ear total fluid space
   ```

2. **Multi-channel segmentation**: Multiple structures in one label map
   ```yaml
   # inner-ear-structures-v1.yaml
   name: inner-ear-structures
   version: 1
   type: segmentation
   channel: multi
   labels:
     0: background
     1: cochlea
     2: vestibule
     3: semicircular canals
   ```

3. **Landmark set**: Named points
   ```yaml
   # inner-ear-landmarks-v1.yaml
   name: inner-ear-landmarks
   version: 1
   type: landmarks
   coordinate_system: LPS
   points:
     - round window
     - oval window
     - cochlear apex
   ```

### Generic fallback ontology

For variable-count or unconstrained annotations (V1 — extend with template ontologies
later if needed):

```yaml
# unconstrained-v1.yaml
name: unconstrained
version: 1
type: segmentation
channel: multi
labels: null
constraints:
  - non_negative_integers
  - sequential_from_zero
  - background_at_zero
```

### Ontology assignment

Ontologies are assigned at **pull time** via `--ontologies`. This fixes the intent
early and guides the annotator toward the right schema. The pull manifest records
expected ontologies per store. At push time, each annotation file is validated against
its expected ontology.

A single pull can request multiple ontologies (e.g. `--ontologies inner-ear-structures
inner-ear-landmarks`), since one annotation session may produce both segmentations and
landmarks. Each annotation file is validated independently against its own ontology.

### Ontology versioning

Version is encoded in the filename (`<ontology-name>-v<N>.yaml`). When an ontology is
extended, a new versioned file is published alongside the old one. `load_ontology(name)`
returns the latest version; `load_ontology(name, version=N)` returns a specific version.

Annotations record the ontology version they were validated against. Consumers can
filter by version. Version mismatch produces a clear error.

### Ontology storage

Versioned YAML data files shipped with the `voxhub-schema` package in an `ontologies/`
directory. Both client and server have access through the shared schema dependency.

---

## Observability & Logging

### Server-side structured logging

The server uses `structlog` for all logging. Every server command invocation logs:

- Timestamp, command name, duration
- Annotator ID, store names
- Success/failure, error details
- Temp dir creation/cleanup events

Log output is structured JSON to stderr. Server operators can redirect to a file
(e.g. via systemd journal, logrotate, or shell redirect in SSH forced command config).
This is separate from the provenance index — provenance is data lineage, logs are
operational observability.

Example log entries:

```json
{"event": "prepare_pull_started", "stores": ["gallivanting-groundhog"], "annotator_id": "alice", "timestamp": "..."}
{"event": "prepare_pull_completed", "stores": ["gallivanting-groundhog"], "staging_dir": "/tmp/dt-pull-abc123", "duration_s": 12.3, "timestamp": "..."}
{"event": "integrate_failed", "store": "gallivanting-groundhog", "error": "checksum_mismatch", "level": "error", "timestamp": "..."}
{"event": "gc_completed", "removed": ["/tmp/dt-push-20260330..."], "count": 1, "timestamp": "..."}
```

### Client-side feedback

The client does not log persistently. It uses `rich` for user-facing progress and error
reporting. Server-side error messages (from structured error envelopes) are surfaced
directly to the user.

---

## Failure Modes & Recovery

### rsync failure during push (partial upload)

**Mitigation**: After rsync completes, the client computes SHA-256 checksums of all
annotation files and passes them to `integrate-annotations --checksums`. The server
verifies checksums before integrating. Mismatch → reject with clear error. Logged via
structlog at error level.

### SSH connection drop between integrate and cleanup

**Mitigation**: Server temp dirs use timestamped naming (`dt-push-20260331T150000-a3f1`).
The `gc` server command removes temp dirs older than a configurable TTL (default: 24h).
Can be run manually (`voxhub-server gc`) or via cron. All cleanup events logged via
structlog.

### Server data changed between pull and push (stale pull)

**Mitigation**: With annotator-scoped subgroups, other annotators' pushes are safe. But
if the raw data was re-exported (shape change), the server verifies that the raw array
shape matches the manifest's recorded shape. Mismatch → reject with "stale pull" error.
Logged via structlog.

### Server command hangs

**Mitigation**: `SshRunner.run()` sets per-command timeout defaults (e.g. 60s for
`list-stores`, 600s for `integrate-annotations`). On timeout, kills SSH process,
reports error. Stale server-side state handled by temp dir TTL.

### Manifest corruption or deletion

**Mitigation**: Re-run `pull` to reconstruct. Checksum caching means existing NRRDs
aren't re-downloaded if unchanged. If annotation files exist locally but manifest is
gone, client warns and offers to re-pull.

---

## Migration & Backward Compatibility

### Strategy: no automatic migration

Existing zarr stores from `dicom-transducer` are treated as pre-system data. The new
system manages only stores it creates. Raw data can be re-exported from basal DICOM
sources using the new pipeline.

For the small subset of existing stores with annotations, a one-time manual migration
will be performed after the new system is operational — manually setting annotator ID,
ontology, and provenance metadata. This does not block development.

### Local workflow

The local CLI (`stage → integrate`) remains a first-class workflow. It enforces the same
strict metadata requirements as the remote workflow:
- Annotator ID: required (from local identity config)
- Ontology: required (from CLI flag or manifest)
- Provenance fields: `pull_session_id` uses `"local"` sentinel, `machine_id` from local
  identity

This ensures that locally-produced annotations are queryable and auditable in exactly
the same way as remote ones.

---

## Annotation Review (Future)

Not planned for V1. The design keeps the door open by allowing a future `review`
subgroup within each annotation instance:

```
annotations/alice-x7f2kp01/inner-ear-structures-20260331-a3f1/
├── (zarr array)                    ← the annotation
└── review/                         ← future: review events
    └── attrs: {reviewer, status: "approved"|"rejected", comments, timestamp}
```

No implementation work now — just don't close the door.

---

## Code Quality Standards

### Formatting & linting

- **Formatter**: ruff format, single quotes, 90-character line length
- **Linter**: ruff check with rules: E, W, F, I, N, UP, B, SIM, TCH, RUF, C4, PT, ANN
- **Type checking**: pyright in standard mode
- **Docstrings**: numpy convention (`Parameters`, `Returns`, `Raises` sections)
- PEP 8 observed throughout

### Type hints

Type hints required on:
- All public function signatures (parameters + return type)
- All `attrs` class fields
- Non-trivial internal functions

Exceptions: test functions, trivially obvious closures/lambdas.

Use `type` statement (Python 3.12+) for type aliases. Use `jaxtyping` for array shape
annotations in core.

### Tool configuration

All tool configuration lives in the root `pyproject.toml`:
- `[tool.ruff]`, `[tool.ruff.lint]`, `[tool.ruff.format]`
- `[tool.pyright]`
- `[tool.pytest.ini_options]`

Packages inherit root configuration. No per-package tool config.

---

## Implementation Order

### Phase 0: Design prerequisites

1. **Finalize the ontology YAML schema** — write the initial versioned YAML files,
   validate the data model works for all known annotation types.
3. **Finalize the annotator-scoped storage path convention** — confirm the
   `<annotator>-<nano_id>/<ontology>-<date>-<random>` layout works for all query
   patterns.

### Phase 1: Schema package (no I/O, fully testable)

4. `voxhub-schema`: protocol models with `PROTOCOL_VERSION`
5. `voxhub-schema`: manifest schema
6. `voxhub-schema`: ontology loading from versioned YAML + `list_ontologies()`
7. `voxhub-schema`: nano-id generation utility
8. `voxhub-schema`: validation logic (spatial + ontology conformance)

### Phase 2: Core package

9. Port/rewrite DICOM pipeline into `voxhub-core` (parsing, loading, geometry, export)
10. `staging.py`: NRRD generation for annotation
11. `integrate.py`: annotation → zarr with annotator-scoped paths + ontology metadata
12. `catalog.py`: store discovery with annotation + ontology awareness
13. `audit.py`: ontology-based cross-store coherence checks
14. `slicer.py`: Slicer format parsers + writers

### Phase 3: Server

15. `server/logging.py`: structlog configuration
16. `server/cli.py`: `list-stores` + `prepare-pull` + `cleanup` + `gc`
17. `server/cli.py`: `integrate-annotations` with checksum verification
18. `server/provenance.py`: zarr attrs + `.meta/provenance.jsonl`
19. `server/locks.py`: per-store filelock

### Phase 4: Client

20. `identity.py`: `set-identity`, `whoami`, `get_identity()`
21. `ssh.py`: `SshRunner` with protocol version checking + timeouts
22. `transfer.py`: `RsyncTransfer` with progress
23. Client CLI: `pull` end-to-end
24. Client CLI: `push` with pre-flight validation + checksum passing
25. Client CLI: `remote-catalog` with ontology filtering

### Phase 5: Hardening

26. Temp dir TTL (`gc` command, timestamp naming)
27. Checksum-based re-pull caching
28. Manifest reconstruction on re-pull
29. Timeout defaults per command

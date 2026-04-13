# `voxhub pull` + `prepare-pull` redesign

## Context

`voxhub pull` does not exist yet. The client package has transport primitives
(`SshRunner`, `RsyncTransfer`) and identity / server-config management, but no
user-facing data commands.

The original `prepare-pull` / `RemoteManifest` design tied pull sessions to
integration events via a session-ID-bearing manifest. That design is being dropped.
Pull is now a simple "rsync volumes down + detailed audit log" operation. The staging
directory is ephemeral and torn down by the client immediately after a successful
transfer; the existing `gc` command handles crash recovery.

The existing `RemoteManifest` / `RemoteManifestEntry` are left untouched — their
fate will be decided when push is redesigned.

---

## Schema additions — `voxhub_schema`

**File:** `packages/voxhub-schema/src/voxhub_schema/manifest.py`

Append three new attrs classes after the existing `RemoteManifest`. Follow the same
pattern: attrs fields, `from_dict` classmethod, JSON helpers, `read` / `write`
disk helpers.

### `PullAnnotationEntry`

```python
@attrs.define
class PullAnnotationEntry:
    path: str                  # original zarr instance-group path (provenance only)
                               # e.g. 'annotations/alice-xyz45678/inner-ear-structures-20260101-ab12'
    kind: str                  # 'segmentation' | 'landmarks'
    ontology: str
    ontology_version: int
    annotator_id: str
    integrated_at: str         # ISO timestamp from zarr array attrs
    reference_filename: str    # filename in reference/ subdir
                               # e.g. 'alice-xyz45678_inner-ear-structures-20260101-ab12.seg.nrrd'
    reference_checksum: str    # 'sha256:<hex>' of the exported reference file
```

`reference_filename` and `reference_checksum` are the push contract: push globs
`*.seg.nrrd` / `*.mrk.json` at the **session root only** (not recursive) and rejects
any file whose SHA256 matches a `reference_checksum` in the manifest.

### `PullStoreEntry`

```python
@attrs.define
class PullStoreEntry:
    store_name: str             # redundant with dict key — cross-checked by push
    raw_checksum: str           # 'sha256:<hex>' of raw.nrrd
    shape: list[int]
    spacing_mm: list[float]
    origin_lps: list[float]
    space_directions: list[list[float]]
    annotations: list[PullAnnotationEntry] = attrs.Factory(list)
```

`store_name` is intentionally redundant with the key in `PullManifest.stores`. Push
cross-checks that `dir_name == entry.store_name`; a mismatch (renamed directory or
edited manifest key) is a hard error before anything reaches the server.

### `PullManifest`

```python
@attrs.define
class PullManifest:
    protocol_version: int
    prepared_at: str            # ISO UTC
    server_stores_dir: str      # provenance only
    stores: dict[str, PullStoreEntry]
```

Written by the server to `<staging_dir>/.voxhub_pull.json`. Read by the client
after rsync to verify raw NRRD checksums, lock the session, and populate the
pull log.

**File:** `packages/voxhub-schema/src/voxhub_schema/__init__.py`

Add `PullAnnotationEntry`, `PullManifest`, `PullStoreEntry` to the `manifest`
import block and to `__all__`.

---

## New server module — `voxhub_core/export.py`

**File:** `packages/voxhub-core/src/voxhub_core/export.py`

Inverse of the integrate functions: reads annotation zarr arrays and writes them
back to their source file formats. Called by `prepare-pull` when producing
reference files.

```python
def export_segmentation(zarr_path: Path, array_zarr_path: str, dest: Path) -> str:
    """Export a segmentation zarr array to a .seg.nrrd file.

    Reads label map data and segment attrs (Segment{i}_ID, Name, LabelValue,
    Color) from the zarr array and writes a Slicer-compatible .seg.nrrd using
    _write_nrrd_raw from staging.py with segmentation-specific headers.

    Returns the 'sha256:<hex>' checksum of the written file.
    """

def export_landmarks(zarr_path: Path, array_zarr_path: str, dest: Path) -> str:
    """Export a landmark zarr array to a .mrk.json file.

    Reads point coordinates and labels from the zarr array attrs and writes
    a Slicer Markup JSON file (same format as parse_mrk_json expects).

    Returns the 'sha256:<hex>' checksum of the written file.
    """
```

`array_zarr_path` is the path within the zarr store to the array, e.g.
`annotations/alice-xyz45678/inner-ear-structures-20260101-ab12/data`.

Both functions raise `ExportError(Exception)` on failure (unreadable array,
unsupported dtype, missing attrs). Callers treat `ExportError` as non-fatal
and skip the annotation entry.

---

## Server changes — `_run_prepare_pull`

**File:** `packages/voxhub-core/src/voxhub_core/server/cli.py`

Three sequential tasks replace the current body:

### Task 1 — Upfront store validation

Runs before `mkdtemp`. If any requested store is absent the command errors
immediately with no staging dir created (fail-all-or-nothing):

```python
if store_names is not None:
    missing = [n for n in store_names if not (stores_dir / f'{n}.zarr').is_dir()]
    if missing:
        _write_error('store_not_found', f'Stores not found: {missing}')
        sys.exit(1)
```

### Task 2 — Stage NRRDs + export reference annotations

The `stage()` call is unchanged. The annotation handling replaces the old
`shutil.copytree` loop: instead of copying zarr array directories, each matched
annotation is **exported** to its source file format under `reference/`.

For each `ann_path` in `include_annotations`, for each store in `store_metadata`:

```python
zarr_path = stores_dir / f'{store_name}.zarr'
src_array = zarr_path / ann_path / 'data'
if not src_array.exists():
    continue

grp = zarr.open_group(zarr_path / ann_path, mode='r')
arr = grp['data']
a   = dict(arr.attrs)
kind = 'segmentation' if 'segments' in a else 'landmarks'

# Derive filename from instance-group basename + annotator prefix.
instance_name  = Path(ann_path).name   # e.g. 'inner-ear-structures-20260101-ab12'
annotator_slug = Path(ann_path).parts[-2]  # e.g. 'alice-xyz45678'
ext = '.seg.nrrd' if kind == 'segmentation' else '.mrk.json'
ref_filename = f'{annotator_slug}_{instance_name}{ext}'

ref_dir = staging_dir / 'reference'
ref_dir.mkdir(exist_ok=True)
ref_dest = ref_dir / ref_filename

try:
    array_zarr_path = str(Path(ann_path) / 'data')
    if kind == 'segmentation':
        checksum = export_segmentation(zarr_path, array_zarr_path, ref_dest)
    else:
        checksum = export_landmarks(zarr_path, array_zarr_path, ref_dest)

    ann_entries_by_store[store_name].append(
        PullAnnotationEntry(
            path=ann_path,
            kind=kind,
            ontology=str(a.get('ontology', '')),
            ontology_version=int(a.get('ontology_version', 0)),
            annotator_id=str(a.get('annotator_id', '')),
            integrated_at=str(a.get('integrated_at', '')),
            reference_filename=ref_filename,
            reference_checksum=checksum,
        )
    )
except ExportError as exc:
    log.warning('annotation_export_failed', path=ann_path, error=str(exc))
    # Non-fatal: skip this annotation entry.
```

`ann_path` convention: instance-group path without `/data`, matching
`catalog.py`'s `AnnotationEntry.path` (e.g.
`annotations/alice-xyz45678/inner-ear-structures-20260101-ab12`).

**Staging dir layout after task 2:**
```
<staging_dir>/
  raw.nrrd
  reference/
    alice-xyz45678_inner-ear-structures-20260101-ab12.seg.nrrd
    alice-xyz45678_inner-ear-landmarks-20260101-cd34.mrk.json
```

### Task 3 — Write `PullManifest` to staging dir

```python
PullManifest(
    protocol_version=PROTOCOL_VERSION,
    prepared_at=datetime.now(UTC).isoformat(),
    server_stores_dir=str(stores_dir),
    stores={
        name: PullStoreEntry(
            store_name=name,
            raw_checksum=meta['raw_checksum'],
            shape=meta['shape'],
            spacing_mm=meta['spacing_mm'],
            origin_lps=meta['origin_lps'],
            space_directions=meta['space_directions'],
            annotations=ann_entries_by_store.get(name, []),
        )
        for name, meta in store_metadata.items()
    },
).write(staging_dir)
```

The stdout JSON response (wire protocol) is **unchanged** — `PreparedStore` fields
`expected_ontologies` and `included_annotations` are kept for wire compatibility.

New imports needed:
```python
from voxhub_core.export import ExportError, export_landmarks, export_segmentation
from voxhub_schema.manifest import PullAnnotationEntry, PullManifest, PullStoreEntry
```

---

## Client changes — `voxhub pull`

**File:** `packages/voxhub-client/src/voxhub_client/cli.py`

### Module-level additions

```python
class ChecksumError(Exception): ...

_PULL_LOG = Path.home() / '.local' / 'share' / 'voxhub' / 'pulls.jsonl'

def _compute_sha256(path: Path) -> str: ...

def _verify_checksums(local_staging: Path, manifest: PullManifest) -> None:
    """Raise ChecksumError on any raw.nrrd mismatch or missing file."""

def _lock_session(session_dir: Path) -> None:
    """Lock the session directory against accidental modification.

    Sets permissions in one pass:
      - .voxhub_pull.json  → 0o444  (read-only file)
      - reference/         → 0o555  (read+execute: listable, not writable)
      - reference/*        → 0o444  (each reference file read-only)

    Non-fatal on OSError anywhere in the traversal — warns only.
    Portable: POSIX enforces full bit mask; Windows collapses to S_IREAD /
    S_IREAD|S_IEXEC, both of which block accidental writes on all three
    platforms.
    """

def _append_pull_log(entry: dict[str, object]) -> None:
    """Best-effort append to pulls.jsonl; warns on OSError, never raises."""
```

### `_run_pull` — step-by-step

| Step | Action | On failure |
|------|---------|------------|
| 1 | `get_identity()` | print actionable error, exit 1 |
| 2 | `get_server()` → `SshRunner`, `RsyncTransfer` | print actionable error, exit 1 |
| 3 | `dest = Path(args.dest or Path.cwd())` | — |
| 4 | SSH `prepare-pull` via `runner.run(...)` | `RemoteError` → print, exit 1 |
| 5 | `transfer.pull(prepare.staging_dir, str(dest))` | `CalledProcessError` → print, **skip cleanup**, exit 1 |
| 6 | `PullManifest.read(dest)` | `FileNotFoundError` → print, **skip cleanup**, exit 1 |
| 7 | `_verify_checksums(dest, manifest)` | `ChecksumError` → print mismatch details, **skip cleanup**, exit 1 |
| 8 | `_lock_session(dest)` | `OSError` → warn, continue |
| 9 | SSH `cleanup <staging_dir>` | warn, continue (GC handles it) |
| 10 | `_append_pull_log({..., 'manifest_sha256': _compute_sha256(dest / '.voxhub_pull.json')})` | `OSError` → warn, continue |
| 11 | Print rich summary table | — |

Steps 5–7 deliberately skip cleanup on failure: the server staging dir may be
needed for diagnosis, and GC will collect it within 24 h.

Step 8 (`_lock_session`) runs after checksum verification succeeds and locks in
one pass: `.voxhub_pull.json` → `0o444`, `reference/` → `0o555`,
`reference/*` → `0o444`. Any `OSError` (e.g. network filesystem) is a warning,
never a failure.

Step 10 records `manifest_sha256` so push can detect modification. The SHA256 is
computed after locking so it reflects the final state of the file.

**Pull log fields** (one JSON line per pull):

```json
{
  "pulled_at": "<ISO UTC>",
  "annotator_id": "alice",
  "machine_id": "abc123...",
  "server_host": "voxhub@myserver.example.com",
  "server_stores_dir": "/srv/voxhub/stores",
  "dest": "/home/alice/work/dt-pull-abc123-XXXXXXXX",
  "stores": ["patient-001", "patient-002"],
  "compress": false,
  "protocol_version": 1,
  "manifest_sha256": "sha256:<hex of .voxhub_pull.json>"
}
```

`manifest_sha256` is the root of trust for push: it records the SHA256 of the
locked manifest at the moment the pull completed successfully. Push reads the pull
log entry matching `dest`, recomputes the manifest hash, and refuses if they differ.

### Push contract (set by this plan, implemented separately)

Push reads the pull manifest from the session dir and uses it to:

1. Verify `manifest_sha256` against the pull log entry for this `dest` path.
2. Glob `*.seg.nrrd` and `*.mrk.json` at the **session root only** (not recursive —
   `reference/` is excluded by directory depth).
3. For each found file compute SHA256 and compare against all
   `entry.reference_checksum` values in the manifest.
   - **Match** → reject with message:
     `"<file> matches a reference file from the pull — create a new annotation instead"`
   - **No match** → include in push payload.

### Local session layout (after successful pull)

```
./my-session/
  .voxhub_pull.json                                               ← 0o444
  raw.nrrd
  reference/                                                      ← 0o555
    alice-xyz45678_inner-ear-structures-20260101-ab12.seg.nrrd    ← 0o444
    alice-xyz45678_inner-ear-landmarks-20260101-cd34.mrk.json     ← 0o444
  [annotator creates new files here at root level]
```

### Subparser

```python
pull = subparsers.add_parser('pull', help='Pull volumes from the remote server.')
pull.add_argument('--stores', nargs='*')
pull.add_argument('--dest', default=None)
pull.add_argument('--compress', action='store_true')
pull.add_argument('--include-existing-annotations', nargs='*', metavar='PATH')
pull.set_defaults(func=_run_pull)
```

**New imports:**

```python
import hashlib, json, subprocess, sys
from datetime import UTC, datetime
from pathlib import Path
from rich.console import Console
from voxhub_client.ssh import SshRunner
from voxhub_client.transfer import RsyncTransfer
from voxhub_schema.manifest import PullManifest
from voxhub_schema.models import PrepareResponse
```

---

## Files touched

| File | Change |
|---|---|
| `packages/voxhub-schema/src/voxhub_schema/manifest.py` | Add `PullAnnotationEntry`, `PullStoreEntry`, `PullManifest` |
| `packages/voxhub-schema/src/voxhub_schema/__init__.py` | Export new classes |
| `packages/voxhub-core/src/voxhub_core/export.py` | **New** — `export_segmentation`, `export_landmarks`, `ExportError` |
| `packages/voxhub-core/src/voxhub_core/server/cli.py` | Redesign `_run_prepare_pull` (3 tasks) |
| `packages/voxhub-client/src/voxhub_client/cli.py` | Add `pull` subcommand + helpers |

---

## Tests

### New: `packages/voxhub-schema/tests/test_pull_manifest.py`

- JSON round-trip, no annotations
- JSON round-trip with `PullAnnotationEntry` (`kind='segmentation'` and `'landmarks'`)
- `write` / `read` round-trip via `tmp_path`
- `read` on missing file → `FileNotFoundError`
- `from_dict` without `'annotations'` key → `annotations == []`
- `store_name` field survives round-trip and matches dict key

### New: `packages/voxhub-core/tests/test_export.py`

Reuse `create_zarr_store`, `populate_store_annotation` from `_core_helpers.py`.

- `test_export_segmentation_produces_seg_nrrd` — populated seg annotation → valid `.seg.nrrd` readable by `parse_seg_nrrd`; segment names, label values, spatial metadata match
- `test_export_segmentation_returns_sha256` — returned checksum matches `hashlib.sha256` of written file
- `test_export_landmarks_produces_mrk_json` — populated lmk annotation → valid `.mrk.json` readable by `parse_mrk_json`; labels and coordinates match
- `test_export_landmarks_returns_sha256` — checksum matches file
- `test_export_segmentation_missing_array_raises_export_error` — nonexistent `array_zarr_path` → `ExportError`
- `test_export_landmarks_missing_array_raises_export_error` — same for landmarks

### Extend: `packages/voxhub-core/tests/test_server_cli.py`

Reuse `stores_dir_factory`, `server_argv`, `parsed_stdout`, `populate_store_annotation`.

- `test_store_not_found_fails_before_staging` — missing store name → `store_not_found` error, no staging dir
- `test_store_not_found_lists_all_missing` — two missing names both in error message
- `test_pull_manifest_written` — `.voxhub_pull.json` exists after success
- `test_pull_manifest_fields` — `protocol_version`, `server_stores_dir`, `store_name`, checksum, shape, spacing correct
- `test_pull_manifest_no_annotations_when_none_requested` — `annotations == []` per store
- `test_pull_manifest_annotation_segmentation` — `populate_store_annotation` with seg → `kind='segmentation'`, `reference_filename` ends with `.seg.nrrd`, `reference_checksum` matches file on disk
- `test_pull_manifest_annotation_landmarks` — `kind='landmarks'`, `reference_filename` ends with `.mrk.json`
- `test_reference_dir_created` — `reference/` subdir exists and contains exported files
- `test_pull_manifest_annotation_export_failure_is_non_fatal` — corrupt zarr attrs → exit 0, `annotations == []`, no `reference/` file

### New: `packages/voxhub-client/tests/test_pull.py`

No `voxhub_core` imports. Use `monkeypatch` and `tmp_path`.

- `_compute_sha256` matches `hashlib.sha256` reference result
- `_verify_checksums` passes on matching checksum
- `_verify_checksums` → `ChecksumError` on hash mismatch (message contains store name)
- `_verify_checksums` → `ChecksumError` when `raw.nrrd` missing
- `_append_pull_log` creates parent dirs + appends valid JSONL
- `_append_pull_log` called twice → two lines
- `_lock_session` sets manifest to `0o444`, `reference/` to `0o555`, each reference file to `0o444`; subsequent `write_text` on any raises `PermissionError`
- `_lock_session` `OSError` on any individual chmod is swallowed (non-fatal), remaining files still processed
- `_run_pull` happy path (monkeypatched): steps 4–10 called in order, log contains `manifest_sha256`
- rsync failure → cleanup not called, exit 1
- checksum failure → cleanup not called, exit 1
- lock failure → cleanup still called, log still written, exit 0
- cleanup failure → log still written, exit 0
- `args.dest=None` → rsync receives `str(Path.cwd())`
- argparse: `--stores` optional, multi-value; `--compress` flag

---

## Verification

```bash
uv run pyright
uv run ruff check packages/
uv run pytest packages/voxhub-schema/tests/test_pull_manifest.py -v
uv run pytest packages/voxhub-core/tests/test_export.py -v
uv run pytest packages/voxhub-core/tests/test_server_cli.py -v -k "prepare_pull or pull_manifest or store_not_found or reference"
uv run pytest packages/voxhub-client/tests/test_pull.py -v
uv run pytest   # full suite must stay green
```

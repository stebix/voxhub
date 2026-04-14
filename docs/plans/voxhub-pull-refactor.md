# `voxhub pull` + `prepare-pull` redesign

## Context

`voxhub pull` does not exist yet. The client package has transport primitives
(`SshRunner`, `RsyncTransfer`) and identity / server-config management, but no
user-facing data commands.

The original `prepare-pull` / `RemoteManifest` design tied pull sessions to
integration events via a session-ID-bearing manifest. That design is being
dropped. Pull is now: "rsync a single store's raw volume + optional exported
reference annotations down, emit a hardened audit manifest." The staging dir
is ephemeral — the client's SSH `cleanup` call both signals success to the
server (delivery ACK) and asks it to reap. The existing `gc` command is the
crash-recovery backstop for un-ACK'd staging dirs.

Pull targets **exactly one store per invocation**. Multi-store workflows
compose at the shell level (invoke pull twice). The existing `RemoteManifest`
/ `RemoteManifestEntry` are left untouched — their fate will be decided when
push is redesigned.

---

## Schema additions — `voxhub_schema`

**File:** `packages/voxhub-schema/src/voxhub_schema/manifest.py`

Append two new attrs classes after the existing `RemoteManifest`. Follow the
same pattern: attrs fields, `from_dict` classmethod, JSON helpers, `read` /
`write` disk helpers.

### `PullAnnotationEntry`

```python
@attrs.define
class PullAnnotationEntry:
    zarr_source_path: str      # original zarr instance-group path (reintegration provenance)
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

`reference_filename` and `reference_checksum` feed the push filter contract
(see below).

### `PullManifest`

```python
@attrs.define
class PullManifest:
    protocol_version: int
    prepared_at: str              # ISO UTC
    server_host: str              # provenance (fqdn of staging server)
    server_stores_dir: str        # provenance
    store_name: str               # reintegration target for push
    raw_name: str                 # filename of raw volume in the session dir
    raw_checksum: str             # 'sha256:<hex>' of raw volume
    shape: list[int]
    spacing_mm: list[float]
    origin_lps: list[float]
    space_directions: list[list[float]]
    annotations: list[PullAnnotationEntry] = attrs.Factory(list)
```

Written by the server to `<staging_dir>/.voxhub_pull.json`. Read by the client
after rsync to verify the raw checksum, lock the session, and populate the
pull log.

`raw_name` is explicit (not a convention) so the locker and push can refer to
the volume by manifest-declared name — keeps the door open for alternative raw
formats later without touching the locker or push filter.

**File:** `packages/voxhub-schema/src/voxhub_schema/__init__.py`

Add `PullAnnotationEntry`, `PullManifest` to the `manifest` import block and
to `__all__`.

---

## New server module — `voxhub_core/export.py`

**File:** `packages/voxhub-core/src/voxhub_core/export.py`

Inverse of the integrate functions: reads annotation zarr arrays and writes
them back to their source file formats. Called by `prepare-pull` when
producing reference files.

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

Three sequential tasks replace the current body. All are single-store — the
command takes exactly one store name.

### Task 1 — Upfront store validation

Runs before `mkdtemp`. If the requested store is absent the command errors
immediately with no staging dir created:

```python
zarr_path = stores_dir / f'{store_name}.zarr'
if not zarr_path.is_dir():
    _write_error('store_not_found', f'Store not found: {store_name!r}')
    sys.exit(1)
```

### Task 2 — Stage raw + export reference annotations

`stage()` is called once for `store_name`. The annotation handling replaces
the old `shutil.copytree` loop: instead of copying zarr array directories,
each matched annotation is **exported** to its source file format under
`reference/`.

Initialize `ann_entries: list[PullAnnotationEntry] = []` and
`skipped_annotations: list[dict[str, str]] = []` before the loop.

For each `ann_path` in `include_annotations`:

```python
src_array = zarr_path / ann_path / 'data'
if not src_array.exists():
    skipped_annotations.append({'path': ann_path, 'reason': 'array not found in store'})
    continue

grp = zarr.open_group(zarr_path / ann_path, mode='r')
arr = grp['data']
a   = dict(arr.attrs)
kind = 'segmentation' if 'segments' in a else 'landmarks'

instance_name  = Path(ann_path).name
annotator_slug = Path(ann_path).parts[-2]
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

    ann_entries.append(
        PullAnnotationEntry(
            zarr_source_path=ann_path,
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
    skipped_annotations.append({'path': ann_path, 'reason': str(exc)})
    # Non-fatal: skip this annotation entry, but surface it in the response.
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
    server_host=socket.getfqdn(),
    server_stores_dir=str(stores_dir),
    store_name=store_name,
    raw_name='raw.nrrd',
    raw_checksum=store_metadata['raw_checksum'],
    shape=store_metadata['shape'],
    spacing_mm=store_metadata['spacing_mm'],
    origin_lps=store_metadata['origin_lps'],
    space_directions=store_metadata['space_directions'],
    annotations=ann_entries,
).write(staging_dir)
```

The stdout JSON response (wire protocol) narrows to a single store naturally.
Since `pull` does not exist yet there are no client consumers to preserve
compatibility for — `PreparedStore`-shaped fields can flatten into
`PrepareResponse` directly.

`PrepareResponse` gains a new field:

```python
skipped_annotations: list[dict[str, str]] = attrs.Factory(list)
# Each entry: {'path': <ann_path>, 'reason': <human-readable reason>}
# Populated when a requested annotation was missing or failed to export.
# Non-fatal on the server side; the client surfaces it prominently so the
# annotator is never silently denied a reference file they asked for.
```

New imports needed:

```python
import socket
from voxhub_core.export import ExportError, export_landmarks, export_segmentation
from voxhub_schema.manifest import PullAnnotationEntry, PullManifest
```

---

## Client changes — `voxhub pull`

**File:** `packages/voxhub-client/src/voxhub_client/cli.py`

### Module-level additions

```python
class ChecksumError(Exception): ...

def _compute_sha256(path: Path) -> str: ...

def _verify_checksums(session_dir: Path, manifest: PullManifest) -> None:
    """Raise ChecksumError on raw or reference-file mismatch / absence.

    Checks manifest.raw_name and every manifest.annotations[*].reference_filename.
    """

def _lock_session(session_dir: Path, manifest: PullManifest) -> None:
    """Lock server-authoritative files against accidental modification.

    Sets permissions in one pass:
      - .voxhub_pull.json        → 0o444
      - .voxhub_pull.sha256      → 0o444   (trust sidecar; see _write_trust_sidecar)
      - manifest.raw_name        → 0o444   (server-authoritative raw volume)
      - reference/               → 0o555   (listable, not writable)
      - reference/*              → 0o444   (each reference file read-only)

    The session root directory is left writable so the annotator can create
    new files. Non-fatal on OSError anywhere in the traversal — warns only.
    Portable: POSIX enforces full bit mask; Windows collapses to S_IREAD /
    S_IREAD|S_IEXEC, both of which block accidental writes.
    """

def _write_trust_sidecar(session_dir: Path) -> str:
    """Hash `.voxhub_pull.json` and write the digest to `.voxhub_pull.sha256`.

    The sidecar is the tamper-evident anchor push uses to detect manifest
    modification. It lives inside the session dir so it moves with the data
    under rename, move, or copy — unlike a `$HOME`-keyed log entry.

    Writes a single line: `sha256:<hex>\\n`. Returns the same digest string
    (for inclusion in the audit log).
    """

### New helper module — `voxhub_client/pull_log.py`

```python
_PULL_LOG = Path.home() / '.local' / 'share' / 'voxhub' / 'pulls.jsonl'

def append_entry(entry: dict[str, object]) -> None:
    """Best-effort append to pulls.jsonl; warns on OSError, never raises."""
```

The log is a **user-facing audit trail only** — "what have I pulled, from
where, when?" It is not a trust surface: push does not read it, and it has no
role in detecting manifest tamper. That job belongs to the in-session
`.voxhub_pull.sha256` sidecar (see `_write_trust_sidecar` above), which moves
with the session directory under rename, move, or copy and does not depend on
absolute paths in `$HOME`.

Keeping the log append-only and trust-free lets the annotator freely reorganise
their working directories without breaking push.

### `_run_pull` — step-by-step

| Step | Action | On failure |
|------|---------|------------|
| 1 | `get_identity()` | print actionable error, exit 1 |
| 2 | `get_server()` → `SshRunner`, `RsyncTransfer` | print actionable error, exit 1 |
| 3 | `dest = Path(args.dest or Path.cwd())` | — |
| 4 | SSH `prepare-pull --store <name> [...]` via `runner.run(...)` | `RemoteError` → print, exit 1 |
| 5 | `transfer.pull(prepare.staging_dir, str(dest))` | `CalledProcessError` → print, **skip cleanup**, exit 1 |
| 6 | `PullManifest.read(dest)` | `FileNotFoundError` → print, **skip cleanup**, exit 1 |
| 7 | `_verify_checksums(dest, manifest)` | `ChecksumError` → print mismatch details, **skip cleanup**, exit 1 |
| 8 | `manifest_digest = _write_trust_sidecar(dest)` — hash `.voxhub_pull.json`, write `.voxhub_pull.sha256` | `OSError` → print, **skip cleanup**, exit 1 (tamper anchor is required) |
| 9 | `_lock_session(dest, manifest)` — locks manifest, sidecar, raw, reference/ in one pass | `OSError` → warn, continue |
| 10 | SSH `cleanup <staging_dir>` (delivery ACK) | warn, continue (GC handles it) |
| 11 | `pull_log.append_entry({...})` — audit only | `OSError` → warn, continue |
| 12 | Print rich summary table, including a **warning panel** listing `prepare.skipped_annotations` (path + reason) if non-empty | — |

Steps 5–8 deliberately skip cleanup on failure: the server staging dir may
be needed for diagnosis, and GC will collect it within the GC horizon.

**Step 8 rationale.** The sidecar is the tamper-evident anchor for push. If
we cannot write it, the session is not fully valid — push will refuse anyway —
so failing the pull here is more honest than silently producing a session that
cannot be pushed. This is the one non-I/O-ignorable failure mode in the
post-rsync tail.

**Step 9 locking.** Runs after checksum verification and sidecar write, and
locks in one pass: `.voxhub_pull.json` → `0o444`, `.voxhub_pull.sha256` →
`0o444`, `manifest.raw_name` → `0o444`, `reference/` → `0o555`, `reference/*`
→ `0o444`. Any `OSError` (e.g. network filesystem) is a warning, never a
failure — locking is defence-in-depth on top of the checksum/sidecar backstop.

**Step 10 semantics.** The `cleanup` SSH call **is** the delivery ACK — one
RPC both signals success to the server and asks it to reap. Failure here
(network blip, transient SSH error) is **non-fatal**: the client already has
valid, locked data with the trust anchor written, and the server's GC will
reap the orphaned staging dir on its next pass. The same applies to client
crashes between step 9 and step 10. GC reap events are the observability
signal for un-ACK'd pulls (see below).

**Step 11** is audit-only (what / when / where from). It carries no
trust-bearing data.

**Pull log fields** (one JSON line per pull, audit-only):

```json
{
  "pulled_at": "<ISO UTC>",
  "annotator_id": "alice",
  "machine_id": "abc123...",
  "server_host": "voxhub@myserver.example.com",
  "server_stores_dir": "/srv/voxhub/stores",
  "dest": "/home/alice/work/patient-001",
  "store": "patient-001",
  "compress": false,
  "protocol_version": 1
}
```

No hash field — the trust anchor is `.voxhub_pull.sha256` inside the session
dir, not the log.

**Trust model.** `.voxhub_pull.sha256` is a **tamper-evident check against
accidental modification**: push recomputes the SHA256 of `.voxhub_pull.json`
and compares it to the sidecar. If they differ, push refuses — the manifest
(and therefore the reject set) can no longer be trusted. This guards against
stray edits, mis-configured tools, and partial rsync overwrites. It does
**not** defend against a determined annotator: both files live in the session
dir as plaintext, and the 0o444 locks are reversible with `chmod u+w`, so a
deliberate attacker can rewrite manifest + sidecar in lockstep. The server
remains the authority. If stronger guarantees are ever needed (e.g. untrusted
annotator hosts), the manifest would have to be signed server-side with a key
the annotator does not hold; that is out of scope here.

### Push contract (set by this plan, implemented separately)

Push reads the pull manifest from the session dir and uses it to:

1. Read `.voxhub_pull.sha256` from the session dir. Recompute SHA256 of
   `.voxhub_pull.json`. Refuse if they differ, or if the sidecar is missing
   (`"no trust sidecar — this session was not produced by `voxhub pull`, or
   the sidecar was removed"`). No `pulls.jsonl` lookup is involved — the
   anchor travels with the data.
2. Read `manifest.store_name` — this is the reintegration target.
3. Glob `*.nrrd`, `*.seg.nrrd`, `*.mrk.json` at the **session root only**
   (not recursive — `reference/` is excluded by directory depth).
4. Build the **reject-by-name** set:
   `{manifest.raw_name} ∪ {e.reference_filename for e in manifest.annotations}`.
   This catches server-authoritative files whose content was modified in place:
   their SHA256 no longer matches the manifest, so a hash-only check would let
   them through as "new annotations".
5. Build the **reject-by-hash** set:
   `{manifest.raw_checksum} ∪ {e.reference_checksum for e in manifest.annotations}`.
   This catches server-authoritative files that were renamed or copied: the name
   is new but the content is unchanged.
6. For each found file at session root, in order:
   - **Filename in reject-by-name set** → hard error:
     `"<file> is server-authoritative — if it was modified, restore from a re-pull; do not push it as an annotation"`
   - **SHA256 in reject-by-hash set** → hard error:
     `"<file> is an unmodified copy of server-authoritative data — create a new annotation instead"`
   - **Otherwise** → include in push payload.

Step 8's locking prevents accidental edits in the common case; the name + hash
compare at push time is the backstop against the remaining accident modes
(lock bypass, copy-to-new-name, in-place edit).

### Local session layout (after successful pull)

```
./patient-001/
  .voxhub_pull.json                                               ← 0o444
  .voxhub_pull.sha256                                             ← 0o444 (trust anchor)
  raw.nrrd                                                        ← 0o444
  reference/                                                      ← 0o555
    alice-xyz45678_inner-ear-structures-20260101-ab12.seg.nrrd    ← 0o444
    alice-xyz45678_inner-ear-landmarks-20260101-cd34.mrk.json     ← 0o444
  [annotator creates new .seg.nrrd / .mrk.json files here at root]
```

### Subparser

```python
pull = subparsers.add_parser('pull', help='Pull a store from the remote server.')
pull.add_argument('--store', required=True, help='Store name to pull.')
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
from voxhub_client import pull_log
from voxhub_client.ssh import SshRunner
from voxhub_client.transfer import RsyncTransfer
from voxhub_schema.manifest import PullManifest
from voxhub_schema.models import PrepareResponse
```

---

## Server-side GC observability

GC-reaped staging dirs are the protocol-health signal for un-ACK'd pulls.
The `gc` command should emit a structured event per reap so operators can
watch the rate:

```python
log.info(
    'staging_dir_reaped',
    staging_dir=str(path),
    age_seconds=age,
    store_name=manifest.store_name if manifest else None,
    had_manifest=manifest is not None,
    reason='gc_unacked',
)
```

Paired with a corresponding event on the normal cleanup path
(`reason='client_ack'`), the ratio `ack_reaps / (ack_reaps + gc_reaps)`
becomes a one-number protocol-health metric. A small steady baseline is
expected (client crashes, flaky SSH after rsync); spikes indicate
degradation. The `had_manifest=False` subset separates prepare-pull crashes
from post-rsync-cleanup failures.

Structlog output only — no changes to on-disk artifacts.

---

## Files touched

| File | Change |
|---|---|
| `packages/voxhub-schema/src/voxhub_schema/manifest.py` | Add `PullAnnotationEntry`, `PullManifest` |
| `packages/voxhub-schema/src/voxhub_schema/__init__.py` | Export new classes |
| `packages/voxhub-core/src/voxhub_core/export.py` | **New** — `export_segmentation`, `export_landmarks`, `ExportError` |
| `packages/voxhub-core/src/voxhub_core/server/cli.py` | Redesign `_run_prepare_pull` (3 tasks); emit `staging_dir_reaped` events from `gc` and `cleanup` |
| `packages/voxhub-client/src/voxhub_client/pull_log.py` | **New** — append-only audit helper for `pulls.jsonl` (no trust role) |
| `packages/voxhub-client/src/voxhub_client/cli.py` | Add `pull` subcommand + helpers |

---

## Tests

### New: `packages/voxhub-schema/tests/test_pull_manifest.py`

- JSON round-trip with no annotations
- JSON round-trip with `PullAnnotationEntry` (`kind='segmentation'` and `'landmarks'`)
- `write` / `read` round-trip via `tmp_path`
- `read` on missing file → `FileNotFoundError`
- `from_dict` without `'annotations'` key → `annotations == []`
- `store_name`, `raw_name`, `raw_checksum`, `server_host` all survive round-trip

### New: `packages/voxhub-core/tests/test_export.py`

Reuse `create_zarr_store`, `populate_store_annotation` from `_core_helpers.py`.

- `test_export_segmentation_produces_seg_nrrd` — populated seg annotation →
  valid `.seg.nrrd` readable by `parse_seg_nrrd`; segment names, label
  values, spatial metadata match
- `test_export_segmentation_returns_sha256` — returned checksum matches
  `hashlib.sha256` of written file
- `test_export_landmarks_produces_mrk_json` — populated lmk annotation →
  valid `.mrk.json` readable by `parse_mrk_json`; labels and coordinates match
- `test_export_landmarks_returns_sha256` — checksum matches file
- `test_export_segmentation_missing_array_raises_export_error` — nonexistent
  `array_zarr_path` → `ExportError`
- `test_export_landmarks_missing_array_raises_export_error` — same for landmarks

### Extend: `packages/voxhub-core/tests/test_server_cli.py`

Reuse `stores_dir_factory`, `server_argv`, `parsed_stdout`, `populate_store_annotation`.

- `test_store_not_found_fails_before_staging` — missing store → `store_not_found` error, no staging dir
- `test_pull_manifest_written` — `.voxhub_pull.json` exists after success
- `test_pull_manifest_fields` — `protocol_version`, `server_host`, `server_stores_dir`, `store_name`, `raw_name`, `raw_checksum`, shape, spacing correct
- `test_pull_manifest_no_annotations_when_none_requested` — `annotations == []`
- `test_pull_manifest_annotation_segmentation` — seg annotation → `kind='segmentation'`, `reference_filename` ends with `.seg.nrrd`, `reference_checksum` matches file on disk
- `test_pull_manifest_annotation_landmarks` — `kind='landmarks'`, `reference_filename` ends with `.mrk.json`
- `test_reference_dir_created` — `reference/` subdir contains exported files
- `test_pull_manifest_annotation_export_failure_is_non_fatal` — corrupt zarr attrs → exit 0, `annotations == []`, no `reference/` file
- `test_skipped_annotation_missing_array` — requesting an `ann_path` whose `data` array does not exist → response `skipped_annotations` contains `{path, reason: 'array not found in store'}`, `annotations == []`
- `test_skipped_annotation_export_failure` — requesting an annotation whose export raises `ExportError` → response `skipped_annotations` contains `{path, reason: <ExportError message>}`
- `test_skipped_annotations_empty_when_all_succeed` — all requested annotations export cleanly → `skipped_annotations == []`
- `test_gc_emits_staging_dir_reaped_event` — GC over an aged staging dir emits `staging_dir_reaped` with `reason='gc_unacked'` and `store_name` from manifest
- `test_cleanup_emits_staging_dir_reaped_event` — normal cleanup emits `reason='client_ack'`

### New: `packages/voxhub-client/tests/test_pull.py`

No `voxhub_core` imports. Use `monkeypatch` and `tmp_path`.

- `_compute_sha256` matches `hashlib.sha256` reference result
- `_verify_checksums` passes on matching raw + reference checksums
- `_verify_checksums` → `ChecksumError` on raw hash mismatch (message contains `raw_name`)
- `_verify_checksums` → `ChecksumError` when `raw_name` file missing
- `_verify_checksums` → `ChecksumError` on any reference-file mismatch
- `_lock_session` sets `.voxhub_pull.json`, `.voxhub_pull.sha256`, `raw_name`, each reference file to `0o444`; `reference/` to `0o555`; session root stays writable
- `_lock_session` — subsequent `write_text` on `raw_name` raises `PermissionError`
- `_lock_session` `OSError` on any individual chmod is swallowed (non-fatal), remaining files still processed
- `_lock_session` uses `manifest.raw_name` (not a hardcoded `'raw.nrrd'`) — parametrize with a custom name
- `_write_trust_sidecar` — writes `.voxhub_pull.sha256` containing `sha256:<hex>` matching `_compute_sha256` of `.voxhub_pull.json`
- `_write_trust_sidecar` — survives rename: move the session dir, recompute hash in new location, sidecar still matches (proves in-session anchoring)
- `_write_trust_sidecar` `OSError` (read-only FS) → `OSError` propagates (this is step 8, a fatal failure)
- `_run_pull` happy path (monkeypatched): steps 4–11 called in order; `.voxhub_pull.sha256` exists and matches hash of manifest; audit log entry contains `store` and `dest` but **no** `manifest_sha256` field
- `_run_pull` — skipped_annotations from `PrepareResponse` surface in the summary output (capture stdout/rich console)
- rsync failure → cleanup not called, exit 1
- checksum failure → cleanup not called, exit 1
- **sidecar write failure** → cleanup not called, exit 1 (session is not fully valid without the anchor)
- lock failure → cleanup still called, log still written, exit 0
- cleanup (ACK) failure → log still written, exit 0
- pull_log append failure → exit 0 (non-fatal)
- `args.dest=None` → rsync receives `str(Path.cwd())`
- argparse: `--store` required (parser errors without it), single value; `--compress` flag

### New: `packages/voxhub-client/tests/test_pull_log.py`

- `append_entry` creates parent dirs + appends valid JSONL
- `append_entry` called twice → two lines
- `append_entry` `OSError` is non-fatal (swallowed with warn)
- entry shape: contains `store`, `dest`, `pulled_at`, `server_host`, `protocol_version`; does **not** contain any hash / trust field (explicit negative assertion — catches accidental re-introduction of the old trust surface)

### New: `packages/voxhub-client/tests/test_pull_e2e.py`

End-to-end tests for the full `voxhub pull` flow. Unlike `test_pull.py`, these
are **not** monkeypatched — they drive the server CLI as a real subprocess
invoked through `SshRunner` configured to use a loopback transport (no real
SSH daemon; `SshRunner` accepts a command-prefix override so `ssh …` resolves
to a direct subprocess exec of `python -m voxhub_core.server`). Rsync uses a
`--rsh` hook that does the same. This catches wire-protocol drift that the
unit layer cannot.

Scope:

- `test_pull_e2e_raw_only` — server has a populated store, no annotations;
  client `pull --store <name>` lands `raw.nrrd`, `.voxhub_pull.json`,
  `.voxhub_pull.sha256`. Sidecar hash matches manifest file. Lock bits
  applied. Server staging dir is gone (ACK'd). Exit 0.
- `test_pull_e2e_with_seg_and_landmarks` — server store with one seg + one
  landmark annotation; client requests both via `--include-existing-annotations`.
  `reference/` contains both exported files, names and checksums match
  manifest entries. `.seg.nrrd` re-parses via `parse_seg_nrrd`; `.mrk.json`
  via `parse_mrk_json`.
- `test_pull_e2e_store_not_found` — client pulls a non-existent store; server
  exits 1 with `store_not_found`, client prints the actionable error, no
  session files written locally, no staging dir on server.
- `test_pull_e2e_skipped_annotation_surfaces` — client requests an annotation
  whose export fails (corrupt attrs); server exits 0, `skipped_annotations`
  appears in the response, client summary output mentions the skipped path.
  Valid annotations still exported.
- `test_pull_e2e_rename_then_verify_sidecar` — after a successful pull, rename
  the session directory; the sidecar still matches the manifest hash when
  recomputed in the new location. This is the core property Option A buys and
  must have an e2e regression guard.
- `test_pull_e2e_wire_protocol_shape` — capture the raw JSON on server stdout,
  assert it parses to `PrepareResponse` with `store_name`, `staging_dir`,
  `skipped_annotations` fields present. Guards against drift between server
  emission and client deserialization.
- `test_pull_e2e_ack_cleanup_reaches_server` — verify (via filesystem check
  in the server's temp dir) that the staging dir exists after prepare-pull
  and is gone after the pull completes. Paired `staging_dir_reaped` event
  with `reason='client_ack'` in captured server logs.

These tests are slower than the unit suite — gate them with a `pytest.mark.e2e`
marker and run in CI but not on every local save.

---

## Verification

```bash
uv run pyright
uv run ruff check packages/
uv run pytest packages/voxhub-schema/tests/test_pull_manifest.py -v
uv run pytest packages/voxhub-core/tests/test_export.py -v
uv run pytest packages/voxhub-core/tests/test_server_cli.py -v -k "prepare_pull or pull_manifest or store_not_found or reference or staging_dir_reaped or skipped_annotation"
uv run pytest packages/voxhub-client/tests/test_pull.py packages/voxhub-client/tests/test_pull_log.py -v
uv run pytest packages/voxhub-client/tests/test_pull_e2e.py -v -m e2e
uv run pytest   # full suite must stay green
```

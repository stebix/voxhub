# `voxhub pull` refactor — follow-up #1

Three cleanups carried over from the main pull-refactor plan. Scoped as
high-level goals; each will get a concrete implementation plan once the
code is inspected.

---

## 1. Three-verb vocabulary: `export` / `extract` / `stage`

### Motivation

The codebase today uses two verbs — *export* and *stage* — for
overlapping concepts, and the word *export* is used for two genuinely
different pipelines:

- `voxhub_core.export` (`export.py`) — **DICOM → zarr** ingestion
  (author-facing, runs once per source dataset).
- `voxhub_core.annotation_export` — **zarr array → NRRD/JSON** for
  reference annotations emitted by `prepare-pull` (added in the main
  plan).
- `voxhub_core.staging.stage()` — does **two** things: the same
  zarr → NRRD transformation on raw volumes, *and* orchestrates the
  staging-directory layout (dir creation, per-store metadata,
  force/exists handling).

Net result: one verb (*export*) covers two unrelated pipelines, and
another verb (*stage*) conflates a transform with a choreography.

### Goal

Adopt three orthogonal verbs, one per concept:

| Verb        | Meaning                                              | Home                              |
|-------------|------------------------------------------------------|-----------------------------------|
| **export**  | DICOM → zarr ingestion                               | `voxhub_core/export.py` (unchanged) |
| **extract** | zarr array → a single on-disk file (NRRD / JSON)     | new `voxhub_core/extraction.py`   |
| **stage**   | assemble a staging directory (layout, manifest, dir) | `voxhub_core/staging.py` (shrinks) |

"Extract" is chosen over alternatives (`transduce`, `materialize`,
`emit`, `render`) because: it already has a foothold in the codebase
(`extract_spatial_metadata`), it is semantically precise for "pull one
array out of a zarr container and express it as a standalone file", and
it avoids re-using *transduce* (which CLAUDE.md explicitly distances
the project from, `dicom-transducer` legacy).

### Concretely

**New module — `voxhub_core/extraction.py`** (houses all zarr → file
transforms and their shared helpers):

- `ExtractionError` (was `annotation_export.ExportError`)
- `extract_segmentation(zarr_path, array_zarr_path, dest) -> str` (was
  `annotation_export.export_segmentation`)
- `extract_landmarks(zarr_path, array_zarr_path, dest) -> str` (was
  `annotation_export.export_landmarks`)
- `extract_volume(zarr_path, dest, *, compress) -> str` — **new**,
  peeled out of the inner loop of `staging.stage()`; performs the
  single-store raw-volume zarr → NRRD transform
- `extract_spatial_metadata(attrs)` — moved from `staging.py`
- `write_nrrd_raw(path, data, header, *, compress)` — promoted from
  `staging._write_nrrd_raw` to public (shared between the extractors)
- `compute_sha256(path)` — deduplicated from the two identical
  private helpers in `staging.py` and `annotation_export.py`
- `_build_raw_volume_header`, `_build_seg_nrrd_header` — stay private

**Shrunk module — `voxhub_core/staging.py`**:

- `stage(stores_dir, staging_dir, *, store_names, compress, force,
  console)` — retains its signature and CLI contract, but becomes a
  pure orchestrator: discovers zarr stores, creates the staging
  directory, calls `extract_volume()` per store, collects metadata.
- Loses `_write_nrrd_raw`, `_build_nrrd_header`, `_compute_sha256`,
  and `extract_spatial_metadata` (all move to `extraction.py`).

**Deleted module**: `voxhub_core/annotation_export.py`.

**Server CLI glue (`voxhub_core/server/cli.py`)**:

- Import site updates to pull from `voxhub_core.extraction`.
- `_export_reference_annotations` → `_extract_reference_annotations`.
- Structlog event key `'annotation_export_failed'` →
  `'annotation_extraction_failed'`.
- Wire-protocol field `skipped_annotations` stays. The *reason*
  strings inside it currently interpolate `ExportError` messages;
  those error texts are not changed by the rename (only the exception
  class name), so the substring assertion in
  `tests/test_server_cli.py` (`'not found' in skip['reason']`) keeps
  passing.

**Tests**:

- `packages/voxhub-core/tests/test_annotation_export.py` →
  `test_extraction.py` (rename + import updates).
- Any staging test that imported `_write_nrrd_raw` or
  `extract_spatial_metadata` from `staging` updates its import path.
- No assertion-text changes needed.

### Non-goals

- Do **not** touch `voxhub_core.export` (DICOM → zarr) — that module
  keeps its name and public API.
- Do **not** change `prepare-pull`'s on-disk layout
  (`reference/<filename>`, `.voxhub_pull.json`) or the `PullManifest`
  schema. The rename is internal to `voxhub-core` plus the server-CLI
  glue.
- Do **not** change the `stage()` CLI surface (the local-workflow
  `voxhub-core stage` command keeps its arguments and output).
- No backward-compat re-exports from `staging.py` for the moved
  symbols — greenfield policy (CLAUDE.md) applies; clean break.

### Open questions for implementation

1. **`write_nrrd_raw` public or private?** Used by both
   `extract_volume` and (indirectly, via `nrrd.write`) the annotation
   extractors. Leaning public to avoid duplication; confirm during
   implementation.
2. **Log-event rename blast radius.** Rename of
   `annotation_export_failed` → `annotation_extraction_failed` is
   safe given no external dashboards yet (Hetzner small-VPS, no
   observability stack provisioned), but worth one last grep before
   landing.

### Commit structure

Prefer a single atomic rename commit — the change is mechanical and
atomicity preserves `git blame` coherence. If review prefers smaller
units, split into:

1. Create `extraction.py` (move from `annotation_export.py` + low-
   level helpers from `staging.py`); delete `annotation_export.py`;
   shrink `staging.py` to orchestrator.
2. Update `server/cli.py` imports, function name, and event key.
3. Rename test module and update imports; update this planning doc
   and the main pull-refactor doc.

### Success criteria

- No occurrence of "export" in the `voxhub-core` surface that refers
  to zarr → Slicer-file materialisation. Remaining uses of "export"
  refer exclusively to DICOM → zarr ingestion.
- `voxhub_core.extraction` is the single home for zarr → on-disk-file
  transforms. `voxhub_core.staging` is the single home for
  staging-directory choreography.
- No change in wire-protocol responses, manifest schema, on-disk
  layout, client code, or test semantics — only names move.
- `just check` and `just test` stay green with identical test counts.

---

## 2. Client-side end-to-end pull test

### Motivation

The main plan's test pyramid was completed up to the unit layer but the
end-to-end suite was deferred. Every piece of the pull flow is unit-
tested in isolation, but nothing exercises the full loop:

```
client CLI → SshRunner → subprocess(voxhub-server prepare-pull)
           → RsyncTransfer → local copy
           → PullManifest.read → _verify_checksums
           → _write_trust_sidecar → _lock_session
           → SshRunner cleanup → pull_log.append_entry
```

Wire-protocol drift between server emission (`_write_dict` in
`server/cli.py`) and client deserialisation (`PullManifest.read`) is
exactly the kind of thing unit tests miss — both sides can be
individually correct yet disagree.

### Goal

Add an opt-in e2e test module that drives the real server entrypoint
from the real client code, without requiring an actual SSH daemon or
network.

Approach (sketch; details in the follow-up implementation plan):

- A **loopback shim** for `SshRunner` that runs
  `python -m voxhub_core.server.cli …` as a direct subprocess and
  returns the parsed JSON, skipping `ssh`.
- A **loopback shim** for `RsyncTransfer.pull` that copies the server's
  staging directory to the client's destination via `rsync` against
  the local filesystem (no `ssh` hop), or `shutil.copytree`.
- Both shims selected via a dependency-injection hook or a pytest
  fixture that monkeypatches the two classes. The production code path
  is unchanged.

Test cases worth encoding (non-exhaustive — see main plan §Tests for
the full list):

- raw-only pull: landed files, locks, sidecar, manifest match.
- seg + landmark export: re-parses via `parse_seg_nrrd` /
  `parse_mrk_json` after round-trip.
- `store_not_found`: client exits 1, no session files written locally,
  no staging dir on "server".
- `skipped_annotations` surfaces in the client's summary output.
- **Rename-then-verify**: after a successful pull, move the session
  directory; sidecar still matches the manifest hash recomputed in the
  new location. This is the core property the sidecar design buys.
- Wire-protocol shape: capture raw server stdout, assert it parses to
  `PrepareResponse` with all expected fields.
- ACK cleanup reaches server: staging dir exists after `prepare-pull`,
  gone after pull completes; `staging_dir_reaped` event with
  `reason='client_ack'` captured.

### Non-goals

- **Not** a full integration test of `push`. Push hasn't been redesigned
  yet (see §3); its e2e coverage belongs with that work.
- **Not** a substitute for live-SSH smoke tests. This suite gives fast,
  hermetic, deterministic coverage; real-network verification is a
  separate concern (CI job against a throwaway VM, manual pre-release).

### Success criteria

- Gated by a `pytest.mark.e2e` marker so normal `just test` runs stay
  fast; explicit `just test-e2e` recipe (or `-m e2e`) runs them.
- Runs in CI but not on every local save.
- No dependency on `ssh`, `sshd`, or any network listener. Must pass on
  a disconnected laptop.
- Rename-then-verify is present — that property only this suite can
  prove.

---

## 3. Document the `RemoteManifest` latent bug

### Motivation

Surfaced during the pull refactor but deliberately out of scope: the
server's `prepare-pull` command never writes `.voxhub_manifest.json`
(the `RemoteManifest` artefact), but `integrate-annotations` reads it
and hard-errors if it's absent. The current test suite papers over the
gap with a `write_remote_manifest` helper that synthesises the file
for each integrate test.

This means:

- The **tests** pass because they fabricate a manifest before invoking
  `integrate-annotations`.
- The **production flow** would fail: a real pull never produces
  `.voxhub_manifest.json`, so a real `integrate-annotations` call would
  return the `manifest_missing` error envelope.

Push has not been executed end-to-end yet in production, which is why
this has not bitten. It **will** bite the moment push runs for real.

### Goal

Land a docs-only change that records the bug, its trigger, its current
masking, and the coarse fix options — so whoever picks up the push
redesign inherits full context instead of rediscovering it.

### Scope

A new doc (e.g. `docs/known-issues/remote-manifest-write-gap.md` —
final location TBD) with at minimum:

- **Symptom**: `integrate-annotations` errors with `manifest_missing`
  when invoked against a staging directory produced by `prepare-pull`.
- **Root cause**: `_run_prepare_pull` does not call
  `RemoteManifest.write(staging_dir)`; the manifest class exists and
  has a working writer, but no caller.
- **Why it's latent**: tests create the manifest via
  `packages/voxhub-core/tests/_core_helpers.py::write_remote_manifest`
  before driving `integrate-annotations`, so the gap never surfaces in
  the unit or subprocess suites.
- **Why the main pull refactor didn't fix it**: the new `PullManifest`
  (`.voxhub_pull.json`) is a separate artefact serving a different
  purpose (pull-side trust anchor + reference audit). `RemoteManifest`
  is push-workflow state (pull session ID, expected ontologies,
  integration status transitions) and deserves a decision coupled with
  the push redesign.
- **Fix options** (coarse — the push plan picks one):
  1. **Delete `RemoteManifest`.** Migrate `integrate-annotations` to
     consume `PullManifest` + any push-time arguments it still needs
     (expected ontologies, included annotations). Simplest long-term;
     biggest scope.
  2. **Write `RemoteManifest` alongside `PullManifest` in
     `prepare-pull`.** Minimally invasive — a few lines in
     `_run_prepare_pull` — but leaves two near-duplicate manifests on
     disk and doubles the schema-evolution surface.
  3. **Treat the manifest as push-owned.** The client synthesises
     `RemoteManifest` at push time from the session dir's
     `PullManifest` plus its own state (session ID, ontologies). No
     server change; the gap disappears because nothing on the server
     side ever needed to write it.

Option 3 is the cleanest boundary (server owns pull; client owns push)
and the push plan should adopt it unless a concrete reason emerges.
But the doc should enumerate all three neutrally.

### Non-goals

- **No code changes.** This item is docs only. The fix belongs with the
  push-redesign plan.
- No test changes. The masking helper (`write_remote_manifest`) stays
  in place until push is redesigned.

### Success criteria

- A single markdown file under `docs/known-issues/` (or wherever fits
  the project's convention) describing the bug.
- Linked from `docs/plans/voxhub-pull-refactor.md` (or its successor)
  so the push-redesign author finds it immediately.
- No code or test changes in the commit that lands this doc.

---

## Follow-on opportunities (out of scope for §1)

Two cleanups naturally enabled by the `extraction.py` split but kept
out of the base rename commit so `git blame` for the rename stays
mechanical:

### A. Simplify `_run_prepare_pull` raw-volume staging

`server/cli.py:_run_prepare_pull` currently calls `staging.stage()` to
produce a single-store NRRD, then manually flattens the nested
`<staging_dir>/<store_name>/raw.nrrd` up to `<staging_dir>/raw.nrrd`
and `rmdir`s the now-empty subdir (`server/cli.py:337-345`). With
`extraction.extract_volume()` available, the flow collapses to a
direct call that writes `raw.nrrd` at the session root:

```python
meta = extract_volume(zarr_path, staging_dir / 'raw.nrrd', compress=compress)
```

The "flatten" kludge and its dedicated error envelope
(`staging_flatten_failed`) disappear. The `stage()` CLI command
(author-facing, multi-store) is untouched.

### B. Dedup `server/cli.py::_compute_sha256`

After the rename, `voxhub_core.extraction.compute_sha256` is a public
helper with the same body as the private `_compute_sha256` at
`server/cli.py:97`. Replace the private copy with an import from
`extraction`. `tests/test_server_cli.py:589` currently reaches into
`server_cli._compute_sha256`; update it to the new import site (or
keep a thin `_compute_sha256 = compute_sha256` alias in the module if
the test access pattern is load-bearing for other reasons).

Both items are trivial follow-ons — expect a single commit each.

# `voxhub pull` refactor — follow-up #1

Three cleanups carried over from the main pull-refactor plan. Scoped as
high-level goals; each will get a concrete implementation plan once the
code is inspected.

---

## 1. Unify `export` / `stage` naming under "staging"

### Motivation

"Export" is overloaded in `voxhub-core`:

- `voxhub_core.export` — **DICOM → zarr** ingestion (author-facing, runs
  once per source dataset).
- `voxhub_core.annotation_export` — **zarr → NRRD/JSON** for reference
  annotations emitted by `prepare-pull` (added in the main plan).

Meanwhile the raw-volume side of the same `prepare-pull` flow lives in
`voxhub_core.staging.stage()`, which does the conceptually identical
zarr → NRRD transformation. The codebase now uses two verbs — *stage*
and *export* — for the same semantic operation ("materialise a zarr
array as a Slicer-readable on-disk file"), and the word *export* also
means something else in the same package.

### Goal

Collapse the "zarr → on-disk Slicer file" vocabulary to a single verb:
**stage**. The DICOM → zarr pipeline keeps *export*, which becomes
unambiguous again.

Concretely:

- `voxhub_core/annotation_export.py` → rename to something like
  `annotation_staging.py` (or fold into `staging.py` if cohesion
  supports it — to be decided during implementation).
- `export_segmentation` / `export_landmarks` → rename to
  `stage_segmentation_reference` / `stage_landmarks_reference` (or
  similar). The *reference* qualifier distinguishes them from the raw
  volume `stage()` already in that module.
- `ExportError` → `StagingError` (or the equivalent) and all call sites
  (`server/cli.py::_export_reference_annotations`, tests).
- Wire-protocol field `skipped_annotations` stays. The *reason* strings
  inside it currently interpolate `ExportError` messages; that text is
  not a contract but worth eyeballing during the rename.

### Non-goals

- Do **not** touch `voxhub_core.export` (DICOM → zarr) — that module
  keeps its name and public API.
- Do **not** change `prepare-pull`'s on-disk layout
  (`reference/<filename>`, `.voxhub_pull.json`) or the `PullManifest`
  schema. The rename is internal to `voxhub-core` plus the server-CLI
  glue.

### Success criteria

- No occurrence of "export" in the `voxhub-core` surface that refers to
  zarr → Slicer-file materialisation.
- `voxhub_core.export` remains and continues to mean DICOM → zarr
  ingestion.
- No change in wire-protocol responses, manifest schema, client code,
  or test semantics — only names move.
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

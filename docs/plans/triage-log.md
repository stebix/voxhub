# Triage Log — Plan Implementation Wave 1

Running, timestamped log of pain points and inconsistencies discovered while
implementing and merging `docs/plans/launch-readiness-implementation-plan.md`
and `docs/plans/architecture-improvement-plan.md`. Newest entries at the
bottom of each day's section. See `docs/plans/after-action-report-2026-07-11.md`
for the full narrative synthesis of the implementation wave.

Severity legend: **P0** blocks launch/merge · **P1** needs an owner decision or
follow-up task · **P2** worth fixing opportunistically, not blocking.

---

## 2026-07-11 — Implementation wave (7 parallel agents)

**[P0] Plan §E.2's tearing premise is factually wrong on local Linux.**
`record_provenance` issues a single `write()` per record (strace-verified at
328 KB); `O_APPEND` on a local regular file is atomic well beyond `PIPE_BUF`.
The documented tear never reproduced against the real code path. The filelock
still landed (justified for NFS + future multi-write records), but the plan's
stated rationale and the old test's docstring were inaccurate.
— Source: `plan/e2-provenance-lock` agent report.

**[P0] Plan §B's OpenSSH claim about `restrict` is incorrect.**
The plan asserts `restrict` disables `environment=` passing unless explicitly
re-enabled. Verified empirically against a live sshd (OpenSSH 8.4): `restrict`
only covers forwarding/PTY/`~/.ssh/rc`; the sole gate on environment delivery
is `PermitUserEnvironment` in sshd_config. `add-annotator.sh` doesn't even use
`restrict` — it uses explicit `no-*` flags. Corrected in the script's comments.
— Source: `plan/b-key-bound-identity` agent report.

**[P1] Launch 2.9 vs arch F.2 — hardened code on the deletion list.**
Task 2.9 added runtime validation to `RemoteManifestEntry.from_dict`
(`ManifestStatus` vocab check). Arch F.2 lists `RemoteManifest`/
`RemoteManifestEntry` for deletion ("the server no longer reads or writes this
file"). Needs a call: accept 2.9's manifest half as temporary work that F.2
deletes, or scope 2.9 down to just the `Ontology.type` half.
— Source: `plan/2.2-2.9-client-fixes` agent report.

**[P1] Behavior tightenings from A.1 that annotators will feel.**
- "Missing required ontology label" unified to **error** (was warning on the
  live path) — partial/empty constrained segmentations now hard-fail integrate
  where they used to pass with a warning.
- `--unconstrained` now actually enforces `unconstrained-v1.yaml` constraints
  (negative/non-sequential labels → failed store) — previously never enforced
  in production.
- RAS-space segmentations now **error** ("re-export as LPS") rather than
  silently converting — the agent's documented choice where the plan offered
  either option; landmarks still auto-convert RAS→LPS.
Needs explicit sign-off before merge/deploy since these change what currently
passes.
— Source: `plan/a1-unified-validation` agent report.

**[P1] New unowned defect: `--checksums` keyed by basename, not path.**
Multi-store staging dirs collide on checksum keys when two stores both name a
file `segmentation.seg.nrrd` (the common case). No task owns this fix today;
arch §C.1's JSON `{path, sha256}` list would fix it structurally — additional
weight toward the JSON-RPC leapfrog on decision 1.
— Source: `plan/2.5-2.8-server-hardening` agent report (worked around in its
own two-store test by varying content, not path).

**[P2] Trunk's own quality gates were red at baseline.**
5 pre-existing `RUF001`/`RUF002` errors (`×` chars, task 0.2) plus a flaky
perf benchmark `test_handrolled_faster_than_pynrrd` (asserts 10x speedup,
trips under CPU contention — no marker/looser bound). Made the plans'
"all green before push" ground rule unsatisfiable repo-wide until 0.2 landed,
and caused the A.1 agent to independently fix the same `×` chars as the
docs-lint agent (identical-hunk overlap, trivial to resolve at merge).
— Source: multiple agent reports (0.1, 0.2/0.3, a1, 2.5-2.8, e2-lock).

**[P2] CLAUDE.md's documented install command fails in fresh checkouts/worktrees.**
`uv sync --group dev` installs only dev-group tools; the workspace root is
`package = false` so members and their runtime deps (numpy/zarr/pynrrd) are
skipped, and uv defaults to Python 3.14 vs the main venv's 3.13. All seven
agents hit this independently; fix is `uv sync --all-packages --group dev`.
Recommend updating CLAUDE.md.
— Source: all seven agent reports.

**[P2] PEP 695 `type` aliases break the `get_args()` pattern launch 2.9 says to copy.**
`get_args()` on a `type`-statement alias returns `()`; the `IssueRecord`
pattern (`get_args(SEVERITY_LEVEL)`) only works because that one is a plain
Literal assignment. Fix used `get_args(ManifestStatus.__value__)`. Relevant to
any future runtime-Literal check under CLAUDE.md's `type`-statement mandate.
— Source: `plan/2.2-2.9-client-fixes` agent report.

**[P2] Deviation accepted: 2.6's `insufficient_memory` is a per-store failure,**
not the plan's implied top-level envelope — consistent with 2.8's per-store
isolation contract. Disk-full remained a top-level envelope as specified.
— Source: `plan/2.5-2.8-server-hardening` agent report.

**[P2] Bonus fix in arch B overlaps launch 6.2.**
`deploy.sh`'s idempotent sshd-config guard only checked for `ForceCommand`, so
re-running deploy.sh on an existing install would silently never add the new
`PermitUserEnvironment` line — identity binding would be inert on upgrades.
Guard tightened as part of the B branch. Launch 6.2 (full config-drift
warning) remains open as a broader version of the same problem.
— Source: `plan/b-key-bound-identity` agent report.

---

## 2026-07-12 — Merging the 7 branches into trunk

**[P1] `validate_seg_preflight` performs the full in-RAM NRRD read that task 2.6's memory
budget was meant to gate — not `parse_seg_nrrd`.**
Merging `plan/a1-unified-validation` (which switched the integrate handler to
`validate_seg_preflight(seg_file, ...)`, a path-based call) with
`plan/2.5-2.8-server-hardening` (which gates `parse_seg_nrrd` on a memory-budget
check) surfaced a real ordering bug: `validate_seg_preflight` calls
`_parse_seg_nrrd_header`, which does `nrrd.read(path)` — a full array
materialization — internally. Under a naive resolution (memory check placed
before the old `parse_seg_nrrd` call, as 2.6's branch alone had it), the OOM
risk 2.6 exists to prevent would occur *inside* `validate_seg_preflight`,
before the budget check ever ran. Resolved by moving the `check_memory_budget`
call to gate `validate_seg_preflight` directly. Neither branch could have
caught this alone — it only exists at the intersection of A.1's refactor and
2.6's guard. Filed as a merge-time fix, not a pre-existing bug in either
branch individually.

**[P2] The seg NRRD is now parsed twice per integrate (efficiency regression from A.1).**
Byproduct of the same intersection: `validate_seg_preflight` reads the full
NRRD once (for validation), then `parse_seg_nrrd` reads it again (for the
write) if validation passes. Pre-A.1, `parse_seg_nrrd` was called once and its
result (`seg_data`) was reused for both validation and the write. Not
incorrect (the two reads are sequential, not concurrent, so peak RAM is
unaffected and the 2.6 budget check still holds) but a real efficiency
regression worth a follow-up — natural candidate for launch 3.1 / arch F.1's
integrate consolidation, which already plans to touch this code path.

**[P0-process] Two branches independently added test classes at the same
insertion point in `test_server_cli.py`, and git's diff3 misaligned their
similar trailing lines into a single confusing conflict block** (each
side's `assert any(...) for e in errors) / assert _written_annotations(...)
== []` tail looked like shared context to the merge algorithm). Resolved by
extracting each complete test class from its source branch tip directly
(bypassing the misleading conflict-marker boundaries) and reassembling by
hand. Lesson: for "both sides added similar-shaped test methods in the same
file region" conflicts, verify against each branch's tip rather than trusting
where git placed the `<<<<<<<`/`=======`/`>>>>>>>` markers.

**[P1] Merging A.1 + 2.8 exposed a real cross-branch test incompatibility
(now fixed).** A.1's `TestUnconstrainedConstraintEnforcement` tests were
written against the exit-code contract *before* 2.8 existed: a single failed
store used to let `_run_integrate_annotations` return normally. Task 2.8
changed the contract so any failed store makes the process `sys.exit(1)`.
All 3 of A.1's constraint tests failed after merging 2.8's branch, with a
clean `SystemExit: 1` — not a logic bug, just two branches independently
correct against different snapshots of the same function's contract. Fixed
by wrapping the now-failing-store assertions in `pytest.raises(SystemExit)`
per the pattern `TestIntegrateChecksumFailClosed` (2.8's own tests) already
established. This is exactly the class of bug the plans' "one task per PR,
small PRs" discipline cannot catch by construction — it only appears when
independently-green branches are combined, which is why the merge step itself
re-ran the full gate suite after every step rather than trusting each
branch's individually-reported green run.

**[P2] `plan/b-key-bound-identity` conflicted with the just-merged 2.7 rollback
wrapper — a clean, expected combination.** Both the segmentation and
landmarks integrate blocks in `server/cli.py` were touched by 2.7 (wraps
`record_provenance` in try/except-rollback) and by B (adds
`identity_source=identity_source` to the same call). Resolved by keeping
2.7's wrapper and adding B's parameter inside it — a mechanical combination,
no design tension. Confirmed `identity_source` is resolved once per handler
via `_resolve_annotator_identity()` before either call site uses it.

**Merge outcome: all 7 branches merged into `trunk`.**
Final state after both real conflicts (`plan/2.5-2.8-server-hardening` in
commit `b895459`, `plan/b-key-bound-identity` in commit `8b15d47`): **671
tests passing**, `ruff check` / `ruff format --check` / `pyright` all green.
The other 5 branches (`0.2-0.3-docs-lint`, `0.1-export-deadlock`,
`2.2-2.9-client-fixes`, `e2-provenance-lock`, `a1-unified-validation`) merged
with zero conflicts. Trunk HEAD is `8b15d47`.

**Follow-ups this merge surfaced, not yet actioned:**
1. Move the memory-budget check + double-parse cleanup (see the two P1/P2
   items above) into the launch 3.1 / arch F.1 integrate-consolidation scope.
2. All P1 items from 2026-07-11 (E.2's tearing rationale, B's `restrict`
   claim, 2.9 vs F.2, A.1's behavior tightenings, the `--checksums` basename
   collision) still need owner review — none were blocking for the merge
   itself since each branch's own tests passed, but they affect what ships.
3. Decision 1 (tactical wrapper fix vs JSON-RPC leapfrog) remains the actual
   launch gate; nothing in this merge wave touched it.

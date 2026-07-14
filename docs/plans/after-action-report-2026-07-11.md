# After-Action Report — Plan Implementation Wave 1 (2026-07-11)

Seven parallel implementation agents were run against
`launch-readiness-implementation-plan.md` and `architecture-improvement-plan.md`,
each in an isolated git worktree, each producing one PR-sized branch off trunk
`cae2d99`. All seven completed with quality gates green (ruff format, pyright,
full pytest incl. slow markers) in their own worktrees. Nothing was pushed or
merged; every branch awaits review.

## 1. What landed (7 branches, 13 commits)

| Branch | Plan tasks | Commits | Diff (files, +/−) | Key outcome |
|---|---|---|---|---|
| `plan/0.1-export-deadlock` | launch 0.1 + 3.4 | 1 | 2, +265/−91 (src net −24) | **Launch blocker fixed**: `voxhub export` default path no longer deadlocks; verified red→green and end-to-end on a real DICOM dir. Export dedup done (`_check_conflicts`, `_write_manifest`, worker reuses `export_zarr`). |
| `plan/0.2-0.3-docs-lint` | launch 0.2 + 0.3 | 2 | 3, +12/−8 | `×`→`x` lint fix; `docs/issues.md` + `docs/deployment-readiness.md` refreshed (incl. two stale refs the plan missed). |
| `plan/2.2-2.9-client-fixes` | launch 2.2 + 2.9 | 2 | 8, +122/−10 | Repeat-pull `PermissionError` fixed (sidecar unlink-before-write); runtime validation for `ManifestStatus` and `Ontology.type`. |
| `plan/a1-unified-validation` | arch A.1 (absorbs launch 2.4) | 1 | 9, +731/−421 | **Single validator in voxhub-schema**; ~200 duplicated lines deleted from `integrate.py`; 2.4 severity fixes (RAS→error, 4D→IssueRecord, undeclared voxels→error) applied once; unconstrained constraints now enforced in production (they never were). Parity test locks server ≡ preflight issue lists. |
| `plan/2.5-2.8-server-hardening` | launch 2.5–2.8 | 4 (one per task) | 3, +769/−68 | Checksums fail-closed; RAM budget + disk-full (≥90%) preconditions on integrate and prepare-pull; provenance-failure rollback with the exists-iff-provenance invariant; no more mid-loop `sys.exit`, per-store statuses always emitted, `reference/` skipped, path-traversal validation, staging cleanup on error paths. |
| `plan/b-key-bound-identity` | arch B | 2 | 7, +476/−6 | Annotator identity bound to the SSH key via `environment="VOXHUB_ANNOTATOR=…"` + scoped `PermitUserEnvironment`; server overrides client flag, `identity_mismatch` on disagreement, `identity_source` stamped in provenance. Verified end-to-end through a real loopback sshd. |
| `plan/e2-provenance-lock` | arch E.2 | 1 | 3, +288/−23 | `provenance_lock` filelock around the JSONL append; tearing test promoted to a hard assertion with deterministic teeth. |

Every branch: failing-test-first where the plan asked for it, conventional
commits, no pushes, one-task-per-PR discipline held (one deliberate exception,
see inconsistency #6).

## 2. Deliberately NOT implemented — blocked on open decisions

These need explicit calls from the project owner (the architecture plan forbids
assuming them):

| Item | Blocking decision | Stakes |
|---|---|---|
| Launch 1.1/1.2 (wrapper + quoting) **vs** arch §C (JSON-RPC leapfrog) | **Decision 1** (recommendation: leapfrog) | **This is the remaining launch gate.** Client↔server transport is still broken in production. Launch 1.3 (rrsync) + 1.4 (sshd e2e suite) are needed either way but need a working wrapper story first. |
| arch A.2 (`--force` must not bypass errors) | Decision 3 (recommendation: warnings-only) | Until decided, a client flag can still land error-laden annotations. |
| arch A.3 (ontology pinning + content hash) | Decision 2 adjacent | Provenance still binds to ontology *names*, not bytes. |
| launch 5.1 (delete client catalog cache) | Team sign-off required by the plan | Also shrinks 2.10 to a fingerprint fix (folded into E.3). |
| arch F.4 (package split) | Decision 4 | — |
| launch 3.1 / arch F.1 (integrate dedup) | Not decision-blocked — **sequencing** | Would have collided with the three concurrent server/cli.py branches; do it after this wave merges (see §4). |
| launch 3.2 (dead-code deletion) | Partially contradicted by §C/F.2 | `PrepareRequest`/`IntegrateRequest` must NOT be deleted if §C ships; rest of the table stands. |
| Launch Phase 4 (client push) | Downstream of decision 1, 3.1, A.2 | Remaining launch blocker #2. |

## 3. Inconsistencies bubbled up for discussion

**Plan factual errors (correct the plan docs):**

1. **Arch §E.2's tearing premise is wrong on local Linux.** `record_provenance`
   issues a single `write()` per record (strace-verified at 328 KB), and
   `O_APPEND` on a local regular file is atomic beyond `PIPE_BUF`; the
   documented tear did not reproduce on the real code path. The lock is still
   justified (NFS, future multi-write records) and landed, but the plan's
   rationale — and the old test docstring's claim — were inaccurate. Code
   comments now state the real exposure.
2. **Arch §B's OpenSSH claim is wrong.** `restrict` does not disable
   `environment=` passing (verified against a live sshd), and
   `add-annotator.sh` uses explicit `no-*` flags, not `restrict`. The only gate
   is `PermitUserEnvironment` in sshd_config. Documented in the script.
3. **Launch 2.9's reference pattern doesn't work here.** `get_args()` on a
   PEP 695 `type`-statement alias returns `()`; the `IssueRecord` pattern the
   plan says to copy only works for plain-assignment Literals. Fixed via
   `ManifestStatus.__value__` — relevant to any future runtime-Literal check
   in this codebase (CLAUDE.md mandates `type` aliases).

**Cross-plan contradictions (needs a call):**

4. **Launch 2.9 hardened code that arch F.2 deletes.** `ManifestStatus`
   validation lives on `RemoteManifestEntry.from_dict` — and
   `RemoteManifest`/`RemoteManifestEntry` are on F.2's deletion list ("the
   server no longer reads or writes this file"). The `Ontology.type` half of
   2.9 survives regardless. Options: accept the manifest half as temporary, or
   drop it when F.2 lands. Also note: the plan's phrase "used by PullManifest"
   was wrong — `PullManifest` has no status field.
5. **Behavior tightenings from A.1 that annotators will feel** (both intended
   by the plans, but worth explicit sign-off before merge):
   - "Missing required ontology label" unified to **error** (was warning on
     the live path) — an empty/partial constrained segmentation now hard-fails
     integrate; `test_multi_annotator_isolation` had to be reworked.
   - `--unconstrained` now actually enforces `unconstrained-v1.yaml`
     constraints (negative labels, non-sequential labels → failed store).
   - RAS segmentations now **error with "re-export as LPS"** rather than being
     silently converted (agent's documented choice where the plan offered
     either; landmarks still auto-convert).

**Process/baseline findings:**

6. **Trunk's own gates were red at baseline**, making the plans' "all green
   before push" ground rule unsatisfiable repo-wide: (a) 5 ruff RUF001/RUF002
   errors from the `×` chars (task 0.2), (b) a flaky perf benchmark
   `test_handrolled_faster_than_pynrrd` (10× assertion, trips under CPU
   contention — needs a `slow`/benchmark marker or looser bound; **unplanned
   item**). Consequence: the A.1 agent also fixed the `×` chars to green its
   gate → identical-hunk overlap with `plan/0.2-0.3-docs-lint` on
   `test_staging.py` (trivial to resolve, see merge order).
7. **CLAUDE.md's install command fails on fresh checkouts/worktrees.**
   `uv sync --group dev` installs only dev tools — the root workspace is
   `package = false`, so members and their runtime deps (numpy/zarr/pynrrd…)
   are skipped, and uv defaults to Python 3.14 (main venv is 3.13). All seven
   agents independently hit this; required: `uv sync --all-packages --group
   dev` (optionally `--python 3.13`). **Recommend fixing CLAUDE.md.**
8. **Pre-existing defect discovered, owned by no task:** `--checksums` entries
   are keyed by file *basename*, so in a multi-store staging dir every store
   shares the `segmentation.seg.nrrd` key — cross-store collision. Arch §C.1's
   JSON `{path, sha256}` list would fix this structurally — a concrete point
   in favor of the leapfrog on decision 1. If tactical is chosen instead, this
   needs its own task.
9. **Deviation to accept or reject (2.6):** integrate's `insufficient_memory`
   is reported as a **per-store failure** (consistent with 2.8's per-store
   isolation) rather than the plan's top-level envelope. Disk-full is a
   top-level envelope as specified.
10. **Bonus fix in B (overlaps launch 6.2):** `deploy.sh`'s idempotency guard
    only checked `ForceCommand`, so upgrades would silently never add
    `PermitUserEnvironment` — identity binding would be inert on existing
    installs. Guard tightened. 6.2 (full config-drift warning) still open.
11. **prepare-pull attribution (B, scope choice):** made attributable via
    structlog `identity_source` rather than new `PullManifest` fields, to keep
    the schema surface and concurrent branches untouched. Revisit if manifest-
    level attribution is wanted.

## 4. Merge-order recommendation

Overlap matrix: `server/cli.py` + `test_server_cli.py` are touched by A.1,
2.5-2.8, and B; `provenance.py` by B and E.2; `test_staging.py` by docs-lint
and A.1 (identical hunk). Suggested sequence (rebase each onto the result of
the previous merges; re-run full gates after each — branches were verified
against trunk individually, not in combination):

1. `plan/0.2-0.3-docs-lint` — unblocks the repo-wide ruff gate for everyone.
2. `plan/0.1-export-deadlock` — disjoint.
3. `plan/2.2-2.9-client-fixes` — disjoint.
4. `plan/e2-provenance-lock` — small; only `provenance.py`/`locks.py`.
5. `plan/a1-unified-validation` — drop its duplicate `×` hunk on rebase.
6. `plan/2.5-2.8-server-hardening` — heaviest `server/cli.py` rebase; its
   integrate-loop rework must reconcile with A.1's validator call sites.
7. `plan/b-key-bound-identity` — last; identity resolution slots on top of the
   hardened integrate loop, and its `provenance.py` edit meets E.2's lock.

After the wave merges: launch 3.1 / arch F.1 step 1 (integrate dedup) becomes
safe and is the prerequisite for Phase 4 (push).

## 5. Immediate next actions

1. **Owner decisions 1–4** — decision 1 (tactical wrapper fix vs JSON-RPC
   leapfrog) is the remaining launch gate; findings #2 and #8 above both add
   weight to the leapfrog recommendation.
2. Review + merge the seven branches per §4.
3. Fix CLAUDE.md's install command (#7); mark/loosen the flaky benchmark (#6).
4. Then: launch 1.3 + 1.4 (± §C), A.2 (after decision 3), 3.1/F.1, Phase 4.

## Appendix — commits per branch

- `plan/0.1-export-deadlock`: `29a2661`
- `plan/0.2-0.3-docs-lint`: `b19c4e6`, `3ddb542`
- `plan/2.2-2.9-client-fixes`: `e1ab3cb`, `d0cc38f`
- `plan/a1-unified-validation`: `12b0a60` (check-diff table in commit body)
- `plan/2.5-2.8-server-hardening`: `1d3e702`, `e6e6abb`, `8594187`, `ff18ca6`
- `plan/b-key-bound-identity`: `de0e9d0`, `dffd4ba`
- `plan/e2-provenance-lock`: `9e01507`

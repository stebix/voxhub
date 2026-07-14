# After-Action Report — Transport Leapfrog + Launch Blockers + Phase 4 Push (2026-07-13/14)

Single supervised session: a four-agent go-live review, the owner's decision 1 call
(JSON-RPC leapfrog), and three implementation waves totalling nine agents. All waves
merged into trunk after per-wave gate verification. Trunk moved
`8b15d47` → Phase 4 merge; suite grew **671 → 837 tests**, all green
(ruff, pyright, full pytest, 10/10 real-sshd e2e).

## 1. What landed, in merge order

| Wave | Branches | Scope | Net effect |
|---|---|---|---|
| Transport (§C + 1.3 + 1.4) | `plan/c1-server-rpc`, `plan/c2-client-rpc`, `plan/c3-13-wrapper-rrsync`, `plan/14-sshd-e2e` via `plan/c-transport-integration` | Protocol v2: `voxhub-server rpc` (JSON-over-stdin, live schema models, path-keyed `ChecksumEntry`), client stdin-RPC (+ strict version check 2.1, timeout policy 2.3), two-branch forced-command wrapper + rrsync `-ro`, real-sshd e2e suite | **Launch blocker #1 (transport) resolved**; checksum basename collision structurally fixed; launch 1.1/1.2 skipped per decision 1 |
| Independent blockers | `plan/independent-blockers` | uv.lock tracked (deploy.sh `--frozen` works) + CLAUDE.md install fix; `--force` can never bypass error-severity issues (A.2, + `forced:true` provenance/attrs stamp); integrate memory gate keys on decompressed NRRD estimate via `nrrd.read_header()`; `backup.sh` + cron + restore drill (6.1) | Fresh-deploy breaker, data-integrity hole, OOM hole, and the backup gap closed |
| Phase 4 (push) | `plan/phase4-push` | Protocol v3: `prepare-push` rpc method; `voxhub push <session-dir>` (--ontology/--unconstrained/--validate-only/--force); writable rrsync + `--no-links` + server-side symlink/path-escape rejection (`invalid_staging_content`); e2e suite extended to 10 tests incl. full pull→annotate→push | **Launch blocker #2 (push) resolved**; the complete annotator loop works over real ssh+rsync+forced-command |

Decision record + pinned wire contract: `docs/plans/c-transport-rpc-implementation-plan.md`.

## 2. Real bugs found by the process (not by unit tests)

1. **Client rsync'd the absolute staging path; rrsync resolves root-relative** — every
   real pull failed (exit 23). Found by the 1.4 sshd suite on its first run; no unit
   or shim test could see it. Fixed (`bcf7a06`, basename addressing); the loopback
   shim now mirrors rrsync's resolution so regressions fail in the cheap suite too.
   This bug is the empirical justification for 1.4's existence.
2. **Symlinked segmentation integrated before the 4.3 defense** — verified red-first:
   a symlink planted in staging via raw rsync was read and integrated. Now refused
   per-store (`invalid_staging_content`) before any staged file is read; symlink scan
   deliberately precedes checksum verification so a correctly-hashing symlink cannot
   launder outside content.

## 3. Deviations and judgment calls needing owner awareness

1. **`--force` residual semantics**: warnings never blocked by default (pre-existing
   behavior, preserved). After A.2, `--force`'s only remaining effect is the
   `forced: true` audit stamp when warnings were accepted. The A.2 done-when (no flag
   combination lands an error-severity annotation) holds. If you want
   warnings-block-by-default, that is a deliberate UX change: client coordination +
   test rework — a candidate next item, not done.
2. **Client push aborts on preflight errors regardless of `--force`** (spec 4.2 said
   "errors abort unless --force") — aligned with the new server semantics; pushing
   would only burn bandwidth toward a guaranteed server-side refusal.
3. **Local manifest**: pull never writes `.voxhub_manifest.json`; push now creates it
   on first success (status `integrated`). Its `pull_session_id` records the *push*
   staging basename, matching the server's provenance stamp (pre-existing server
   naming quirk — the two records reconcile; rename is cosmetic follow-up).
4. **Push discovery is strict**: >1 seg or >1 lmk per store is a hard client error
   (the server's `find_annotation_files` silently picks the first — silent data drop
   avoided client-side; server-side fix is a follow-up, see §5).
5. **`integrate-annotations` moved to the 1800 s client timeout tier** (RAM-parse +
   zarr write under lock; 60 s would kill large pushes).
6. **prepare-push params include optional `annotator_id`** for local/dev parity; the
   key-bound `VOXHUB_ANNOTATOR` remains authoritative over SSH.
7. **Wrapper hardcodes the protocol version literal** in deny() envelopes (now `3`)
   — tests pin it to the schema constant, but a future bump must touch the script.
8. **Legacy per-method shims** remain for one release; the deprecation note now
   straddles v2→v3 (timeline unchanged, wording stale — trivial follow-up).

## 4. Plan-doc corrections discovered

- Arch §C.2 claimed request models have a `serialize()` method — none existed;
  `attrs.asdict` per CLAUDE.md convention is the implemented contract.
- The response models were not merely unwired but *wrong* (`IntegrateResult.
  annotations`, missing `memory_warnings`); making them live required fixing them —
  new `IntegratedAnnotation`/`PreparePushResponse` models.
- Launch 1.1's case table (bare-subcommand allowlist) is superseded by the pinned
  two-branch contract; the old `test_forced_command.py` tested the superseded form.
- Launch 4.1/4.2 pre-date the rpc contract (wrapper allowlist, argv flags,
  `sha256:`-prefixed checksums) — implemented per the supersession.
- `docs/architecture.md` §push still documents the pre-rpc flow (`mktemp`,
  `dt-push-*`) — **stale, needs a rewrite pass** (follow-up).
- Debian bullseye/bookworm ship rrsync gzipped under docs, not as a binary —
  deploy.sh handles all three variants.

## 5. Remaining work (ranked)

**Before go-live:**
1. **Primal go-live test** on a live Hetzner VPS (plan: `docs/testing/go-live-test-plan.md`)
   — the only remaining gate; everything else below is post-go-live hardening.

**Should-fix, post-go-live window:**
2. Server-side audit findings from the 2026-07-13 review still open: store-lock
   `Timeout`/malformed-store escapes break the per-store report contract after
   earlier stores committed; partial store success reports `integrated`/exit 0;
   no provenance↔annotations reconciliation tool (`audit --provenance`);
   `find_annotation_files` silent first-pick; nano-id/machine-id argv validation.
3. Launch 3.1/arch F.1 integrate consolidation (also absorbs the seg double-parse
   efficiency regression) — now unblocked since all waves merged.
4. deploy.sh config-drift warning (6.2); architecture.md push-section rewrite;
   `pull_session_id` naming; warnings-block-by-default decision (see §3.1).
5. Arch A.3 (ontology content-hash pinning), D.1/D.2 (raw-checksum binding, push
   idempotency — D.2 matters once annotators retry failed pushes), Phase 5 catalog
   cache decision (5.1), F.4 package split.

## 6. Process notes

- Wave-1 lessons applied: pinned wire contract before parallel fan-out (zero contract
  drift beyond one test expectation and one intentional bridge collapse); gates re-run
  after every merge, not trusted from per-branch runs; e2e-on-real-infrastructure as
  the wave gate rather than an afterthought.
- All seven implementation/verification agents ran red-first where a defect was named,
  and every wave's worktree gates reproduced on trunk after merge.

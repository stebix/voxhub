# Architecture Improvement Plan

**Audience:** junior/mid-level developer. Each task states *why*, *where*, *what to do*,
*which tests to write*, and *when it is done*. This plan is the architecture-level
companion to `docs/plans/launch-readiness-implementation-plan.md` (bug-level fixes).
Where a task here supersedes or modifies a launch-plan task, that is called out
explicitly — read those notes before starting either plan's task.

**Source:** four-dimension architecture review of trunk `7a8dd13` (2026-07-11):
transport/protocol, storage/provenance/concurrency, ontology/validation, package
boundaries/workflow. File:line references are from that revision — re-verify before
editing.

**Goals this plan serves** (restated from the project owner):

- Vetted I/O access for authorized users (pull volumes, push annotations).
- Ontology-checked pushes with the **server as the authoritative validator**.
- Full provenance for every landed annotation.
- One raw volume → many annotations, keyed by ontology.
- **Immutable, append-only** annotation instances (decided: no revision chains,
  no review states; consumers pick by date/annotator).
- Free browsing + metadata filters as the pull model (no assigned work queues).
- Robust and simple; single small VPS (Hetzner CX22/CX32), 2–5 annotators.

**Standing decisions** (made 2026-07-11):

| Decision | Answer |
|---|---|
| Pull model | Free browsing + filters |
| Trust boundary | Server re-validates at integrate time (authoritative) |
| Annotation lifecycle | Immutable, append-only |
| Transport | SSH retained (recommended; see §C rationale) — HTTPS rejected for now |
| Storage stack | Keep {zarr, JSONL, zarr-attrs}; no database; fix semantics, not tech |

**Open decision points** (get an explicit call from the project owner before starting
the affected task — do not assume):

1. **Sequencing** (affects §C): tactical wrapper fixes first (launch plan 1.1/1.2) vs
   leapfrog directly to the JSON-RPC redesign. Recommendation: leapfrog.
2. **Ontology registry** (affects §A.3/§A.4): `name@version` pinning + content hash
   only (recommended for launch), or also a server-side `get-ontology` RPC now.
3. **`--force` semantics** (affects §A.2): restrict to warnings-only server-side
   (recommended) — means a hard-blocked annotator needs operator help, not
   self-service override.
4. **Package split** (affects §F.4): promote `voxhub_core/server/` to its own
   package vs keep the AST-test-enforced intra-package wall.

**Ground rules for every PR** (same as the launch plan):

- One task (or one checklist item) per PR. Small PRs, reviewed before merge.
- Before pushing: `uv run ruff check packages/ && uv run ruff format --check packages/
  && uv run pyright && uv run pytest -q` — all green.
- Every behavior change ships with a test that fails before the change and passes
  after. Write the test first when you can.
- Follow CLAUDE.md conventions: attrs (not dataclasses), numpy docstrings, single
  quotes, 90-char lines, `type` statement for aliases.
- Tests that spawn subprocesses or sshd get `pytestmark = pytest.mark.slow`.
- Deletion PRs include `grep -rn` output in the description proving zero remaining
  references for each removed symbol.

---

## Priority overview

| § | Improvement | Effort | Depends on | Supersedes / modifies |
|---|---|---|---|---|
| A | Close the validation trust boundary | 4–6 d | — | Redirects launch 2.4 |
| B | Key-bound annotator identity | 1–2 d | — | Feeds launch 4.2 |
| C | JSON-over-stdin RPC + rrsync | 4–6 d | decision 1 | Replaces launch 1.1/1.2; keeps 1.3/1.4 |
| D | Content binding + idempotent push + atomic writes | 3–4 d | — | Extends launch 2.7, 4.x |
| E | Storage semantics (source of truth, audit log) | 2–3 d | — | Extends launch 5.1 |
| F | Structural simplification | 4–6 d | A (for F.2) | Extends launch 3.1/3.2 |

Suggested order: **A.2 and B first** (small, load-bearing for "vetted", independent of
everything else) → **C** (once decision 1 is made) → **A.1/A.3** → **D** → **E** →
**F**. Launch-plan Phase 0 and Phase 2 tasks not mentioned here proceed unchanged in
parallel.

---

## A — Close the validation trust boundary

The server is already the only validator in production — the schema package's
preflight validators (`packages/voxhub-schema/src/voxhub_schema/validation.py`) have
**zero production callers**; the live checks are a second, drifted implementation in
`packages/voxhub-core/src/voxhub_core/integrate.py:58-283`. Three holes make the
boundary defeatable; close them in this order.

### A.1 One validation implementation, owned by voxhub-schema

**Why.** Two validator implementations exist and have already diverged: the schema
path enforces the `unconstrained-v1.yaml` structural constraints
(`non_negative_integers`, `sequential_from_zero`, `background_at_zero`) via
`_check_unconstrained_conformance` (`validation.py:249-286`), but the live server path
skips constraint checks whenever `ontology is None` (`integrate.py:169`) — so those
constraints are **never enforced in production**. Any future fix applied to one copy
silently misses the other. The trust boundary needs exactly one implementation, called
by the server as the authority and (later, launch Phase 4) by the client push for fast
pre-flight feedback.

**Where.** `packages/voxhub-schema/src/voxhub_schema/validation.py` (becomes
canonical), `packages/voxhub-core/src/voxhub_core/integrate.py:58-283` (validators
removed, calls redirected), `packages/voxhub-core/src/voxhub_core/server/cli.py`
(call sites `:659-705`, `:793-797`).

**What.**
1. Diff the two implementations check-by-check. Build a table in the PR description:
   every check present in either implementation, its severity, and which copy has it.
2. Merge the union of checks into `voxhub_schema.validation`, keeping the schema
   package free of zarr imports (it validates parsed NRRD/JSON structures + ontology,
   not stores — pass the volume's spatial metadata in as plain values, which is what
   `integrate.py:83-138` already does).
3. Apply the severity fixes from launch-plan task 2.4 **here, once** (RAS space
   handling, 4D headers, undeclared-voxel-values as errors) — do not fix them in the
   copy that is about to be deleted.
4. Delete `validate_segmentation`/`validate_landmarks` from `integrate.py`; both the
   local `integrate()` and the server handler call the schema validators.
5. Enforce the unconstrained constraints on the live path: when integrating with
   `--unconstrained`, resolve the actual `unconstrained-v1.yaml` ontology and validate
   against its `constraints` instead of passing `None`.

**Tests.** All existing tests in `packages/voxhub-schema/tests/test_validation.py`
and `packages/voxhub-core/tests/test_integrate.py` pass against the unified
implementation (they are the regression net for the merge). New tests: unconstrained
push with a negative label value → error (fails before this task, passes after);
unconstrained push with non-sequential labels → per the constraint list; one
parametrized test asserting the server integrate path and a direct
`validate_seg_preflight` call produce identical issue lists for the same input.

**Done when** `grep -rn 'def validate_segmentation' packages/voxhub-core` returns
nothing, the server imports its validators from `voxhub_schema`, and the
unconstrained-constraint test passes.

**Launch-plan interaction:** supersedes the *location* of 2.4 — the checks 2.4
specifies still get written, but into the unified validator. If 2.4 was already done
in `validation.py`, this task just deletes the core copy.

### A.2 `--force` must not bypass hard errors

**Why.** `force` is a client-supplied flag (`models.py:355`, wired at
`server/cli.py:1342`) and the server's gate is `if errors and not force:`
(`server/cli.py:708` and `:801`) — with `force=True` hard validation errors are logged
and the annotation is **still written with provenance** (`:724-746`, `:817-839`). A
vetting gate the vetted party can switch off is not a gate. (Decision point 3:
recommendation is warnings-only; confirm before starting.)

**Where.** `packages/voxhub-core/src/voxhub_core/server/cli.py:708,801`; mirror the
same rule in local `integrate()` (`integrate.py:649-653`) for workflow parity.

**What.** Split the semantics: `force` (rename to `--accept-warnings` if you touch the
flag name — coordinate with the client CLI) may only suppress **warning**-severity
issues. Error-severity issues always fail the store, regardless of any client flag.
Record accepted warnings in provenance exactly as today (`provenance.py` `issues[]`).
Return a distinct error code (`validation_failed`) so the client can render it.

**Tests** (`test_server_cli.py`): push with a hard error + `--force` → store status
`failed`, **no** annotation group in the zarr store, **no** provenance line (this test
fails before the change); push with warnings only + force → integrated, warnings in
the provenance record; local `integrate(force=True)` behaves identically.

**Done when** no combination of client-supplied flags can land an annotation that has
an error-severity issue.

### A.3 Pin ontology versions on the wire; content-hash them in provenance

**Why.** The wire carries only an ontology *name*; the server resolves "latest version
installed in my `voxhub-schema`" (`server/cli.py:521` → `ontology.py:106-146`). Client
and server pip installs can silently disagree, and once a `-v2.yaml` ships, every new
integrate validates against v2 even for sessions pulled under v1. Provenance records
`ontology_version` as a bare integer — editing a `-v1.yaml` in place is undetectable
downstream. For a provenance system, "which ontology" must mean *which bytes*.

**Where.** `packages/voxhub-schema/src/voxhub_schema/ontology.py`,
`server/cli.py:489-535` (`_resolve_ontologies`), `server/provenance.py:74-107`,
`integrate.py:352-354,419-421` (attrs stamping), the pull-manifest models
(`manifest.py`) and client CLI.

**What.**
1. Accept `--expected-ontology name@version` (keep bare `name` working = latest, for
   one release; log a deprecation warning server-side when unpinned).
2. Resolve via the already-existing `load_ontology(name, version)` — it supports
   pinning today; nothing calls it with a version.
3. Add `content_hash()` to `Ontology` (sha256 of the canonical YAML bytes as loaded).
   Record `ontology_hash` in the provenance JSONL line and the annotation's zarr
   attrs, alongside the existing name+version.
4. Stamp the pull's ontology intent: add `expected_ontologies: list[str]`
   (`name@version` strings) to `PullManifest` written by `prepare-pull`, so the pull
   records what the annotator was told to annotate against. (The push flow, launch
   Phase 4, reads this to pre-select the ontology.)
5. **Deferred (decision point 2):** a `get-ontology` RPC making the server the single
   ontology source. Do not build it in this task; leave a `docs/` note.

**Tests** (`test_ontology.py`, `test_server_cli.py`, `test_server_provenance.py`):
`name@version` parses and resolves the pinned file; unknown version → structured
error; provenance line and zarr attrs both carry a 64-hex `ontology_hash`; two loads
of the same YAML give the same hash, a one-byte edit changes it; `PullManifest`
round-trips `expected_ontologies`.

**Done when** every new provenance line binds the annotation to exact ontology bytes,
and a pinned integrate against an older version passes while the same file exists.

---

## B — Transport-enforced annotator identity

**Why.** All annotator keys share the single `voxhub` OS account
(`server_config.py:22`, `add-annotator.sh:104-112`), and `annotator_id`/`machine_id`
are **flags the client sends about itself** (`server/cli.py:1338-1340`) that flow
straight into provenance (`:733-746`) and the annotator-scoped zarr paths. Any
authorized key can push as any annotator — provenance is currently client-honest, not
enforced. Binding key → annotator makes "who pushed this" cryptographically anchored,
which is the substance of the "vetted access with provenance" goal. Small task, big
win, no new infrastructure.

**Where.** `scripts/deploy/add-annotator.sh`, `scripts/deploy/deploy.sh` (sshd
config), `scripts/deploy/voxhub-forced-command.sh`, `server/cli.py` integrate handler.

**What.**
1. `add-annotator.sh` writes each key line with
   `environment="VOXHUB_ANNOTATOR=<name>"` in the options (in addition to the existing
   `command=` and `restrict` options).
2. `deploy.sh` sshd config: inside the existing `Match User voxhub` block, add
   `PermitUserEnvironment VOXHUB_ANNOTATOR` (scoped allowlist form — never a bare
   `PermitUserEnvironment yes`).
3. The forced command passes the variable through to the server process (it is in the
   environment already; just do not sanitize it away).
4. `_run_integrate_annotations`: read `os.environ.get('VOXHUB_ANNOTATOR')`. If set, it
   **overrides** any client-sent `--annotator-id`; if both are present and disagree,
   fail the request with `code='identity_mismatch'` (do not silently prefer either —
   disagreement means a misconfigured client worth surfacing). If unset (local/dev
   use, loopback tests), fall back to the flag exactly as today. Log which source was
   used, and record it in the provenance line (`identity_source: 'ssh_key' | 'flag'`).
5. Also apply to `prepare-pull` so `pull_session_id` provenance is attributable.

**Tests.** Wrapper test (same harness as launch 1.1): env var survives into the stub
server's environment. `test_server_cli.py`: env set + no flag → provenance carries the
env identity with `identity_source='ssh_key'`; env and flag disagree →
`identity_mismatch`, nothing written; env unset → flag path unchanged. E2e (extends
launch 1.4 suite): an `authorized_keys` line with the environment option → pull's
provenance/log shows the key-bound name.

**Done when** a push over SSH cannot record an `annotator_id` other than the one bound
to the connecting key, and the loopback/local paths still work flag-only.

**Launch-plan interaction:** launch 4.2 (client push) step 7 must send the flag for
dev use but expect the server to override it; note this in the 4.2 PR.

---

## C — Replace argv-as-protocol with JSON-over-stdin RPC + rrsync bulk plane

**⚠ Blocked on decision point 1.** If the call is "tactical first", do launch 1.1/1.2
as written and schedule this section post-launch. If "leapfrog" (recommended), skip
launch 1.1/1.2 entirely and do this instead. Launch 1.3 (rrsync) and 1.4 (sshd e2e
suite) are **required either way** and are unchanged except where noted.

### C.1 Server: a single `rpc` subcommand

**Why.** Today the wire format is bash word-splitting: requests are CLI flags, the
forced command re-execs them unquoted (`voxhub-forced-command.sh:58-60`), composite
values are hand-packed strings split with `entry.split(':', 2)`
(`server/cli.py:592-597`), and the typed request/response models in
`voxhub-schema/models.py` are dead weight — the server hand-builds dicts
(`server/cli.py:467-483`) and the client reads raw keys (`voxhub-client/cli.py:302-305`).
Moving the request body off argv makes shell tokenization, quoting, and glob expansion
structurally impossible (not merely patched), and makes the schema models the *actual*
contract.

**Where.** `packages/voxhub-core/src/voxhub_core/server/cli.py` (new dispatch, argparse
tree shrinks), `packages/voxhub-schema/src/voxhub_schema/models.py` (request/response
models become live).

**What.**
1. Add subcommand `voxhub-server rpc`: read **one** JSON object from stdin —
   `{"protocol_version": N, "method": "<name>", "params": {...}}` — dispatch on
   `method` to the existing `_run_*` handlers, write the usual single JSON response to
   stdout. Unknown method → `code='unknown_method'`. Malformed JSON → structured error,
   never a traceback. Reject a request whose `protocol_version` mismatches (this makes
   version checking bidirectional — today only the client checks).
2. Deserialize `params` through the schema models (`PrepareRequest.from_dict`,
   `IntegrateRequest.from_dict`, …) and serialize responses through their `serialize()`
   counterparts. Missing/invalid fields → the models' documented errors → structured
   envelope. Checksums become a proper JSON list of `{path, sha256}` objects — delete
   the `filename:sha256:hex` string packing.
3. Keep the existing per-method subcommands working for **one release** (operator
   muscle memory + rollout overlap), implemented as thin shims that build the params
   dict and call the same dispatch. Delete them the release after.
4. Method surface for annotators: `list-stores`, `prepare-pull`, `prepare-push`
   (launch 4.1), `integrate-annotations`, `cleanup`, `healthcheck`. Operator-only
   commands (`gc`, `catalog`, `validate-attributes`) stay as plain subcommands and are
   **not** reachable via `rpc` — the forced command only allowlists `rpc`, so
   operator commands are automatically unreachable over annotator SSH.

**Tests** (`test_server_rpc.py`): every method callable via stdin JSON with a
golden-file request/response pair; malformed JSON / unknown method / missing field /
version mismatch → correct envelopes, exit 1, no traceback; a param containing
`"; echo pwned"`, spaces, and `*` round-trips byte-identically (the test that
*motivates* the design); shim subcommands produce identical responses to their rpc
equivalents.

### C.2 Client: pipe JSON, stop building argv

**Where.** `packages/voxhub-client/src/voxhub_client/ssh.py`,
`packages/voxhub-client/src/voxhub_client/cli.py:289-305`.

**What.** `SshRunner.run(method, params)`: remote command is the constant string
`voxhub-server rpc`; the request JSON goes to the subprocess's stdin
(`subprocess.run(..., input=payload)`). Build requests via the schema request models'
`serialize()`; parse responses via the response models' `from_dict()` — delete the
raw-dict key reads. Apply the per-method timeout policy from launch 2.3 here (60 s
default, long/none for `prepare-pull`/`prepare-push`).

**Tests** (`test_ssh.py`): monkeypatched `subprocess.run` asserts the remote command
is exactly `voxhub-server rpc` and the payload arrives on stdin as valid JSON;
injection-shaped params never appear in argv; response missing `protocol_version` →
`RemoteError` (folds in launch 2.1).

### C.3 Forced command: two-branch, ten lines

**Where.** `scripts/deploy/voxhub-forced-command.sh`, `scripts/deploy/deploy.sh`.

**What.** The wrapper becomes: if `SSH_ORIGINAL_COMMAND` starts with `rsync ` →
`exec rrsync <flags> "$STAGING_ROOT"` (launch 1.3, unchanged: `-ro` until push ships,
then writable per launch 4.3 with the symlink defenses); if it equals
`voxhub-server rpc` (accept the bare `rpc` and the `voxhub-server rpc` prefix forms) →
`exec "$VOXHUB_SERVER" rpc`; anything else → forbidden JSON envelope, exit 1. No
tokenization of arguments is ever needed because there are none.

**Tests.** The launch 1.1 test harness (subprocess-driving the real script with a stub
binary) applies verbatim with a smaller case table: rpc allowed, rsync → rrsync stub,
`voxhub-server gc` → forbidden, empty → forbidden. The launch 1.4 sshd e2e suite is the
final gate and needs no changes beyond invoking the new client.

**Done when (whole section)** the launch 1.4 e2e suite passes over real
sshd + forced command + rsync using the rpc path, and `grep -rn 'SSH_ORIGINAL_COMMAND'
scripts/` shows only the two-branch wrapper.

**Launch-plan interaction:** replaces 1.1 and 1.2 (their *tests'* intent — injection
args round-trip as literals — moves into C.1/C.2 tests). 1.3 and 1.4 proceed
unchanged. 2.1 and 2.3 fold into C.2. 3.3's "make the client use
`PrepareResponse.from_dict`" is subsumed by C.2. The launch 3.2 deletion list changes:
`PrepareRequest`/`IntegrateRequest` are **no longer dead** — do not delete them.

---

## D — Bind annotations to content; make push idempotent; write atomically

### D.1 Record the raw-volume checksum in annotation provenance

**Why.** Provenance stores the checksum of the annotation NRRD
(`provenance.py:80`) but never the raw volume's — the raw↔annotation relation is
directory co-location plus a live geometry check (`integrate.py:83-138`). If
`raw/full` is ever re-exported in place, every existing annotation silently re-binds
to different voxel content with no recorded discontinuity. The one-to-many relation
should be a recorded fact, not a filesystem coincidence.

**Where.** `server/cli.py` prepare-pull + integrate handlers, `manifest.py`
(`PullManifest` already carries the raw checksum — verify field name),
`provenance.py:74-107`, `integrate.py:346-356,382-423` (attrs).

**What.** `prepare-pull` already computes and ships the raw sha256 in the pull
manifest. Thread it through push (launch 4.2 sends it back as a param; until push
exists, integrate accepts an optional `--raw-checksum`), and at integrate time:
(a) recompute/read the current raw checksum server-side, (b) if the client-supplied
one is present and differs → fail the store with `code='raw_content_changed'` (the
volume changed since the pull — annotating stale geometry), (c) stamp
`raw_sha256` into the annotation's zarr attrs and the provenance line. Cache the raw
checksum in `raw/full` attrs at export time so integrate doesn't re-hash a 256 MB
array per push; recompute only if the attr is missing.

**Tests** (`test_server_cli.py`, `test_server_provenance.py`): integrate with matching
checksum → attrs + JSONL carry `raw_sha256`; mismatched → `raw_content_changed`,
nothing written; absent (old client) → integrated, provenance records the server-side
value, warning logged.

### D.2 Content-addressed idempotency for push

**Why.** Every integrate mints a fresh random instance suffix
(`server/cli.py:720-722,813-815`), so a retried push lands the same annotation twice
as two unrelated instances — asserted today by
`test_concurrency.py:661-704`. Under the immutable-append-only decision, dedup by
content is the natural fix: retries become safe with zero mutable state.

**Where.** `server/cli.py` integrate handler (inside the per-store lock, before the
write); discovery helpers in `catalog.py` if needed for the lookup.

**What.** Before writing, scan the target store's existing instances for this
annotator (one directory level, cheap) for an instance whose attrs match on all of
`(annotator_id, ontology name, ontology_version, source file sha256, raw_sha256)`.
On match: do **not** write; report the store as `status: 'already_integrated'` with
the existing instance path; append **no** new provenance line (the original one is the
record — log the dedup event to structlog instead). Update
`test_concurrency.py:661-704` to assert the new contract (double push → one
instance).

**Tests**: same file pushed twice → one instance, second response
`already_integrated` with the first path; same annotator, one changed voxel → two
instances (content differs); two *different* annotators, identical file → two
instances (attribution differs). Concurrency: two simultaneous identical pushes →
exactly one instance (the store lock serializes them; the loser sees the winner's
instance).

### D.3 Atomic instance writes (crash consistency)

**Why.** A zarr instance write is many filesystem operations with no journal; a crash
mid-integrate leaves a partial instance that `_discover_annotations`
(`catalog.py:81-95`) will list. Directory rename is atomic on one filesystem — an
instance should exist completely or not at all. This also gives launch 2.7
(provenance-failure rollback) a cleaner mechanism than post-hoc deletion.

**Where.** `integrate.py:309-423` (write functions) and the server handler's
write+provenance block (`server/cli.py:671-861`).

**What.** Write the instance group under a temp name inside the same annotator dir
(e.g. `.tmp-<instance-name>` — dot-prefixed so discovery ignores it; add that ignore
to `catalog.py` discovery), fully populate data + attrs, then `os.rename` to the final
instance name as the **last step before** the provenance append. On any failure before
the rename, `shutil.rmtree` the temp dir. Order inside the store lock becomes:
temp-write → rename → provenance append; if the provenance append fails, remove the
just-renamed dir (launch 2.7's invariant, now with a narrow window instead of a wide
one). Document the invariant in a comment: *an annotation is visible iff its rename
completed; it is legitimate iff its provenance line exists.*

**Tests** (`test_integrate.py`, `test_server_provenance.py`): monkeypatch the attrs
write to raise mid-instance → no instance dir, no `.tmp-` leftover after the handler's
cleanup, discovery sees nothing; monkeypatch `record_provenance` to raise → renamed
dir removed (2.7's test, now passing via this mechanism); a planted stale `.tmp-` dir
is ignored by discovery and removed by `gc`.

**Done when (whole section)** a `kill -9` test (extend `test_concurrency.py`'s
existing SIGKILL harness) at any point during integrate leaves either a complete
instance with provenance or nothing visible.

---

## E — Storage-layer semantics (keep the stack, fix the roles)

Standing decision: keep {zarr, provenance JSONL, zarr attrs}; no SQLite/database now.
Revisit only if one of these triggers fires (record them in `docs/architecture.md`):
store count in the hundreds with visible `list-stores` lag; cross-store analytics or
dashboards; a review workflow needing state queries; non-annotation consumers
(training pipelines, viewers) querying the corpus. If a queryable index is ever added,
the rule is: **derived, disposable, never authoritative** — rebuildable from zarr
attrs by a single command, absence never an error.

### E.1 Declare the source of truth; enrich the audit log

**Why.** The same annotation facts live in three places — array attrs, provenance
JSONL, catalog cache — written by three code paths, with `annotator_id` re-derived a
fourth way from the path slug (`catalog.py:56-129`). Nothing states which wins. And
the JSONL record omits the very things an audit log exists for: checksums, annotation
kind, a real session id (it currently writes a synthetic `dt-push-<ts>` regenerated
per array, so the seg and lmk of one push get *different* session ids —
`provenance.py:92`).

**Where.** `docs/architecture.md` (contract), `server/provenance.py:88-112` (record
shape), `server/cli.py` (thread the push session id through).

**What.**
1. Write the contract into `docs/architecture.md`: **zarr attrs are the source of
   truth for what exists; `provenance.jsonl` is the append-only audit log of what
   happened; the catalog is a disposable derivation of attrs.** All discovery code
   reads attrs; nothing treats the JSONL or catalog as authoritative.
2. Enrich the JSONL record: add `annotation_sha256`, `raw_sha256` (D.1),
   `ontology_hash` (A.3), `kind` (`segmentation`/`landmarks`), `source_file`,
   `identity_source` (B), and a single `push_session_id` shared by every array of one
   integrate invocation (mint one nano-id per invocation; delete the `dt-push-`
   remnant — also fix the `dt-push-` prefix in `ssh.py:223` or delete `mktemp`
   entirely per launch 3.2).
3. These are *additive* fields — old lines stay parseable; any JSONL reader must
   tolerate missing keys (use `.get`).

**Tests** (`test_server_provenance.py`): one push with seg+lmk → both lines share one
`push_session_id`; a line contains every new field; a reader helper handles a
pre-change line without KeyError.

### E.2 Lock the provenance append

**Why.** Cross-store appends to the single global JSONL rely on `O_APPEND` atomicity
under `PIPE_BUF`; `test_concurrency.py:508-562` documents that large records (~24 KB
of issues) can tear. At 2–5 users a small lock is the cheap, correct answer (per-store
JSONL files would also work but are a migration — not now).

**Where.** `server/provenance.py:88-112`, `server/locks.py`.

**What.** Take a `filelock` on `<stores_dir>/.meta/provenance.jsonl.lock` around the
open-write-fsync of the append (same pattern as the store lock, short timeout). Keep
`O_APPEND` + fsync as-is.

**Tests.** Promote the documented-tearing test into an assertion: N processes
appending large records concurrently → every line parses as JSON (fails without the
lock at large record sizes, passes with it). Mark `slow`.

### E.3 Richer `list-stores` metadata for browsing + filters

**Why.** The chosen pull model is free browsing with filters, and the decision is *no
database* — so the filter surface must come from the catalog payload. Today a browsing
annotator cannot see what is already annotated without pulling.

**Where.** `server/catalog_cache.py` payload builder, `catalog.py:56-129`
(discovery), client `cli.py` `list-stores` rendering.

**What.** Ensure each store's catalog entry carries, per annotation: annotator,
ontology name+version, `integrated_at`, kind. Add a per-store summary the client can
filter on: `annotation_count`, distinct `ontologies` list, modality/resolution from
`dataset_attributes`. Client `list-stores` gains `--ontology`, `--annotated/
--unannotated`, `--annotator` filters applied client-side over the returned JSON (no
server changes beyond payload completeness — keep the server dumb). Fix the
fingerprint blindness while in the file: include each store's `annotations/` subdir
mtimes (this is launch 2.10's surviving fragment — do it here, once).

**Tests** (`test_catalog_cache.py`, client tests): payload contains the summary
fields; an integrate followed by a TTL-expired read shows the new annotation (the
2.10 staleness scenario); client filter flags select the right subset of a canned
response.

**Launch-plan interaction:** launch 5.1 (delete the client catalog cache) is
**endorsed — do it first**, then this task touches only the server cache. Launch
2.10 shrinks to the fingerprint fix, done here.

---

## F — Structural simplification

### F.1 One integrate orchestrator (server wraps local, for real)

**Why.** CLAUDE.md rule 1 ("server wrappers call local functions") holds for staging —
`stage()` and `prepare-pull` both route through `extract_volume` — but is violated for
integrate: `_run_integrate_annotations` (`server/cli.py:538-894`, ~356 lines)
re-implements the orchestration of `integrate.py:426-720` nearly verbatim (discovery
loop `:605-616` vs `integrate.py:539-554`; error gating `:708` vs `:649-653`; path
construction `:720-722` vs `:684-687`). Every integrate change is written twice, and
A/D land their logic in one place only if this collapses.

**Where.** `packages/voxhub-core/src/voxhub_core/integrate.py`,
`packages/voxhub-core/src/voxhub_core/server/cli.py:538-894`.

**What.** Two PRs:
1. *Mechanical* (launch 3.1 as written): extract the parameterized
   per-kind helper in each file; no behavior change; full suite green.
2. *Design*: refactor local `integrate()` into a core that accepts resolved
   ontologies + staged files and **returns planned writes + issues** (pure,
   no server params — the hard wall stays intact), plus two thin wrappers: local
   (adds console output) and server (adds store lock, checksum verification, identity,
   provenance, catalog invalidation — the things `voxhub_core/server/` legitimately
   owns). Reconcile the interface drift (local single `ontology` vs server repeatable
   `--expected-ontology`/`--unconstrained`) at the wrapper edge via
   `_resolve_ontologies`, not inside the core. Target: the server handler shrinks to
   \<100 lines of wrapping.

**Tests.** No new behavior: `test_integrate.py`, `test_server_cli.py`,
`test_server_provenance.py`, `test_concurrency.py` pass unchanged — they are the
safety net. Add one parametrized test running the identical scenario through the local
CLI and the server handler and asserting identical instance layout + attrs (locks the
parity promise).

**Done when** integrate validation/path/write logic exists exactly once, and
`server/cli.py`'s handler contains no per-kind branches.

### F.2 Delete the dead cluster; retarget the false-confidence test

**Why.** ~600 src + ~650 test lines tell a story production abandoned:
`RemoteManifest`/`RemoteManifestEntry` ("the server no longer reads or writes this
file", `manifest.py:82-90`), client `manifest.py` (0 production importers), and —
worst — `tests/test_workflow.py`, the repo's headline e2e test, which exercises the
**dead** manifest + preflight path (`test_workflow.py:23-35`), passing green while the
live path broke.

**Where/What.** After A.1 (which decides `validation.py`'s fate — it *stays*, as the
canonical validator):
- Delete `RemoteManifest`, `RemoteManifestEntry`, client `manifest.py`,
  `test_client_manifest.py`. The `ManifestStatus` lifecycle they carried
  (`pulled→pushed→integrated`) is *needed by push* — launch 4.2 re-adds status as a
  field on the live `PullManifest` sidecar instead; note this in the deletion PR.
- Retarget `test_workflow.py` onto the live path (`PullManifest` + unified validators
  + integrate) or delete it in favor of `test_pull_e2e.py`/the future
  `test_push_e2e.py` — decide in review; do not leave it testing the dead path.
- **Amended deletion list vs launch 3.2:** if §C ships, `PrepareRequest`/
  `IntegrateRequest`/response models are live wire contract — keep them. The rest of
  launch 3.2's table stands.

**Tests.** Full suite green; `grep -rn` proof-of-death in the PR description.

### F.3 Right-size the catalog machinery

**Why.** Complexity budget: ~1,800 lines (src+tests, both sides) of version counters,
fingerprints, TTLs, and write-through invalidation to cache a directory listing whose
full rebuild takes "tens of milliseconds" (the cache's own docstring) at this scale.

**What.** In order: (1) launch 5.1 — delete the client cache + `--if-version`
plumbing (endorsed, get the sign-off the launch plan asks for); (2) E.3's fingerprint
fix; (3) *then measure*: log rebuild duration in `catalog refresh`/`list-stores`. If
it stays under ~200 ms at your real store count, open a follow-up PR reducing the
server cache to "rebuild when fingerprint changed" (drop TTL and `catalog_version`).
Do not do step 3 speculatively — the measurement is the gate.

### F.4 (Decision point 4 — do not start unbidden) Physical server/domain split

**Why.** The server/local wall is enforced by a bespoke AST test
(`test_architecture.py:44-79`) rather than a dependency edge, and server-only deps
(`filelock`, `structlog`) live in the domain lib every client of `voxhub-core` drags
in.

**What (if approved).** Move `voxhub_core/server/` to a new `voxhub-server` package
depending on `voxhub-core`; move `filelock`/`structlog` with it; the AST test shrinks
to the core↛client rules. **Lighter alternative** if a fourth package is judged too
much release friction: keep the layout, move server-only deps to a
`[project.optional-dependencies] server` group, and keep the AST test. Either way,
resolve the `voxhub` script-name collision noted in review (both
`voxhub_core.cli` and `voxhub_client.cli` register the same `[project.scripts]` entry
— rename the local one to `voxhub-local`, or document that the two packages must
never share an environment).

---

## Quality gates — the bar for this plan as a whole

Per-PR gates are in each task. These are the plan-level exit criteria:

1. **Trust boundary:** no client-controllable input (flag, env, file content) can land
   an annotation with an error-severity validation issue, and exactly one validator
   implementation exists (A). Verified by the A.2 force test + the A.1 grep.
2. **Attribution:** over SSH, provenance `annotator_id` always comes from the
   connecting key (B). Verified in the sshd e2e suite.
3. **Wire safety:** no request parameter ever passes through shell tokenization
   (C, if approved). Verified by the injection round-trip tests + the ten-line
   wrapper.
4. **Content binding:** every new provenance line carries `raw_sha256`,
   `annotation_sha256`, and `ontology_hash` — an annotation is bound to bytes, not
   names (A.3 + D.1 + E.1).
5. **Idempotency + atomicity:** double push → one instance; SIGKILL at any point
   during integrate → complete instance or nothing (D.2 + D.3). Verified in
   `test_concurrency.py`.
6. **Single source of truth:** discovery and catalog read only zarr attrs; the
   architecture doc states the attrs/JSONL/catalog roles (E.1).
7. **No duplicate orchestrators:** integrate logic exists once; the parity test locks
   local ≡ server layout (F.1).
8. **Line count goes down:** F.2 + F.3 + launch Phase 3/5 together should remove
   more code than A–E add. Track src-line delta per PR in descriptions; if the plan
   ends net-positive in lines, something went wrong — raise it.

## Sequencing at a glance

```
A.2 + B (days, independent) ─► C (after decision 1) ─► A.1 ─► A.3 ─► D ─► E ─► F.1 ─► F.2/F.3
                                │
launch Phase 0 / 2.x (parallel) ┘        launch 1.3 + 1.4 land inside C
                                          launch Phase 4 (push) starts after D
```

Rough total: 3–4 weeks alongside the launch plan's remaining work, with C and F.1 the
two tasks that most benefit from a senior review of the design before coding.

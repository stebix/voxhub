# §C Transport Implementation Plan — JSON-RPC over stdin + rrsync + sshd e2e gate

**Decision record (2026-07-13):** the project owner chose the **leapfrog** on decision 1:
implement arch plan §C (JSON-over-stdin RPC), skipping launch 1.1/1.2 entirely. Launch
1.3 (rrsync) and 1.4 (sshd e2e suite) proceed as specified. Launch 2.1 and 2.3 fold
into the client task. Launch 3.2's deletion list is amended: `PrepareRequest` /
`IntegrateRequest` are live contract, not dead code.

Source specs: `architecture-improvement-plan.md` §C.1–C.3;
`launch-readiness-implementation-plan.md` 1.3, 1.4, 2.1, 2.3. This document pins the
**wire contract** so the three wave-A tasks can be implemented in parallel without
drift. Where this document and the source plans conflict, this document wins; report
the conflict in your final summary rather than silently deciding.

---

## Pinned wire contract (RPC, protocol_version 2)

All three wave-A tasks implement against this contract verbatim.

### Request

One JSON object on the server process's **stdin** (the whole of stdin is one request;
read to EOF, parse once):

```json
{"protocol_version": 2, "method": "prepare-pull", "params": {...}}
```

- `PROTOCOL_VERSION` in `voxhub-schema/models.py` bumps **1 → 2**.
- Remote command is the constant string `voxhub-server rpc` — no other argv ever.
- `params` is the serialized form of the method's schema request model.

### Method surface (annotator-reachable via `rpc`)

| method | params model | response model |
|---|---|---|
| `list-stores` | `{}` (no model) | existing list-stores envelope + `StoreInfo` items |
| `prepare-pull` | `PrepareRequest` | `PrepareResponse` |
| `integrate-annotations` | `IntegrateRequest` | `IntegrateResponse` |
| `cleanup` | `{"staging_dir": str}` | `CleanupResponse` |
| `healthcheck` | `{}` | existing healthcheck envelope |

`prepare-push` is **reserved** for Phase 4 — do not implement, but dispatch must make
adding a method a one-line change. Operator commands (`gc`, `catalog`,
`validate-attributes`) are NOT reachable via `rpc` and stay as plain subcommands.

### Response

Single JSON object on **stdout** (both success and error; nothing structured on
stderr). Every response carries `protocol_version`. Error envelope shape follows the
existing `ServerError` model:

- unknown method → `code='unknown_method'`, exit 1
- malformed / non-JSON / multi-object stdin → `code='malformed_request'`, exit 1,
  never a traceback
- `protocol_version` missing or ≠ server's → `code='protocol_mismatch'`, message
  naming both versions, exit 1 (version checking becomes bidirectional)

### Checksums (replaces `filename:sha256:hex` packing)

`IntegrateRequest.checksums` becomes a list of objects:

```json
[{"path": "<store-dir>/segmentation.seg.nrrd", "sha256": "<64 hex chars>"}]
```

- `path` is **relative to the staging dir**, POSIX separators, resolved server-side
  via `file.relative_to(staging_dir).as_posix()`. This kills the basename-collision
  defect (triage log 2026-07-11, P1).
- Add a small `ChecksumEntry` attrs model in `voxhub-schema/models.py`
  (`path: str`, `sha256: str`; validate 64-char lowercase hex in `from_dict`).
- Delete the `entry.split(':', 2)` parsing and the basename-keyed dict on the server.

### Compatibility shims (one release)

The existing per-method server subcommands (`list-stores`, `prepare-pull`,
`integrate-annotations`, `cleanup`, `healthcheck`) remain working as thin shims: the
argparse handler builds the params dict and calls the **same** dispatch function as
`rpc`. Mark them deprecated in `--help` text. The shims keep the OLD `--checksums`
string format (they are the legacy surface); only the rpc path uses `ChecksumEntry`.
Delete-after-one-release note goes in `docs/issues.md`.

### Forced-command wrapper contract

- `SSH_ORIGINAL_COMMAND` starts with `rsync ` → `exec` **rrsync** (read-only) rooted
  at the staging root.
- `SSH_ORIGINAL_COMMAND` equals `voxhub-server rpc` OR `rpc` → `exec "$VOXHUB_SERVER" rpc`
  (stdin/stdout pass through untouched).
- Anything else → forbidden `ServerError`-shaped JSON envelope on **stdout** (the
  current wrapper wrongly uses stderr), exit 1.
- No tokenization of arguments is ever performed — there are none.

---

## Wave A — three parallel tasks (isolated worktrees, branches off trunk)

### Task A1 — `plan/c1-server-rpc` (voxhub-schema + voxhub-core)

Arch §C.1. **Files:** `voxhub-schema/src/voxhub_schema/models.py` (+ `__init__.py`
exports), `voxhub-core/src/voxhub_core/server/cli.py`, new
`voxhub-core/tests/test_server_rpc.py`.

1. Schema: bump `PROTOCOL_VERSION` to 2; add `ChecksumEntry`; extend
   `IntegrateRequest` to carry `checksums: list[ChecksumEntry]` (and whatever
   integrate params it's currently missing vs the argparse surface — make the model
   match the real handler signature; that's the point of making it live).
2. `voxhub-server rpc` subcommand: read stdin to EOF, parse, validate
   `protocol_version`, dispatch on `method` per the pinned table, deserialize `params`
   through the models' `from_dict` (model errors → structured envelope), call the
   existing `_run_*` handlers, serialize responses through the models. Refactor
   handlers minimally: prefer extracting a `_dispatch(method, params) -> dict` layer
   that both `rpc` and the shims call; do NOT redesign handler internals (identity
   resolution, locking, rollback are freshly hardened — leave them).
3. Checksum verification: replace basename-keyed dict with relative-path keys per the
   pinned contract (rpc path); shims translate the legacy string format into
   `ChecksumEntry` at the boundary so verification code has ONE format internally.
4. Identity binding unchanged: `VOXHUB_ANNOTATOR` env resolution
   (`_resolve_identity`) applies to rpc-dispatched integrate exactly as today;
   `identity_mismatch` refusal must work when `annotator_id` arrives via params.
5. **Tests** (`test_server_rpc.py`): every method callable via stdin JSON with a
   golden request/response pair; malformed JSON / unknown method / missing field /
   version mismatch → correct envelopes, exit 1, no traceback; a param containing
   `"; echo pwned"`, spaces, and `*` round-trips byte-identically; shim subcommands
   produce responses identical to their rpc equivalents; two-store integrate with
   same-basename files and different contents passes checksum verification via
   path-keyed entries (the regression test for the basename defect).

### Task A2 — `plan/c2-client-rpc` (voxhub-client only)

Arch §C.2 + launch 2.1 + 2.3. **Files:** `voxhub-client/src/voxhub_client/ssh.py`,
`cli.py`; tests `test_ssh.py`, `test_pull.py`, loopback shims as needed.

**Constraint: do NOT edit voxhub-schema.** Code against the pinned contract; where the
client needs a model change, use the contract's shapes and note it in your summary
(A1 owns models.py; the integration step reconciles).

1. `SshRunner.run(method: str, params: dict)` sends the constant remote command
   `voxhub-server rpc`, request JSON on stdin (`subprocess.run(..., input=payload)`).
   Delete argv building for RPC. Keep the rsync transfer path (`RsyncTransfer.pull`)
   as-is — bulk data stays on rsync.
2. Build requests via schema request models where they exist; parse responses via
   response models' `from_dict` — delete raw-dict key reads in `cli.py`.
3. Launch 2.1: response missing `protocol_version` → `RemoteError` (today it skips
   the check); also check `PullManifest.protocol_version` on manifest read in
   `_run_pull` with a "re-pull with a current client" message.
4. Launch 2.3: per-method timeouts — 60 s default (`list-stores`, `cleanup`,
   `healthcheck`), 1800 s for `prepare-pull`; catch `TimeoutExpired` in
   `SshRunner.run` → `RemoteError('server command timed out after ...')`.
5. `SshRunner` grows a constructor param for extra ssh options (port, identity file,
   known-hosts overrides) — 1.4 needs it; keep it minimal.
6. Update the loopback shims (`tests/loopback.py`) to speak the rpc contract so the
   existing pull e2e tests keep passing.
7. **Tests**: monkeypatched `subprocess.run` asserts remote command is exactly
   `voxhub-server rpc` and payload arrives as valid JSON on stdin; injection-shaped
   params never appear in argv; missing `protocol_version` → `RemoteError`; timeout
   → `RemoteError`; `prepare-pull` invoked with the long timeout.

### Task A3 — `plan/c3-13-wrapper-rrsync` (scripts/deploy + wrapper tests)

Arch §C.3 + launch 1.3. **Files:** `scripts/deploy/voxhub-forced-command.sh`,
`scripts/deploy/deploy.sh`, `voxhub-core/tests/test_forced_command.py` (rewrite).

1. Rewrite the wrapper to the pinned two-branch contract (rsync→rrsync-ro, rpc→exec,
   else forbidden-on-stdout). Preserve the existing `VOXHUB_ANNOTATOR` /
   `VOXHUB_SERVER_CONFIG` env handling.
2. Staging root: rendered into the wrapper by deploy.sh at install time (chosen
   option; comment why in both scripts). The repo copy of the wrapper reads a
   `VOXHUB_STAGING_ROOT` placeholder/env fallback so tests can run it unrendered.
3. deploy.sh: ensure rrsync is executable at a known path — Debian ships it in the
   rsync package (`/usr/bin/rrsync` on trixie/newer; gzipped under
   `/usr/share/doc/rsync/scripts/` on bullseye/bookworm) — handle both, install to
   `/usr/local/bin/rrsync` if only the doc copy exists. Keep deploy.sh idempotency
   discipline (the PermitUserEnvironment guard pattern).
4. Read-only (`-ro`) rrsync for now; add the launch-plan security note about
   symlinks + Phase 4 writable flip to the wrapper comments.
5. **Tests** (subprocess-drive the real script with stub `voxhub-server`/`rrsync`
   binaries on PATH): `rpc` and `voxhub-server rpc` exec the server with argv
   `['rpc']` and pass stdin through; `rsync --server --sender ...` execs the rrsync
   stub with the staging root; `voxhub-server gc`, bare `prepare-pull`, empty, and
   garbage → forbidden envelope on stdout, exit 1; env vars survive the exec.
   Note: the OLD test file tests the bare-subcommand form — that form is now
   forbidden; rewrite accordingly.

## Wave B — after wave A merges into the integration branch

### Task B1 — `plan/14-sshd-e2e` (final gate)

Launch 1.4, built on the integration branch (all of wave A merged). New
`voxhub-client/tests/test_e2e_sshd.py` + `sshd_loopback` conftest fixture, per the
launch plan's fixture recipe (ssh-keygen'd host/client keys, high port, StrictModes
no, authorized_keys mirroring add-annotator.sh incl. `environment="VOXHUB_ANNOTATOR=…"`,
real `sshd -D`, teardown). Everything `slow`-marked + `skipif` on missing
sshd/ssh-keygen/rsync. Test cases: list-stores end-to-end; full pull end-to-end
(NRRD lands, checksums verify, staging cleaned); forbidden subcommand rejected;
rsync confined to staging (stores_dir refused); pull into a dest with spaces.
Document in `docs/testing/` per convention.

**Done-when for the whole effort:** B1's suite passes over real sshd + forced command
+ rsync using the rpc path; `grep -rn SSH_ORIGINAL_COMMAND scripts/` shows only the
two-branch wrapper; full gates green on the integration branch.

---

## Ground rules (all agents)

- Branch off current trunk (`8b15d47`); conventional commits; one task per branch;
  never push, never merge, never touch trunk.
- Fresh-worktree install: `uv sync --all-packages --group dev` (plain
  `uv sync --group dev` is known-broken in worktrees).
- Gates before declaring done: `uv run ruff format packages/`,
  `uv run ruff check packages/`, `uv run pyright`, `uv run pytest` (full suite).
- Failing-test-first where the spec names a defect (checksum basename regression
  test, wrapper contract tests).
- Deviations, discovered inconsistencies, and anything the spec got factually wrong
  go in your final report — do not silently decide contract-level questions.

# Launch-Readiness Implementation Plan

**Audience:** junior/mid-level developer. Each task states *why*, *where*, *what to do*,
*which tests to write*, and *when it is done*. Work top to bottom — phases are ordered
by dependency, and tasks within a phase are independent unless noted.

**Source:** full-repo review of trunk `7a8dd13` (2026-07-05). Three findings block
launch outright: the SSH forced-command wrapper rejects every client RPC, the client
has no `push` command, and the default `voxhub export` path deadlocks. Everything else
is hardening and complexity reduction.

**Ground rules for every PR:**

- One task (or one checklist item) per PR. Small PRs, reviewed before merge.
- Before pushing: `uv run ruff check packages/ && uv run ruff format --check packages/
  && uv run pyright && uv run pytest -q` — all green.
- Every behavior change ships with a test that fails before the fix and passes after.
  Write the test first when you can.
- Follow CLAUDE.md conventions: attrs (not dataclasses), numpy docstrings, single
  quotes, 90-char lines, `type` statement for aliases.
- Tests that spawn subprocesses or sshd get `pytestmark = pytest.mark.slow` (existing
  convention, see `packages/voxhub-core/tests/test_server_cli_subprocess.py:27`).

---

## Phase 0 — Quick fixes (≈1 day)

### 0.1 Fix the parallel-export deadlock

**Why.** `voxhub export` hangs forever on its *default* path. `export.py:400-406`
passes a plain `multiprocessing.Queue` to `ProcessPoolExecutor` workers. Pickling it
raises `RuntimeError: Queue objects should only be shared between processes through
inheritance`, every future fails at submission, and the progress loop at
`export.py:425-435` blocks on `progress_queue.get()` forever.

**Where.** `packages/voxhub-core/src/voxhub_core/export.py`

**What.** Drop the queue entirely. Iterate `concurrent.futures.as_completed(futures)`
and advance the progress display once per completed store (per-slice progress is not
worth cross-process plumbing). While in the file, also do task 3.4 (export dedup) if
the diff stays reviewable — otherwise separate PR.

**Tests.**
- *Unit/integration* (`test_export.py`): build a tiny synthetic DICOM collection with
  the existing conftest fixtures, run `export_zarr_collection_parallel` with
  `max_workers=2`, wrap in `pytest.timeout` or a `concurrent.futures` deadline of 60 s.
  Assert the zarr stores exist and match the serial path's output (same arrays, same
  manifest). Mark `slow`.
- *Regression guard*: assert the parallel and serial paths produce identical
  `manifest.json` contents for the same input.

**Done when** the new test passes, and `voxhub export` on a real DICOM dir completes
without `--no-parallel`.

### 0.2 Lint cosmetics

Replace the `×` characters in `packages/voxhub-core/tests/test_staging.py:203-221`
with `x`. `uv run ruff check packages/` must report 0 errors. No test needed.

### 0.3 Refresh stale docs

- `docs/issues.md`: the "client cli.py is a stale copy of server cli.py" item is
  resolved — check it off with a note (client CLI is now a real pull implementation).
  Add unchecked items for: client `push` missing; forced-command wrapper rejects all
  client RPCs (link this plan).
- `docs/deployment-readiness.md`: fix the `dt-*` staging-prefix references (the real
  prefix is `vxhb-staging-`, see `server/cli.py`), and the stale `staging.py:313`
  pointer (the `arr[:]` load now lives in `extraction.py:309`). Operators verify
  cleanup against these paths — wrong prefix means they verify the wrong thing.

---

## Phase 1 — Make the transport actually work (≈3-5 days)

This phase is the launch gate. Nothing client↔server works in production today, and
no test can see it because every e2e suite substitutes the SSH layer with local shims
(`packages/voxhub-client/tests/loopback.py`). Task 1.4 builds the harness that keeps
this class of bug from coming back — do 1.1-1.3 first so it has something to pass.

### 1.1 Fix the forced-command wrapper ↔ client contract

**Why.** The client runs `ssh user@host -- voxhub-server prepare-pull ...`
(`packages/voxhub-client/src/voxhub_client/ssh.py:117,163`), so the server sees
`SSH_ORIGINAL_COMMAND="voxhub-server prepare-pull ..."`. The wrapper
(`scripts/deploy/voxhub-forced-command.sh:43`) takes the **first token** (`SUBCMD=
"${CMD%% *}"`) and matches it against bare names (`list-stores`, `prepare-pull`, ...).
First token is `voxhub-server` → every RPC returns `{"code":"forbidden"}`. Even if it
were allowlisted, line 60's `exec "$VOXHUB_SERVER" $CMD` would run
`voxhub-server voxhub-server prepare-pull`.

**Where.** `scripts/deploy/voxhub-forced-command.sh`

**What.**
1. Parse `SSH_ORIGINAL_COMMAND` into a bash array. Use `read -r -a` on a
   whitespace-split — do **not** run it through `eval` or a shell.
2. If the first element is `voxhub-server`, strip it (the wrapper supplies the binary;
   the client keeps sending the prefix so old and new wrappers can coexist during
   rollout).
3. Validate the next element against `ALLOWED_SUBCOMMANDS` exactly as today —
   fail-closed, structured JSON error on stdout, exit 1.
4. `exec "$VOXHUB_SERVER" "${ARGS[@]}"` — quoted array expansion, no word-splitting,
   no glob expansion. This replaces the unquoted `$CMD` (which today also
   glob-expands `*` against the cwd).
5. Keep `gc`, `catalog`, `validate-attributes` **off** the allowlist (operator-only).

**Tests.** New file `packages/voxhub-core/tests/test_forced_command.py`, mark `slow`.
Drive the real script via `subprocess.run(['bash', wrapper_path], env={...})` with a
stub `voxhub-server` (a tiny script that prints its argv as JSON) placed first on
`PATH` / pointed at via `VOXHUB_SERVER`:
- `SSH_ORIGINAL_COMMAND='voxhub-server list-stores'` → stub receives `['list-stores']`.
- `SSH_ORIGINAL_COMMAND='list-stores'` (no prefix) → same result.
- `SSH_ORIGINAL_COMMAND='voxhub-server gc --ttl-hours 0'` → exit 1, stdout is a JSON
  envelope with `"code": "forbidden"`.
- Empty/unset `SSH_ORIGINAL_COMMAND` → forbidden envelope.
- Args survive intact: `'voxhub-server cleanup /tmp/vxhb-staging-abc'` → stub argv is
  exactly `['cleanup', '/tmp/vxhb-staging-abc']`.
- A `*` in an argument arrives literally (no glob expansion).

**Done when** all wrapper tests pass and a manual
`SSH_ORIGINAL_COMMAND='voxhub-server list-stores' bash scripts/deploy/voxhub-forced-command.sh`
against a dev config returns real JSON.

### 1.2 Quote arguments across the SSH boundary

**Why.** `ssh` joins its trailing argv into **one string** for the remote shell.
`SshRunner.run` (`ssh.py:163`) passes args unquoted, so any argument with a space
breaks tokenization — and against a server *without* the ForceCommand wrapper (dev
boxes), it is arbitrary shell injection.

**Where.** `packages/voxhub-client/src/voxhub_client/ssh.py`,
`packages/voxhub-client/src/voxhub_client/transfer.py`

**What.**
- In `SshRunner.run`, apply `shlex.quote()` to every element before building the
  remote command string.
- In `RsyncTransfer`, add `--protect-args` (`-s`) to the rsync argv so the remote
  side does not shell-expand paths, and add `-o BatchMode=yes` to the `-e ssh`
  options so rsync can never hang on an interactive prompt (`transfer.py:27-30`).

**Tests** (`test_ssh.py`, `test_transfer.py` — extend existing files):
- Unit: monkeypatch `subprocess.run`, call `SshRunner.run('cleanup', '/tmp/has space')`,
  assert the captured command string contains `'/tmp/has space'` (quoted) and that a
  `; echo pwned` argument arrives as a single quoted token.
- Unit: assert rsync argv contains `-s` and `BatchMode=yes`.
- Integration: covered by 1.4 (a store path with no spaces still round-trips; spaces
  in dest dir round-trip).

**Done when** injection-shaped args round-trip as literals in the unit tests.

### 1.3 Allowlist rsync safely (rrsync)

**Why.** Even with 1.1 fixed, `RsyncTransfer.pull` makes the server see
`SSH_ORIGINAL_COMMAND="rsync --server --sender ..."` → forbidden. Adding bare `rsync`
to the allowlist is **not** acceptable: `rsync --server` grants read/write anywhere
the voxhub user can reach, including `stores_dir` and `.meta/provenance.jsonl`.

**Where.** `scripts/deploy/voxhub-forced-command.sh`, `scripts/deploy/deploy.sh`

**What.**
1. In the wrapper, recognize a first token of `rsync` and re-exec through **rrsync**
   (ships with the rsync package, usually
   `/usr/share/doc/rsync/scripts/rrsync` or `rrsync` binary on Debian 13) rooted at
   the staging root: `exec rrsync -ro "$STAGING_ROOT"` for now (read-only; Phase 4
   flips to writable for push).
2. Read the staging root from the same server.toml the server uses (parse
   `[storage].staging_dir` with a small `grep`/`sed`, or export it from deploy.sh into
   the wrapper at install time — pick one and comment it).
3. `deploy.sh`: install rrsync if missing (`dpkg -s rsync` already probed; ensure the
   script is executable at a known path), and render the staging root into the wrapper.

**Security review note for the PR description:** rsync `-a` preserves symlinks — a
malicious peer could upload a symlink into staging pointing at `stores_dir`. Read-only
rrsync makes this moot for pull; Phase 4 (push) must re-visit it (see 4.3).

**Tests.**
- Wrapper unit tests (same harness as 1.1): `SSH_ORIGINAL_COMMAND='rsync --server
  --sender ...'` execs rrsync (assert via a stub rrsync that echoes argv); any rsync
  invocation targeting a path outside the staging root is refused by rrsync itself
  (covered in 1.4 with the real binary).
- E2e in 1.4: real `rsync` pull of a staged directory through the wrapper succeeds;
  `rsync` of `$STORES_DIR` through the wrapper fails.

**Done when** a real rsync through the forced command can read staging and nothing else.

### 1.4 Loopback-sshd e2e suite (the regression net)

**Why.** The one test the previous audit asked for and the exact gap that hid 1.1-1.3.
The existing loopback shims deliberately bypass ssh/rsync/sshd; this suite uses all
three for real, on localhost, no root needed.

**Where.** New: `packages/voxhub-client/tests/test_e2e_sshd.py` + a
`sshd_loopback` fixture in `conftest.py`. Mark everything `slow`, and
`pytest.mark.skipif` when `sshd`/`ssh-keygen`/`rsync` are missing
(`shutil.which`).

**What (fixture recipe).**
1. In `tmp_path`: `ssh-keygen -t ed25519 -f host_key -N ''` and a client keypair.
2. Write `sshd_config`: `Port <free high port>`, `ListenAddress 127.0.0.1`,
   `HostKey <host_key>`, `AuthorizedKeysFile <authorized_keys>`,
   `PasswordAuthentication no`, `PidFile <tmp>`, `StrictModes no` (tmpdirs fail the
   ownership checks otherwise).
3. `authorized_keys` line mirrors `add-annotator.sh`:
   `command="<repo>/scripts/deploy/voxhub-forced-command.sh",restrict <client pubkey>`.
4. Set `VOXHUB_SERVER_CONFIG` (via the wrapper env or a test wrapper shim) to a
   generated `server.toml` whose `stores_dir`/`staging_dir` live in `tmp_path`, with a
   small zarr store created by the existing core test helpers
   (`packages/voxhub-core/tests/_core_helpers.py`).
5. Launch `sshd -D -f sshd_config` as the current user via `subprocess.Popen`; poll the
   port; yield host/port; terminate in teardown.
6. Point the client at it: `ssh -p <port> -i <client key> -o
   UserKnownHostsFile=<tmp> -o StrictHostKeyChecking=no` — `SshRunner` needs to accept
   extra ssh options for this (small, test-only constructor param if not present).

**Test cases.**
- `test_list_stores_end_to_end`: `SshRunner.run('list-stores')` through real sshd +
  wrapper returns the seeded store with correct `protocol_version`.
- `test_pull_end_to_end`: full `voxhub pull` (invoke `_run_pull` or the CLI via
  subprocess) → NRRD lands locally, checksums verify, manifest written, server staging
  dir cleaned up.
- `test_forbidden_subcommand_rejected`: raw `ssh ... voxhub-server gc` → forbidden
  envelope.
- `test_rsync_confined`: rsync targeting `stores_dir` through the connection fails;
  targeting the issued staging dir succeeds.
- `test_pull_with_spaces_in_dest`: local dest containing a space works (guards 1.2).

**Done when** the suite passes locally (`uv run pytest -m slow -k sshd`) and is
documented in `docs/testing/` per the existing convention. Budget the most time here —
sshd fixtures are fiddly; expect a day of debugging `sshd -ddd` output.

---

## Phase 2 — Correctness hardening (≈4-6 days, all tasks independent)

### 2.1 Enforce protocol version strictly

**Why.** `ssh.py:196-197` skips the check when the field is **missing** — the exact
failure mode of a wrong/old binary on the pipe. CLAUDE.md rule 4 promises an error.

**What.** Missing `protocol_version` → raise `RemoteError` naming the problem. Also
compare `PullManifest.protocol_version` (`manifest.py:302`) against
`PROTOCOL_VERSION` when a manifest is read in `_run_pull`, with a clear
"re-pull with a current client" message.

**Tests** (`test_ssh.py`, `test_pull.py`): response without the field → `RemoteError`;
mismatched value → `RemoteError` naming both versions; manifest with old version →
clear error, no traceback.

### 2.2 Fix the repeat-pull `PermissionError`

**Why.** `_lock_session` chmods `.voxhub_pull.sha256` to `0o444`
(`voxhub-client/cli.py:104-145`); the next pull to the same dest rewrites the sidecar
via `write_text` on the read-only file → `PermissionError` → misreported as
"session is not fully valid" (`cli.py:347-352`). Refresh pulls are a normal workflow.

**What.** In `_write_trust_sidecar` (`cli.py:100`): if the sidecar exists, `unlink()`
it first (or `chmod(0o644)` then write). Also replace the obfuscated
`stat.S_IREAD | stat.S_IEXEC | 0o055` at `cli.py:143` with a literal `0o555`.

**Tests** (`test_pull.py` / `test_pull_e2e.py`): pull twice into the same dest with
the loopback shims — second pull succeeds and refreshes the sidecar.

### 2.3 Give `prepare-pull` a real timeout policy

**Why.** `ssh.py:137` defaults every call to 60 s; `prepare-pull` stages a full volume
(RAM load + NRRD write + sha256) and will exceed that on large stores. The escaping
`subprocess.TimeoutExpired` is not caught in `_run_pull` (`cli.py:296-300`) → raw
traceback + orphaned server staging dir.

**What.** Per-command timeouts as the architecture doc already specifies: 60 s
`list-stores`/`cleanup`, `None` (or 1800 s) for `prepare-pull`. Catch
`TimeoutExpired` in `SshRunner.run` and re-raise as `RemoteError('server command
timed out after ...')` so the CLI's existing error rendering handles it.

**Tests** (`test_ssh.py`): a stub command that sleeps past a 0.2 s timeout surfaces as
`RemoteError`, not `TimeoutExpired`; `prepare-pull` is invoked with the long timeout
(assert on captured kwargs).

### 2.4 Close the segmentation validation holes

**Why.** Three gaps in `packages/voxhub-schema/src/voxhub_schema/validation.py`, all
behind the documented "exact label set" contract:
1. The NRRD `space` field is never read (`validation.py:29-33`) — a RAS-written
   `.seg.nrrd` is compared numerically against LPS manifest values: wrong error, or a
   silent false pass on near-symmetric volumes. (Landmarks handle RAS at `:408-411`;
   segmentations don't.)
2. Multi-layer 4D Slicer exports (pynrrd yields a NaN row in `space directions`,
   shape `(4,3)`) crash `np.allclose` at `validation.py:145` with a broadcast
   `ValueError` instead of producing an `IssueRecord`.
3. Voxel values undeclared in both header and ontology only **warn**
   (`validation.py:160-172`), and `_check_seg_ontology_conformance:195-246` checks
   header-declared labels only — its `label_map` parameter is dead.

**What.**
1. Read `header['space']`. `left-posterior-superior` → proceed. RAS → either convert
   origin/directions before comparing (mirror the landmark logic) or emit an
   error-severity `IssueRecord` telling the annotator to export LPS — pick one,
   document it in the docstring. Anything else → error `IssueRecord`.
2. Detect 4D/NaN-row headers before `np.allclose`; emit an error `IssueRecord`
   ("multi-layer seg.nrrd not supported — export a single merged segmentation").
   Never let a `ValueError` escape pre-flight.
3. Voxel values absent from the ontology label set → **error** severity. Either use
   `label_map` for the voxel-level check or delete the parameter — no dead params.
   Also check `Ontology.coordinate_system` in `validate_lmk_preflight` (currently
   never read).

**Tests** (`packages/voxhub-schema/tests/test_validation.py`): RAS-header seg →
expected issue/conversion; 4D header → error issue, no exception; voxel value 99
undeclared anywhere → error (not warning); happy path unchanged (existing tests keep
passing).

### 2.5 Make checksum verification fail-closed on the server

**Why.** `server/cli.py:620-626`: a file missing from `--checksums` is silently
unverified; malformed entries are silently dropped (`:592-597`). A client bug disables
the integrity check with no signal.

**What.** Annotation file without a checksum entry, or a malformed entry → per-store
error `IssueRecord` + `status: 'failed'` for that store (do not integrate it).
Log at error level.

**Tests** (`test_server_cli.py`): push a staging dir where one file lacks a checksum →
that store fails with a clear message; malformed `--checksums` token → same.

### 2.6 Integrate-side RAM budget + disk-full precondition

**Why.** The memory budget gates `prepare-pull` (`server/cli.py:399-414`) but
`integrate-annotations` parses pushed NRRDs unguarded (`cli.py:699`). And nothing
refuses work on a nearly-full disk — the torn-provenance scenario.

**What.**
- Before `parse_seg_nrrd` in `_run_integrate_annotations`, run the same
  `memory_budget` check using the NRRD file size as the estimate (cheap proxy; keep
  the segmentation safety factor).
- At the top of `_run_prepare_pull` and `_run_integrate_annotations`:
  `shutil.disk_usage` on the staging root (and stores dir for integrate); ≥90 % used →
  structured error envelope `code='disk_full'`, exit 1, nothing touched.

**Tests** (`test_server_cli.py`): monkeypatch the meminfo reader → integrate refuses
with `insufficient_memory`; monkeypatch `shutil.disk_usage` → both commands refuse
with `disk_full`.

### 2.7 Provenance failure must not strand an unaudited annotation

**Why.** `record_provenance` failure lands in the broad `except Exception`
(`server/cli.py:756,849`) *after* the zarr write committed: annotation exists with no
provenance line, store reports "failed", client retries, duplicates appear.

**What.** In the per-store handler, wrap write+provenance so that if
`record_provenance` raises, the just-created annotation group is deleted (the
instance path is known; the per-store lock is still held) before reporting failure.
Add a comment stating the invariant: *an annotation exists in zarr iff its provenance
line exists.*

**Tests** (`test_server_provenance.py`): monkeypatch `record_provenance` to raise →
store reports failure **and** the annotation group is absent from the zarr store;
happy path still writes both.

### 2.8 Integrate loop robustness (non-atomic multi-store push)

**Why.** A checksum mismatch calls `sys.exit(1)` mid-loop (`server/cli.py:627-637`)
after earlier stores already committed — the response never reports what succeeded,
so the client cannot reconcile.

**What (minimum, not full idempotency).**
- Never `sys.exit` mid-loop: record the store as `status: 'failed'` with its issues
  and continue to the next store.
- After the loop: exit 0 only if all stores integrated; exit 1 if any failed — but
  always emit the full per-store JSON either way.
- Skip the `reference/` staging subdir in store discovery (`server/cli.py:605-617`)
  so pulled reference annotations can never be re-integrated
  (one-line `continue`).
- Validate `--include-existing-annotations` paths against the expected
  `annotations/<slug>/<instance>` shape (relative, exactly three segments, no `..`)
  instead of relying on zarr's accidental key validation (`server/cli.py:272`).
- Clean up the `mkdtemp` staging dir in `prepare-pull`'s error paths
  (`server/cli.py:406-419,451-454`) instead of leaving it for gc.

**Tests** (`test_server_cli.py`): two-store staging dir where store 2 has a bad
checksum → store 1 integrated, store 2 failed, exit 1, both statuses in the JSON;
`reference/` dir present → ignored; `--include-existing-annotations ../../etc` →
`invalid` error; failed prepare-pull leaves no staging dir behind.

### 2.9 Runtime-validate manifest fields

**Why.** `ManifestStatus` is a `Literal` fiction: `manifest.py:55` has
`status=d['status'],  # type: ignore[arg-type]` — any string round-trips.

**What.** In `from_dict`: `if d['status'] not in get_args(ManifestStatus): raise
ManifestError(...)` (copy the pattern from `IssueRecord.from_dict`,
`models.py:155`). Do the same for `Ontology.type` in the schema package.

**Tests**: bad status string → `ManifestError`, not a silent pass.

### 2.10 Catalog cache correctness (only if the cache survives Phase 5)

**Why.** Two related defects:
1. `catalog_version` resets to 1 on any cache rebuild (`server/catalog_cache.py:
   255-264`); the `--if-version` short-circuit is pure equality (`server/cli.py:205`)
   → a client holding pre-reset version N gets a false `unchanged` when the counter
   climbs back to N.
2. `fingerprint()` hashes only top-level store-dir mtime (`catalog_cache.py:149-164`);
   annotation writes don't change it, so a swallowed `invalidate_store` failure
   (`server/cli.py:862-865`) hides new annotations from `list-stores` indefinitely —
   the code comment claiming "TTL + fingerprint reconciles" is wrong.

**What.** Pair the version counter with a random epoch token minted at cache-file
creation (`unchanged` only if epoch **and** version match). Include each store's
`annotations/` subdirectory mtimes in the fingerprint. Fix the comment; log
invalidation failures at error level. Client side: fix the unbounded `unchanged`
recursion (`voxhub-client/catalog_cache.py:285-290` — one retry, then hard error) and
wrap `response['catalog_version']`/`['stores']` access in proper `RemoteError`s.

**Tests** (`test_catalog_cache.py`, `test_client_catalog_cache.py`): rebuild cache,
climb version back to N → client does NOT treat as unchanged; annotation written +
invalidation suppressed → next TTL-expired read still shows it; server that always
answers `unchanged` → client errors after one forced retry instead of
`RecursionError`.

**Decision point:** if Phase 5 removes the client cache and `--if-version`, defects
1 and the recursion disappear — do 5.1 first and shrink this task to the fingerprint
fix.

---

## Phase 3 — Mechanical simplification (≈2-3 days)

Do this **before** implementing push: 3.1 halves the integrate surface push targets.
Every task here is behavior-preserving — the full test suite is the safety net, plus
the listed spot checks. No new features in these PRs.

### 3.1 Deduplicate the seg/landmark twin blocks

**Why.** `server/cli.py:673-767` (segmentation) and `:770-860` (landmarks) are
near-verbatim clones; same twinning in `integrate.py:573-641` (validation) and
`:683-709` (writes), and the two `write_*_to_zarr` functions share their scaffolding
(`integrate.py:309-356` vs `:382-423`). Every integrate change is currently written
twice — and the server handler re-implements the local `integrate()` orchestration
instead of calling it (CLAUDE.md rule 1 says server wrappers *call local functions*).

**What (mechanical step).** Extract one parameterized helper per layer:
`_integrate_one(kind, parse_fn, validate_fn, write_fn, ...)` in `server/cli.py`;
same pattern in `integrate.py`. Target ≈155 lines removed.
**What (follow-up design step, separate PR, discuss first).** Fold the server handler
onto local `integrate()` + a provenance callback (~150 more lines, restores the
rule-1 architecture).

**Tests.** No new behavior: `test_integrate.py`, `test_server_cli.py`,
`test_server_provenance.py`, `test_concurrency.py` must pass unchanged. Add one
parametrized test that runs the same scenario through both kinds (seg + lmk) to lock
the shared path.

### 3.2 Delete dead code (~330 lines incl. tests)

| Delete | Where | Evidence it's dead |
|---|---|---|
| `ScpFallback` | `voxhub-client/transfer.py:98-126` + its tests | Never constructed by production code; scp isn't allowlisted by the wrapper, so it *cannot* work deployed |
| `SshRunner.mktemp` | `voxhub-client/ssh.py:210-229` + tests | Unused; blocked by wrapper; `dt-push-` prefix is dicom-transducer legacy |
| `voxhub_client/manifest.py` | whole module + `test_client_manifest.py` | Pure pass-through wrappers; `cli.py` never imports it; re-add with push if needed |
| `PrepareRequest`, `IntegrateRequest` | `voxhub-schema/models.py:231-238,346-355` | Zero references (requests travel as CLI args) |
| `build_seg_nrrd_header` + `_compute_segment_extent` | `voxhub-core/slicer.py:413-471` + tests | Superseded by `extraction._build_seg_nrrd_header`; only their own unit test uses them |
| `visualization.py` | `voxhub-core` | Imported nowhere; imports matplotlib at module scope, which is not a core dependency — cannot import in prod. Move to a notebook if the plots are wanted |
| `load_dicom_directory` | `voxhub-core/dicom/loading.py` | Exported, unused; `actualize`'s inline `_load_with_progress` duplicates it — keep one |

**Tests.** Deletion PRs: full suite green + `grep -rn` in the PR description showing
zero remaining references for each symbol.

### 3.3 Collapse `from_dict` boilerplate (~100 lines)

**Why.** Ten hand-written `from_dict`s in `models.py`/`manifest.py`, each ~15-30 lines
of `str()/int()` coercion + identical `except (KeyError, TypeError, ValueError)`
wrapping, saturated with `# type: ignore`.

**What.** One module-private helper in `voxhub-schema` (e.g. `_req(d, key, conv)` +
a `coerce_error(cls_name)` context manager). Keep the public `from_dict` classmethod
convention (CLAUDE.md: no cattrs). Rewrite each `from_dict` to use it.
While there: make the client actually **use** `PrepareResponse.from_dict` in
`_run_pull` (`cli.py:302-304` currently reads the raw dict) — 3 lines, turns the
protocol classes into enforcement.

**Tests.** Existing round-trip tests in `test_pull_manifest.py`/schema tests are the
net; add one test per model for a *missing required key* → the model's documented
error (not a raw `KeyError`).

### 3.4 Export module dedup (~45 lines)

The conflict-check + manifest-write block is duplicated verbatim
(`export.py:225-241` vs `:372-388`; `:251-253` vs `:455-457`), and `_worker_fn:282-285`
re-implements `export_zarr:118-121`. Factor `_check_conflicts(output_dir, names)` and
reuse `export_zarr` in the worker. Pairs naturally with 0.1.

### 3.5 Small cleanups (single PR)

- `naming.py`: wrap word lists many-per-line under `# fmt: off` → ~830 lines shrink to
  ~120. Zero behavior change (do NOT change list contents — names are seeded).
  Convert its Google-style docstrings to numpy style.
- `nano_id.py`: body → `''.join(secrets.choice(alphabet) for _ in range(size))` —
  shorter, removes the JS-ism `| 0` no-op, and upgrades to a proper CSPRNG.
- `models.py:81` `Resolution.isotropic`: plain `@property` with
  `math.isclose` tolerance instead of exact float equality + the
  `init=False`/`__attrs_post_init__` dance.
- `server/cli.py`: stamp `'protocol_version': PROTOCOL_VERSION` once inside
  `_write_dict` instead of in all 9 handlers.
- Client `cli.py`: one `_require_config(err_console)` helper for the four identical
  `get_identity()/get_server()` + red-panel + `sys.exit(1)` blocks; catch
  `json.JSONDecodeError`/`KeyError` there too (corrupt config currently tracebacks,
  `cli.py:267-277`).
- `integrate.py:350,417`: record the **actual** source filename in zarr attrs instead
  of hard-coded `'segmentation.seg.nrrd'`/`'landmarks.mrk.json'` (the JSONL provenance
  already records the real name — the two surfaces currently disagree). *This one is a
  behavior change — add an attrs assertion to `test_server_provenance.py`.*
- `dicom/parsing.py:56-73`: move the non-integer-stem check *before* the
  `int(p.stem)` sort so users get the intended error message.
- `server/cli.py` `main()`: parse `--help` before loading settings so help works on a
  misconfigured box.

**Tests.** Full suite + one new test for the source-file attr and one for the DICOM
stem error message.

---

## Phase 4 — Client `push` (the real feature work, ≈1-2 weeks)

**Spec:** `docs/architecture.md` §"push". Server-side `integrate-annotations` exists
and is tested; this phase builds the client half and the transport path for uploads.
Design decision baked in here (flag deviations in the PR): the old `mktemp`-based
push staging is dead — push must use a **server-issued** staging dir, same
confinement as pull.

### 4.1 Server: `prepare-push` subcommand

**What.** Mirror `prepare-pull`'s staging creation: `tempfile.mkdtemp(prefix=
'vxhb-staging-<nanoid>-', dir=staging_root)`, respond
`{protocol_version, staging_dir}`. Add to the wrapper allowlist. Log via structlog.
This is a protocol addition → **bump `PROTOCOL_VERSION`** in voxhub-schema and note
it in the PR (both sides deploy together; the version check now actually enforces
this thanks to 2.1).

**Tests.** Unit (`test_server_cli.py`): response shape, dir exists under staging root
with the prefix, refuses on disk-full (2.6 precondition applies). Wrapper test: the
new subcommand is allowlisted.

### 4.2 Client: `voxhub push <session-dir>`

**What.** Follow the architecture doc flow, adapted to the current pull layout:
1. `get_identity()` — abort with the friendly set-identity message if unconfigured.
2. Read the pull manifest/sidecar from the session dir; verify raw checksum → detect
   stale pulls client-side early.
3. Discover annotation files (`*.seg.nrrd`, `*.mrk.json`), match each to its expected
   ontology from the manifest; error on unmatched files.
4. Pre-flight validation via `voxhub_schema.validation` (now hardened by 2.4);
   `--validate-only` stops here; errors abort unless `--force`.
5. Compute `sha256:` checksums (reuse `_compute_sha256`).
6. `prepare-push` → staging dir; `RsyncTransfer.push` the annotation files (only the
   annotation files, not the raw volume) into it.
7. `integrate-annotations <staging_dir> --annotator-id ... --machine-id ...
   --nano-id ... --checksums ...`; render per-store results with rich (statuses,
   issues); non-zero exit if any store failed (matches 2.8 contract).
8. `cleanup <staging_dir>`; update local manifest status to `integrated` per store.

Keep `_run_push` in the same shape as `_run_pull` (fatal/non-fatal triage, clear
messages) — it is the house style and it is good.

**Tests.**
- *Unit*: annotation discovery (nested dirs, unmatched files, multiple ontologies);
  checksum assembly; `--validate-only` short-circuits before any SSH call
  (monkeypatched runner records zero calls).
- *Integration (loopback shims)*: new `test_push_e2e.py` mirroring
  `test_pull_e2e.py` — full push against the real server CLI via the existing
  `loopback.py` shims: annotation lands at
  `annotations/<annotator>-<nanoid>/<ontology>-<date>-<rand>/`, zarr attrs carry
  full provenance, `.meta/provenance.jsonl` gains exactly one line, manifest status
  flips to `integrated`.
- *Failure paths*: validation error → no SSH calls; checksum mismatch on server →
  clear per-store failure rendered; cleanup failure → warning, not fatal.

### 4.3 Transport: writable rsync for push + symlink defense

**What.** Wrapper: rrsync rooted at the staging root becomes read-**write**
(`rrsync "$STAGING_ROOT"` without `-ro`). Defense against symlink upload (rsync `-a`
preserves symlinks; a symlink in staging pointing at `stores_dir` could fool later
file reads):
- Client: push with `--no-links` (there is no legitimate symlink in a push).
- Server: in `_run_integrate_annotations`, before parsing, reject any staging entry
  where `is_symlink()` is true or `resolve()` leaves the staging dir. Fail the store
  with `code='invalid_staging_content'`.

**Tests.** Server unit: staging dir containing a symlink to a stores file → store
fails, nothing integrated. E2e (4.4): symlink planted in staging via raw rsync →
integrate refuses.

### 4.4 E2e: extend the loopback-sshd suite

Add to `test_e2e_sshd.py`: `test_push_end_to_end` (pull → copy a valid fixture
seg.nrrd into the session → push → assert zarr + provenance on the "server" side),
`test_push_rejects_bad_checksum`, `test_rsync_write_confined_to_staging`. This is the
final launch gate: **pull → annotate (simulated) → push, over real ssh + rsync +
forced command, in CI.**

---

## Phase 5 — Design-change simplification (decide, then ≈1-3 days)

### 5.1 Remove the client catalog cache and `--if-version` plumbing (recommended)

**Why.** ~450 lines (client `catalog_cache.py` 297 + tests + `--if-version` handling
in `server/cli.py`) whose only payoff is response-*byte* saving — the SSH round-trip
always happens (`voxhub-client/catalog_cache.py:243-249` says so itself), and the
server cache's own docstring concedes the full walk takes "tens of milliseconds" at
≤100 stores. Removing it also erases the version-collision bug and the recursion bug
(2.10) outright.

**What.** Client: `list-stores` calls `SshRunner.run` directly; delete
`catalog_cache.py`, its tests, and the cache wiring in `cli.py:19-23,176-184`.
Server: keep `--if-version` accepted-but-ignored for one release (old clients), then
drop. Keep the **server-side** cache for now (it serves `list-stores` cheaply and is
well-built) — revisit only if it causes trouble.

**Get sign-off from the team before this PR** — it deletes working, tested code on
purpose. Link this plan section.

**Tests.** `list-stores` returns fresh data immediately after an integrate (the
staleness scenario from 2.10 becomes trivially correct); client works against a
server still sending `catalog_version` fields.

### 5.2 (Optional, later) Server cache removal

The maximal cut (~900 lines) if operational experience shows the cache isn't earning
its invalidation complexity. Not needed for launch. Requires 5.1 first.

---

## Phase 6 — Ops for go-live (≈1-2 days + operator actions)

### 6.1 Backups — the only non-reproducible data

Annotations + `.meta/provenance.jsonl` cannot be re-created (raw volumes can be
re-exported from DICOM). Ship:
- `scripts/deploy/backup.sh`: nightly `rsync -a --link-dest` snapshot of
  `$STORES_DIR/*/annotations/` and `$STORES_DIR/.meta/` to a second disk/host;
  rotate 14 days; structlog-style JSON line to the log on success/failure.
- Cron entry installed by `deploy.sh` (same idempotent `crontab -l | grep` pattern
  already used for gc).
- Operator checklist item: enable Hetzner snapshots on instance + volume.

**Tests.** A `slow` test that runs `backup.sh` against a tmp stores dir twice and
asserts hardlink dedup + rotation. Manual: restore drill documented in
`docs/deployment-readiness.md`.

### 6.2 deploy.sh config-drift warning

`server.toml` is only written if absent (`deploy.sh:428-438`) — re-running with a
different `--staging-dir`/`--stores-dir` silently keeps the old config while the
summary prints the new paths. Diff the would-be config against the existing file and
`warn` loudly on mismatch. Test: shell-level check in the wrapper test file, or
manual verification noted in the PR.

### 6.3 Go-live smoke test

Re-run the checklist in `docs/deployment-readiness.md` §6 (with the corrected
`vxhb-staging-` prefix), now including: `voxhub push` from a client box, provenance
line verified, backup cron fired once manually, disk-full simulation returns
`code='disk_full'`.

---

## QA summary — the test pyramid after this plan

| Layer | Where | Runs | What it guards |
|---|---|---|---|
| Unit | per-package `tests/` | every PR, `uv run pytest -q` (fast set) | validation rules, protocol models, arg quoting, budgets, wrapper token parsing |
| Integration (loopback shims) | `test_pull_e2e.py`, new `test_push_e2e.py` | every PR | full client↔server control flow minus ssh/rsync |
| Subprocess/concurrency | `test_server_cli_subprocess.py`, `test_concurrency.py` (`slow`) | every PR (CI), locally on demand | entrypoint behavior, cross-process locking, O_APPEND provenance |
| E2e (real sshd + rsync + forced command) | `test_e2e_sshd.py` (`slow`, skipif no sshd) | CI + before every deploy | the transport contract — the layer that was silently broken |
| Ops smoke | deployment-readiness checklist §6 | at go-live and after deploy-script changes | deploy.sh, cron, backups, disk/memory failure envelopes |

**Suggested CI gate:** `ruff check` + `ruff format --check` + `pyright` + `pytest`
(all markers) on every PR; the `slow` e2e suite may be a separate required job.

## Sequencing at a glance

```
Phase 0 (1d) ──► Phase 1 (3-5d) ──► Phase 2 (4-6d) ──► Phase 3 (2-3d) ──► Phase 4 (1-2w) ──► Phase 6 (1-2d) ──► LAUNCH
   quick fixes     transport +          hardening         simplify           client push          ops
                   e2e harness                            (do 3.1 first)
Phase 5 (cache removal) — anytime after Phase 1; doing 5.1 before 2.10 shrinks 2.10.
```

Total: roughly 4-6 weeks for one junior/mid developer with review support, of which
Phase 4 is the only real feature work — everything else is fixing, tightening, and
deleting.

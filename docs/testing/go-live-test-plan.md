# voxhub Primal Go-Live Test Plan

**First real end-to-end validation on live infrastructure, before any production
annotator touches the system.**

One operator, ~half a day. Protocol wire version **3**. This runbook provisions a
real Hetzner VPS with `scripts/deploy/deploy.sh`, drives **every** client action
from a genuine third-party host, and audits the server-local logs and data against
what each action was supposed to produce — all using *synthetic marked data* so
every artifact left on the VPS is attributable to a numbered test step.

This document does not duplicate the deployment audit (`docs/deployment-readiness.md`)
or the loopback-sshd suite (`docs/testing/loopback-sshd-e2e.md`); it references them.
The loopback suite proves the transport works in-process on localhost; this plan
proves it works across three real machines over real sshd + rrsync.

Helper scripts referenced here live under `scripts/golive/`:

| script | runs on | purpose |
|---|---|---|
| `seed-store.sh` | server | seed one synthetic marked zarr store + refresh catalog |
| `make-fixtures.sh` | client | generate marked annotation fixtures (clean/warn/error/huge/lmk) + sha256 manifest |
| `verify-evidence.sh` | server | Phase C audit: provenance ↔ zarr bijection, log events, staging cleanliness, backup |

---

## 1. Scope & exit criteria

### In scope

- Provisioning exclusively via `deploy.sh` + `add-annotator.sh` (§Phase A).
- Every command the client CLI offers (`set-server`, `set-identity`, `whoami`,
  `list-stores`, `pull`, `push` and its flags), happy path **and** adversarial
  (§Phase B).
- Mapping every client action to its server-side evidence: structlog events in
  `/var/log/voxhub/debug.log`, provenance lines in `.meta/provenance.jsonl`, zarr
  annotation groups, staging-dir lifecycle (§Phase C).
- Ops drills: backup + restore, mid-life `deploy.sh` re-run, annotator revocation
  (§Phase D).

### Out of scope

- 3D Slicer itself. Fixtures are generated programmatically by
  `make-fixtures.sh`; they are byte-identical in structure to Slicer exports
  (`.seg.nrrd` / `.mrk.json`) and are what the client validators and server
  integration actually consume.
- Load/soak testing beyond a two-annotator concurrency check.

### Exit criteria — "GO" means ALL of:

1. Every row in the Phase B matrix (§5) reached its expected client exit code and
   produced (or, for adversarial rows, did **not** produce) its expected
   server-side evidence.
2. `scripts/golive/verify-evidence.sh` exits `0`: provenance ↔ zarr bijection holds,
   every expected happy-path and refusal log event is present, the staging root
   is clean, the backup snapshot is complete.
3. **Zero unexplained server-side artifacts.** No annotation group, provenance
   line, or foreign annotator identity exists that is not traceable to a numbered
   step. No leftover `vxhb-staging-*` dir. (Row A5 deliberately leaves one stray
   non-staging entry under the staging root; §Phase C step C7 accounts for it and
   it must be removed before declaring GO.)
4. Phase D drills pass: a restore reproduces the annotations; the `deploy.sh`
   re-run converges to all-`[SKIP]`; a revoked annotator can no longer connect.

Any unmet criterion is **NO-GO**. Record the failing step and stop.

---

## 2. Prerequisites

### 2.1 Manual prerequisites the deploy scripts do NOT cover

`deploy.sh` provisions packages, the `voxhub` user, sshd hardening, the forced
command, the venv, dirs, crons, and the log dir. It does **not** do any of the
following — complete each before Phase A:

1. **Hetzner instance.** CX32 recommended (4 vCPU / 8 GB / 80 GB), Debian 13
   (trixie); CX22 (4 GB) works for the ≤400 MB synthetic store here.
   *(Owner input required: Hetzner account, project, SSH key for the root/sudo
   admin login.)*
2. **Attached data volume**, mounted at `/mnt/storage/voxhub`, present in
   `/etc/fstab`. Verify with `findmnt /mnt/storage/voxhub`. deploy.sh only *warns*
   if the mount is not in fstab (`deploy.sh` Step 8) — it will otherwise silently
   write to the root disk.
3. **DNS or a pinned IP** for the VPS. *(Owner input.)* The client refers to the
   server by host/IP via `voxhub set-server`. A hostname in the admin's
   `~/.ssh/config` is optional but convenient.
4. **Deploy key for the private repo.** `deploy.sh` defaults
   `--repo-url git@github.com:stebix/voxhub.git` (SSH). The `voxhub` service user
   is created with `nologin` and never runs git; **root/the sudo admin runs
   `deploy.sh` and performs the clone**, so the deploy key (a GitHub deploy key or
   the admin's key with repo read) must be on the admin's SSH agent / in
   root's `~/.ssh`. Confirm `sudo git ls-remote git@github.com:stebix/voxhub.git`
   succeeds before Phase A. (Alternatively pass an HTTPS `--repo-url`.)
5. **Firewall.** Allow inbound TCP 22 (or your chosen sshd port) from the
   third-party client host's egress IP. Hetzner Cloud Firewall or `ufw`. No other
   port is needed — voxhub is SSH-only, on-demand.
6. **Admin sudo access** on the VPS for the operator running Phase A/C/D.

### 2.2 Third-party client host (§Phase B)

A machine that is **neither the dev box nor the VPS** — a laptop or a second
cloud instance. Requirements:

- Linux or macOS with `ssh`, `ssh-keygen`, and **`rsync`** on PATH
  (`rsync --version`). The transport is `ssh` + `rsync` (client
  `voxhub_client/transfer.py` invokes `rsync -az [--no-links] -e ssh`).
- **Python 3.12+** and **`uv`** (to run the client from the repo). The repo is
  checked out here purely to run `uv run voxhub …` and `make-fixtures.sh`;
  a production annotator would instead `uv tool install` the client, but for the
  go-live test running from the checkout keeps the client version pinned to the
  commit under test.
- Install the client: `uv sync --package voxhub-client` (this also brings
  `voxhub-schema`, which supplies numpy + pynrrd for `make-fixtures.sh`).
- Two throwaway SSH keypairs (generated in §3).

### 2.3 Time budget

| phase | est. |
|---|---|
| A — provision (incl. prerequisite checks) | 60–90 min |
| B — client matrix (both annotators) | 90–120 min |
| C — evidence audit | 30 min |
| D — ops drills | 45 min |
| **total** | **~4–5 h** |

---

## 3. Synthetic marked data design

Everything created for this test carries a `golive-` marker so it is trivially
greppable and unambiguously attributable.

### 3.1 Two throwaway annotator identities

On the **client host**, generate two fresh keypairs (never reuse a personal key):

```bash
mkdir -p ~/golive-keys
ssh-keygen -t ed25519 -N '' -C golive-alice -f ~/golive-keys/golive-alice
ssh-keygen -t ed25519 -N '' -C golive-bob   -f ~/golive-keys/golive-bob
```

Two annotators exercise the multi-annotator isolation invariant (CLAUDE.md rule 2):
both push to the *same* store and must land in *separate* annotator-scoped groups.

The client stores exactly one identity + one server at a time in
`~/.config/voxhub/{identity.json,server.json}` (`voxhub_client/identity.py`,
`server_config.py`). To act as two annotators from one host, switch identity with
`voxhub set-identity` and select the key per invocation via
`GIT_SSH_COMMAND`-style ssh config. Simplest: give each identity its own SSH host
alias in `~/.ssh/config` on the client:

```
Host golive-alice-vps
    HostName <VPS-IP-or-DNS>
    User voxhub
    IdentityFile ~/golive-keys/golive-alice
    IdentitiesOnly yes
Host golive-bob-vps
    HostName <VPS-IP-or-DNS>
    User voxhub
    IdentityFile ~/golive-keys/golive-bob
    IdentitiesOnly yes
```

Then `voxhub set-server golive-alice-vps` / `golive-bob-vps` selects the key by
picking the host alias. The client always SSHes as the `voxhub` user
(`SERVER_INTERACTION_USER`, fixed); the *key* — not the flag — determines the
recorded identity, because sshd injects `VOXHUB_ANNOTATOR` from the key's
`environment=` option and the server treats it as authoritative
(`server/cli.py::_resolve_annotator_identity`).

> **Identity naming note.** `set-identity <name>` sets the *client-local*
> `annotator_id`. Over SSH it is advisory only — a disagreement with the key-bound
> `VOXHUB_ANNOTATOR` is refused (`identity_mismatch`). Set the client identity name
> to match the key's annotator (`golive-alice` / `golive-bob`) for the happy path;
> row A6 deliberately mismatches them.

### 3.2 Synthetic store

Seeded on the **server** by `scripts/golive/seed-store.sh` (Phase A step A9). One
store named `golive-store-<UTC-date>` (e.g. `golive-store-20260715`), canonical
geometry:

| property | value |
|---|---|
| zarr layout | `raw/full` array, zarr v3 |
| shape | `10 x 12 x 14` |
| dtype | `float32` |
| origin (LPS) | `[-5.0, -6.0, -7.0]` |
| spacing (mm) | `[0.5, 0.5, 0.5]` |
| voxel content | `numpy default_rng(20260713).standard_normal(...)` (deterministic) |

Uncompressed raw is ~6.7 KB — small on purpose. To also exercise the memory path
against a *real* large volume rather than the synthetic huge-header fixture,
optionally seed a second store with a larger shape; not required for GO.

### 3.3 Annotation fixtures with sentinel content

Generated on the **client** by `scripts/golive/make-fixtures.sh <out-dir> <tag>`.
For each annotator tag it emits, with the same geometry as the store and a
sentinel label pattern (single voxels at `[0,0,0]=1`, `[1,1,1]=2`, `[2,2,2]=3`):

| fixture | validates as | sentinel |
|---|---|---|
| `<tag>-clean.seg.nrrd` | **0 issues** vs `inner-ear-structures` | labels named cochlea/vestibule/semicircular_canals |
| `<tag>-warn.seg.nrrd` | **exactly 1 warning** | label 2 renamed `vestibule-golive-warn` (name mismatch) |
| `<tag>-error.seg.nrrd` | **exactly 1 error** | space origin shifted +10 mm in L |
| `<tag>-huge.seg.nrrd` | header declares 4096³ int16 = **128 GiB** decompressed | trips the server memory gate |
| `<tag>.mrk.json` | 0 issues (landmarks) | three LPS points labelled `golive-lmk-{1,2,3}` |

`make-fixtures.sh` also writes `MANIFEST.sha256` — the sha256 of every fixture.
These are the values the client sends as `ChecksumEntry` on push. **Generate the
fixtures once per go-live run and keep `MANIFEST.sha256` with the evidence bundle**
(pynrrd stamps a generation-timestamp comment into each header, so digests differ
between generation runs — the manifest, not a hardcoded constant, is the record).

The validator behavior of the clean/warn/error/huge/lmk fixtures was confirmed
against the real `voxhub_schema` validators and `voxhub_core.slicer` memory
estimator while authoring this plan.

### 3.4 Evidence manifest table (fill in during the run)

Record every expected durable artifact so the audit is a checklist, not a hunt:

| step | annotator | fixture (sha256, from MANIFEST) | expected zarr group | expected provenance line |
|---|---|---|---|---|
| B9  | golive-alice | `<tag>-clean.seg.nrrd` … | `annotations/golive-alice-<nano>/inner-ear-structures-<date>-<rand>/data` | 1 push line, `identity_source=ssh_key`, no `forced` |
| B10 | golive-bob   | `<tag>-clean.seg.nrrd` … | `annotations/golive-bob-<nano>/inner-ear-structures-<date>-<rand>/data` | 1 push line, `identity_source=ssh_key`, no `forced` |
| B12 | golive-alice | `<tag>-warn.seg.nrrd` … | `annotations/golive-alice-<nano>/inner-ear-structures-<date>-<rand>/data` | 1 push line, `forced: true` |
| (adversarial rows leave NO group and NO line) | | | | |

---

## 4. Phase A — Provision

All steps run on the **VPS** as the sudo admin, from the repo checkout's
`scripts/deploy/` directory (deploy.sh requires being run from there — it reads
its sibling wrapper/backup scripts).

**A1.** Confirm §2.1 prerequisites. In particular:
```bash
findmnt /mnt/storage/voxhub            # volume mounted
grep -q /mnt/storage/voxhub /etc/fstab && echo "in fstab" || echo "NOT in fstab — FIX"
sudo git ls-remote git@github.com:stebix/voxhub.git >/dev/null && echo "repo reachable"
```

**A2. Dry-run deploy** (no changes; surfaces misconfiguration):
```bash
sudo ./deploy.sh --stores-dir /mnt/storage/voxhub/data \
                 --staging-dir /mnt/storage/voxhub/staging \
                 --backup-target /mnt/backup/voxhub \
                 --dry-run
```
Expect a clean run ending in the deployment-complete banner. Point
`--backup-target` at a **separate** disk/volume if one is attached; the default
(`<stores-dir parent>/backups`) sits on the same volume and only protects against
bad pushes, not disk failure (deploy.sh Step 11b).

**A3. Real deploy:**
```bash
sudo ./deploy.sh --stores-dir /mnt/storage/voxhub/data \
                 --staging-dir /mnt/storage/voxhub/staging \
                 --backup-target /mnt/backup/voxhub
```
Expect `[OK]`/`[INFO]` through Steps 1–13 and the final banner. The healthcheck
(Step 13) may `[WARN]` "no annotators" — expected on a fresh box.

**A4. Expected post-deploy state** — verify each:

| what | check | expect |
|---|---|---|
| service user | `id voxhub` | exists, `nologin` |
| forced command | `test -x /usr/local/bin/voxhub-forced-command.sh && echo ok` | `ok` |
| rendered staging root | `grep STAGING_ROOT= /usr/local/bin/voxhub-forced-command.sh` | `…:-/mnt/storage/voxhub/staging}` (token replaced) |
| sshd block | `sudo grep -A8 'Match User voxhub' /etc/ssh/sshd_config.d/voxhub.conf` | `ForceCommand …`, `PermitUserEnvironment VOXHUB_ANNOTATOR` |
| server binary | `command -v voxhub-server` | `/usr/local/bin/voxhub-server` |
| server config | `sudo cat /home/voxhub/.config/voxhub/server.toml` | `stores_dir`/`staging_dir` as passed |
| stores/staging dirs | `ls -ld /mnt/storage/voxhub/{data,staging}` | owned `voxhub:voxhub`; staging mode `700` |
| log dir | `ls -ld /var/log/voxhub` | owned `voxhub` |
| gc cron | `sudo crontab -u voxhub -l \| grep 'voxhub-server gc'` | `0 4 * * * … gc --ttl-hours 48` |
| backup cron | `sudo crontab -u voxhub -l \| grep voxhub-backup` | `30 3 * * * …` |
| authorized_keys | `sudo test -f /home/voxhub/.ssh/authorized_keys && echo ok` | `ok` (empty) |

**A5. Add both annotators** (copy the two `.pub` files from the client host, or
paste them):
```bash
sudo ./add-annotator.sh golive-alice /path/to/golive-alice.pub
sudo ./add-annotator.sh golive-bob   /path/to/golive-bob.pub
```
Each prints `[OK] Added annotator '…'`, the fingerprint, and
`Bound id: VOXHUB_ANNOTATOR=<name>`. Verify the binding landed:
```bash
sudo grep -c 'environment="VOXHUB_ANNOTATOR=' /home/voxhub/.ssh/authorized_keys   # -> 2
```

**A6. Healthcheck:**
```bash
sudo ./healthcheck.sh --stores-dir /mnt/storage/voxhub/data
```
Expect all-green except possibly the "no stores" line (still `PASS` — "no stores
found" is healthy). "2 annotator key(s)" should now show.

**A7. Prove `sshd` actually enforces the forced command** (from the client host,
before any client command):
```bash
ssh -i ~/golive-keys/golive-alice voxhub@<VPS> 'echo hi; id'
```
Expect a **JSON `forbidden` envelope** on stdout and exit 1 (the wrapper denies
anything that is not `rsync …` or `rpc`). This is the transport wall working.

**A8. deploy.sh idempotent re-run** (prove convergence):
```bash
sudo ./deploy.sh --stores-dir /mnt/storage/voxhub/data \
                 --staging-dir /mnt/storage/voxhub/staging \
                 --backup-target /mnt/backup/voxhub
```
Expect **every** step to report `[SKIP] … (already done)` (packages, rrsync, uv,
user, sshd config, wrapper via `cmp -s`, stores/staging dirs, config, crons) — the
git step will `[OK] Updated to latest origin/main` (a fetch/reset, benign on an
unchanged tree). No `authorized_keys` change. This confirms re-deploys are safe.

**A9. Seed the synthetic store:**
```bash
sudo /path/to/repo/scripts/golive/seed-store.sh \
     --stores-dir /mnt/storage/voxhub/data \
     --name golive-store-$(date -u +%Y%m%d)
```
Expect `seeded …` and `catalog refreshed`. (The script chowns the store to
`voxhub` and runs `voxhub-server catalog refresh --store …` so it is immediately
visible.)

---

## 5. Phase B — Client-action matrix (from the third-party host)

Run from the **client host**, repo checkout, via `uv run voxhub …`. Generate
fixtures first:
```bash
cd /path/to/repo
uv sync --package voxhub-client
./scripts/golive/make-fixtures.sh /tmp/golive-fixtures/alice golive-alice
./scripts/golive/make-fixtures.sh /tmp/golive-fixtures/bob   golive-bob
```

Columns: **step → command → expected client output / exit → server log event(s) →
durable artifact**. Exit codes: `0` success; `1` handled failure (`sys.exit(1)` /
`RemoteError`). Log events are structlog `event` values in
`/var/log/voxhub/debug.log`; grep them in Phase C.

Set alice as the active identity + server for the happy-path block:
```bash
uv run voxhub set-identity golive-alice
uv run voxhub set-server golive-alice-vps       # host alias from §3.1
```

### Happy-path rows

| # | command | expected client | exit | server log event(s) | durable artifact |
|---|---|---|---|---|---|
| **B1** | `voxhub set-server golive-alice-vps` | `Server configured: voxhub@…` | 0 | *(none — local only)* | `~/.config/voxhub/server.json` |
| **B2** | `voxhub set-identity golive-alice` | `Identity configured: golive-alice` + nano/machine id | 0 | *(none — local)* | `~/.config/voxhub/identity.json` |
| **B3** | `voxhub whoami` | prints identity + server (SSH user `voxhub`) | 0 | *(none — local)* | — |
| **B4** | `voxhub list-stores` | table incl. `golive-store-<date>`, catalog `v<N>` | 0 | `rpc_request` (method `list-stores`), `list_stores_completed` | — |
| **B5** | `voxhub list-stores` (again, warm client cache) | same table | 0 | `rpc_request`, **`list_stores_unchanged`** (client sent `if_version`, server short-circuits) | client cache `~/.cache/voxhub/**/catalog.json` |
| **B6** | `voxhub list-stores --no-cache` | same table | 0 | `rpc_request`, `list_stores_completed` (full payload, no `if_version`) | — |
| **B7** | `voxhub pull --store golive-store-<date> --dest /tmp/golive-pull/alice` | "Pulled …" summary table, trust sidecar digest | 0 | `rpc_request`×N, `prepare_pull_started`, `identity_resolved` (`identity_source=ssh_key`, `annotator_id=golive-alice`), `prepare_pull_completed`, `cleanup_started`, `staging_dir_reaped` (`reason=client_ack`) | session dir with `raw.nrrd`, `.voxhub_pull.json`, `.voxhub_pull.sha256`; **staging dir reaped** |
| **B8** | repeat B7 into the **same** dest (sidecar refresh) | "Pulled …" again; sidecar rewritten | 0 | same as B7 (a second `prepare_pull_completed` + `staging_dir_reaped`) | sidecar re-created (pull re-locks 0444 then refreshes — `_write_trust_sidecar`) |
| **B9** | copy `alice/golive-alice-clean.seg.nrrd` into the session dir, then `voxhub push /tmp/golive-pull/alice --ontology inner-ear-structures` | "Pushing 1 …", `ok`, "push complete" | 0 | `rpc_request`, `prepare_push_started/completed`, `identity_resolved`, `integrate_started` (`identity_source=ssh_key`), `integrate_completed` (`any_failed=false`), `staging_dir_reaped` | **1 zarr group** `annotations/golive-alice-<nano>/…`; **1 provenance line** (`identity_source=ssh_key`, no `forced`) |
| **B10** | switch to bob (`set-identity golive-bob`, `set-server golive-bob-vps`), pull into `/tmp/golive-pull/bob`, copy `bob/golive-bob-clean.seg.nrrd`, `voxhub push /tmp/golive-pull/bob --ontology inner-ear-structures` | "push complete" | 0 | same as B9 with `annotator_id=golive-bob` | **separate** group `annotations/golive-bob-<nano>/…`; **1 more** provenance line — proves multi-annotator isolation (same store, disjoint groups) |
| **B11** | (as alice, fresh pull to `/tmp/golive-pull/alice-warn`) copy `alice/golive-alice-warn.seg.nrrd`, `voxhub push … --ontology inner-ear-structures --validate-only` | `ok`/`1 warning(s)` then `validation passed (--validate-only: stopping)` | 0 | **NONE** (zero SSH contact — validate-only returns before constructing the runner) | **none** (no group, no line) |
| **B12** | same session, `voxhub push … --ontology inner-ear-structures --force` | `1 warning(s) — accepted explicitly (--force is recorded in provenance)`, "push complete" | 0 | `integrate_completed`; the seg accepted with warnings | **1 group + 1 provenance line with `forced: true`** and the warning in `issues[]` |
| **B13** | *(optional)* landmark push: copy `<tag>.mrk.json`, `voxhub push <lmk-session> --ontology inner-ear-landmarks` | "push complete" | 0 | `integrate_completed` | landmark group + provenance line |

> Between B9-style pushes, re-pull for a fresh session dir each time (push verifies
> the session's trust sidecar + raw checksum; a session is single-use per
> annotation). The session dir may contain at most one `.seg.nrrd` and one
> `.mrk.json` (`_discover_annotation_files`).

### Adversarial / negative rows

These prove the transport wall and server refusals. Run from the client host.
`<staging-name>` / `<staging-abs>` below are captured from a prior `prepare-pull`
(read them from a session's `.voxhub_pull.json` or the debug log) where needed.

| # | command | expected client | exit | server evidence | durable artifact |
|---|---|---|---|---|---|
| **A1** | `ssh -i ~/golive-keys/golive-alice voxhub@<VPS> 'voxhub-server gc'` | JSON `forbidden` envelope on stdout | 1 | **no** `rpc_request`; only sshd accept in `journalctl -u ssh`. Wrapper denial is NOT in debug.log (see §C note) | **none** |
| **A2** | `ssh -i …alice voxhub@<VPS> 'list-stores'` (legacy bare subcommand) | JSON `forbidden` envelope | 1 | same as A1 (wrapper only allows `rpc` / `rsync …`) | **none** |
| **A3** | raw rsync **read** of stores: `rsync -e 'ssh -i ~/golive-keys/golive-alice' voxhub@<VPS>:../data/ /tmp/leak/` (or any `..`/absolute path aimed at stores_dir) | rsync error, non-zero | ≠0 | rrsync confines to staging root; path refused | **none** (nothing leaked) |
| **A4** | raw rsync **read** of a store by absolute path `voxhub@<VPS>:/mnt/storage/voxhub/data/` | rsync error | ≠0 | rrsync re-roots/refuses | **none** |
| **A5** | raw rsync **write** outside staging: `rsync -e 'ssh -i …alice' ./evil/ voxhub@<VPS>:../data/` | rsync error | ≠0 | rrsync confines writes to staging root — write never lands in stores_dir | **stray dir may appear directly UNDER the staging root** (rrsync re-roots the `..` to a child of staging); gc never reaps a non-`vxhb-staging-*` name → **must be removed manually** (C7 flags it) |
| **A6** | identity spoof: `uv run voxhub set-identity mallory` then `voxhub push <alice-session> --ontology inner-ear-structures` **using alice's key/server** | `push aborted: … identity_mismatch …` from server | 1 | `identity_resolved` not reached for write; **`identity_mismatch`** (key `golive-alice` vs flag `mallory`) at `prepare_push` or `integrate` | **none** — no group, no line |
| **A7** | tampered checksum: push a session where the `.seg.nrrd` is modified **after** the client computed its checksum. Simplest reproduction: push normally but corrupt the staged copy — use the loopback-suite technique, or push `clean` then swap bytes. Practically: `printf x >> <session>/golive-alice-clean.seg.nrrd` after a successful `--validate-only`, then push | per-store `failed (…)`, `Checksum mismatch …`, push finished with failed store(s) | 1 | `integrate_started`, **`checksum_mismatch`** (store `golive-store-…`), `integrate_completed` (`any_failed=true`) | **none integrated** for that store |
| **A8** | symlink attack via raw rsync: mint a push staging dir (`prepare-push` via a scripted rpc, or reuse one from a real push before cleanup), plant a symlink into `<staging>/golive-store-<date>/` with raw `rsync -a` (bypassing client `--no-links`), then drive `integrate-annotations` for that staging dir | store `failed`, `invalid_staging_content` | 1 | **`invalid_staging_content`** (symlink refused before any staged file is read) | **none integrated** |
| **A9** | legacy/protocol mismatch: send a v2 request. `printf '{"protocol_version":2,"method":"list-stores","params":{}}' \| ssh -i …alice voxhub@<VPS> 'voxhub-server rpc'` | JSON `protocol_mismatch` envelope | 1 | **`rpc_protocol_mismatch`** (`declared=2`) | **none** |
| **A10** | oversized-header push (memory refusal): push a session containing `<tag>-huge.seg.nrrd` (`--unconstrained` to skip ontology, isolating the memory gate) | store `failed`, `Insufficient memory to integrate segmentation …` | 1 | **`integrate_refused_low_memory`** (store `golive-store-…`); `integrate_completed` (`any_failed=true`) | **none integrated** — the header alone (128 GiB decompressed estimate) trips the budget before any voxel parse |

Notes grounding the adversarial rows in code:
- A1/A2: `voxhub-forced-command.sh` only matches `'rsync '*` and `'voxhub-server
  rpc' | 'rpc'`; everything else hits `deny forbidden …`.
- A6: `_resolve_annotator_identity` raises `_IdentityMismatchError` →
  `identity_mismatch` envelope when key-bound `VOXHUB_ANNOTATOR` disagrees with the
  client `annotator_id`.
- A7: `_run_integrate_annotations` computes `compute_sha256` per file and fails the
  store on mismatch (`checksum_mismatch`), never `sys.exit` mid-loop.
- A8: `_find_invalid_staging_entries` refuses symlinks / escaping paths →
  `invalid_staging_content`.
- A9: `_run_rpc` rejects any `protocol_version != 3` → `rpc_protocol_mismatch`.
- A10: `estimate_seg_nrrd_ram_bytes` reads only the header;
  `check_memory_budget` raises `MemoryBudgetError` → `integrate_refused_low_memory`
  (requires the server's `refuse_when_low_memory` policy, the deploy default). On a
  box with ≥128 GiB free this would *not* refuse — the fixture is sized to exceed
  any Hetzner small VPS; confirm the box is a CX22/CX32, not a huge instance.

A6/A8 require driving the RPC surface a little more manually than the happy path
(the client refuses to build a spoofed request for you). If reproducing A8 exactly
is impractical during the live run, the loopback-sshd suite
(`test_symlink_via_raw_rsync_is_refused_by_integrate`) already proves the same
defense on real sshd+rrsync — cite it and mark A8 "covered by CI" rather than
skipping the assertion.

---

## 6. Phase C — Server-side evidence audit

Run on the **VPS**. This is where every client action is matched to its server
evidence. The `verify-evidence.sh` helper automates the bulk; the manual steps
cover what a script cannot assert.

**C1. Run the automated audit:**
```bash
sudo /path/to/repo/scripts/golive/verify-evidence.sh \
    --stores-dir /mnt/storage/voxhub/data \
    --staging-dir /mnt/storage/voxhub/staging \
    --store golive-store-<date> \
    --annotators "golive-alice golive-bob" \
    --expect-provenance 3 \
    --expect-forced 1
```
Adjust `--expect-provenance` to the number of *successful* pushes you ran
(B9 + B10 + B12 = 3 in the matrix above; add B13 if you did the landmark push).
`--expect-forced` = number of `--force`-with-warning pushes (B12 = 1). Expect
`ALL CHECKS PASSED`. The script asserts:
- provenance.jsonl parses; exactly N push lines for the store; all from allowed
  annotators; every line `identity_source=ssh_key`; exactly F `forced: true`; no
  foreign annotator anywhere in the file.
- **zarr annotation groups ↔ provenance lines are a bijection** (no orphan group,
  no unbacked line) — this is the "annotation exists iff its provenance line
  exists" invariant.
- happy-path log events all present (`rpc_request`, `list_stores_completed`,
  `list_stores_unchanged`, `prepare_pull_completed`, `prepare_push_completed`,
  `integrate_completed`, `identity_resolved`, `staging_dir_reaped`).
- refusal events all present (`rpc_protocol_mismatch`, `identity_mismatch`,
  `checksum_mismatch`, `invalid_staging_content`, `invalid_staging_dir`,
  `integrate_refused_low_memory`).
- staging root has no `vxhb-staging-*` leftovers and no stray entries.

Pass `--backup-target /mnt/backup/voxhub` after the Phase D backup run to also
assert the snapshot completeness.

**C2. Negative evidence — adversarial rows left nothing.** The bijection + foreign
annotator checks in C1 already prove no adversarial row created a group or line.
Additionally confirm the *count* is exactly the happy-path count:
```bash
sudo wc -l /mnt/storage/voxhub/data/.meta/provenance.jsonl        # == successful pushes
sudo find /mnt/storage/voxhub/data/golive-store-<date>.zarr/annotations \
     -mindepth 2 -maxdepth 2 -type d | wc -l                       # == same count
```

**C3. Spot-check one provenance line against its zarr attrs** (cross-consistency):
```bash
sudo tail -1 /mnt/storage/voxhub/data/.meta/provenance.jsonl | python3 -m json.tool
# then read the matching group's zarr attrs and confirm annotator_id, ontology,
# source_nrrd_checksum, identity_source (and forced, for B12) agree:
sudo -u voxhub cat "/mnt/storage/voxhub/data/golive-store-<date>.zarr/annotations/<slug>/<instance>/data/zarr.json"
```
The `source_nrrd_checksum` must equal the fixture's `MANIFEST.sha256` value
(prefixed `sha256:`). For the B12 line, `forced: true` must be present in both the
JSONL line and the zarr attrs.

**C4. Confirm `--validate-only` (B11) touched the server not at all.** There should
be no `prepare_push` / `integrate` events attributable to the B11 session, and the
provenance count is unchanged by it. (This is implicit in the exact-count check but
call it out.)

**C5. Identity-source audit.** Every line must be `ssh_key`, never `flag` — the
transport binds identity to the key. A `flag` line would mean sshd is not injecting
`VOXHUB_ANNOTATOR` (a broken `PermitUserEnvironment`), a launch blocker:
```bash
sudo grep -c '"identity_source": "flag"' /mnt/storage/voxhub/data/.meta/provenance.jsonl   # -> 0
```

**C6. GC.** Confirm the gc cron is registered (`sudo crontab -u voxhub -l`). To
exercise it now rather than wait for 04:00, run it manually and confirm it reaps
nothing unexpected (all real sessions were already reaped by client cleanup ACKs):
```bash
sudo -u voxhub env VOXHUB_SERVER_CONFIG=/home/voxhub/.config/voxhub/server.toml \
     voxhub-server gc --ttl-hours 0
# -> {"removed": […], "count": N}; log gains gc_started/gc_completed and one
#    staging_dir_reaped (reason=gc_unacked) per reaped dir.
```
`--ttl-hours 0` reaps every `vxhb-staging-*` dir regardless of age — use it only in
the test window, never leave a 0 TTL cron.

**C7. Account for the A5 stray.** Row A5 (raw rsync write via `..`) can leave a
**non-`vxhb-staging-*`** directory directly under the staging root. gc will never
touch it (it only reaps the prefix). `verify-evidence.sh` flags it as a stray;
inspect and remove it:
```bash
sudo find /mnt/storage/voxhub/staging -mindepth 1 -maxdepth 1 ! -name 'vxhb-staging-*'
# confirm it is the A5 artifact (empty / your ./evil payload), then:
sudo rm -rf /mnt/storage/voxhub/staging/<stray>
```
Re-run C1 — staging checks must now be clean.

> **§C note — wrapper-level refusals are invisible to debug.log.** Rows A1/A2/A3/A4
> are denied by `voxhub-forced-command.sh` / `rrsync` *before* any
> `voxhub-server` process starts, so they produce **no** structlog line. Their
> server-side trace is the sshd connection record (`journalctl -u ssh` /
> `/var/log/auth.log`) plus the **absence** of a corresponding `rpc_request`. The
> plan asserts the absence (exact provenance/group counts) rather than a positive
> log line for these rows. This is a real observability gap — see Findings.

---

## 7. Phase D — Ops drills

**D1. Backup + restore drill.** Run the backup once by hand, then follow the
restore drill in `docs/deployment-readiness.md §6a` (do not duplicate it here):
```bash
sudo -u voxhub /usr/local/bin/voxhub-backup.sh \
     --stores-dir /mnt/storage/voxhub/data --target /mnt/backup/voxhub \
     --log-file /var/log/voxhub/backup.log
grep '"event": "backup_completed"' /var/log/voxhub/backup.log | tail -1
```
Then restore `latest` into a scratch dir and confirm the go-live annotations and
provenance survive (deployment-readiness §6a steps 1–2). Finally re-run C1 with
`--backup-target /mnt/backup/voxhub` to assert snapshot completeness
automatically.

**D2. deploy.sh upgrade re-run mid-life.** Already done as A8 for idempotency; if a
new commit landed on `main` during the test, re-run `deploy.sh` and confirm it
fetches/resets, re-syncs the venv, and re-validates without disturbing
`authorized_keys`, stores, or provenance. A live store + annotations must be
untouched by a redeploy (deploy.sh never writes into `stores_dir` beyond creating
`.meta`).

**D3. remove-annotator + verify revocation.**
```bash
sudo ./remove-annotator.sh golive-bob --force
sudo grep -c 'annotator:golive-bob ' /home/voxhub/.ssh/authorized_keys   # -> 0
```
From the client host, prove bob is locked out:
```bash
ssh -i ~/golive-keys/golive-bob voxhub@<VPS> 'rpc'    # -> Permission denied (publickey)
uv run voxhub list-stores    # as bob's server alias -> ssh_failed / permission denied
```
alice must still work (`voxhub list-stores` as alice → 0). This proves per-key
revocation is immediate (no sshd reload needed — `authorized_keys` is read per
connection).

---

## 8. Teardown / go-decision

### 8.1 Synthetic data disposition — recommendation

**Keep the go-live artifacts as the inaugural audit trail; do NOT wipe.** The
`golive-store-<date>` store, its annotations, and the provenance lines are the
first real records the system produced and are the reference the first production
audit compares against. They are clearly marked (`golive-` prefix everywhere) and
trivially filterable. Two cleanup actions only:

1. Remove the two throwaway annotators once the test is signed off (they are test
   identities, not real annotators): `sudo ./remove-annotator.sh golive-alice`
   (bob already removed in D3). Their *annotations* stay as history.
2. Delete the A5 staging stray if not already done (C7).

If instead a pristine production start is required, `rm -rf` the
`golive-store-<date>.zarr`, delete its provenance lines, and note the removal — but
prefer keeping them.

### 8.2 Go / No-Go checklist

- [ ] Phase A: deploy clean; state table (A4) all-match; A7 forced-command
      enforced; A8 re-run all-`[SKIP]`.
- [ ] Phase B: every happy-path row exit 0 with expected output; every adversarial
      row refused with the expected error/exit.
- [ ] Phase C: `verify-evidence.sh` → `ALL CHECKS PASSED`; provenance ↔ zarr
      bijection holds; identity_source all `ssh_key`; exact counts; A5 stray
      removed.
- [ ] Phase D: restore reproduces annotations; redeploy non-destructive;
      revocation immediate.
- [ ] Zero unexplained artifacts anywhere on the VPS.

All boxes ticked → **GO**. Any unticked → **NO-GO**; record the failing step.

### 8.3 Week-1 monitoring

- `tail -f /var/log/voxhub/debug.log` (or ship to an aggregator) — watch for
  `disk_full`, `insufficient_memory`, `integrate_refused_low_memory`,
  `checksum_mismatch`, `invalid_staging_*`, `identity_mismatch`, `unhandled_exception`.
- Nightly: confirm `backup_completed` in `/var/log/voxhub/backup.log` and that gc
  `count` is small (large counts mean clients aren't ACKing cleanup — a transport
  problem).
- Disk: `df -h /mnt/storage/voxhub`; the server refuses writes at ≥90%
  (`_DISK_FULL_THRESHOLD`), but you want warning long before.
- `journalctl -u ssh` for unexpected auth failures (revoked/rotated keys, probing).
- Memory during the first real (non-synthetic) large-volume pulls — the
  `arr[:]` materialization is the sizing bottleneck (deployment-readiness §2b).

---

## Appendix — server structlog event reference (verified in `server/cli.py`)

Events this plan greps for, with their emitter:

| event | emitted by | meaning |
|---|---|---|
| `rpc_request` | `_run_rpc` | one JSON request accepted (any method) |
| `rpc_protocol_mismatch` | `_run_rpc` | request `protocol_version` ≠ 3 |
| `rpc_malformed_request` | `_run_rpc` | non-JSON / missing method/params |
| `list_stores_completed` / `list_stores_unchanged` | `_run_list_stores` | full payload / `if_version` short-circuit |
| `prepare_pull_started` / `prepare_pull_completed` | `_run_prepare_pull` | pull staging lifecycle |
| `prepare_pull_refused_low_memory` | `_run_prepare_pull` | raw volume exceeds RAM budget |
| `prepare_push_started` / `prepare_push_completed` | `_run_prepare_push` | push staging minted |
| `identity_resolved` | `_resolve_annotator_identity` | records `identity_source` (`ssh_key`/`flag`/`none`) |
| `identity_mismatch` | prepare-pull / prepare-push / integrate | key-bound id ≠ client flag |
| `integrate_started` / `integrate_completed` | `_run_integrate_annotations` | integration batch |
| `checksum_verification_failed` / `checksum_mismatch` | integrate | missing checksum / digest mismatch |
| `invalid_staging_content` | integrate | symlink or escaping path in staging |
| `invalid_staging_dir` | integrate / cleanup | echoed staging_dir outside root or missing prefix |
| `integrate_refused_low_memory` | integrate | pushed seg decompressed size exceeds budget |
| `seg_validation_errors` / `lmk_validation_errors` | integrate | error-severity issues blocked the write |
| `cleanup_started` / `staging_dir_reaped` | cleanup / gc | staging removed (`reason=client_ack` / `gc_unacked`) |
| `gc_started` / `gc_completed` | `_run_gc` | reaper run |
| `disk_full` | prepare-*/integrate | filesystem ≥90% |
| `unhandled_exception` | `main` | any uncaught error → `internal_error` envelope |

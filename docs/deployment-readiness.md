# voxhub — Senior Deployment Readiness Audit (Hetzner small VPS)

**Verdict:** Ship-ready with caveats. The codebase is noticeably more mature than
most "first deploy" projects — SSH hardening is done properly, the protocol is
versioned, provenance is structured, logging is production-grade, and `deploy.sh`
is idempotent. But there are **three concrete issues that should be fixed before
first production push**, and the memory/disk envelope for a small VPS is tighter
than the architecture doc suggests.

Target assumed: **Hetzner CX22 (2 vCPU / 4 GB) or CX32 (4 vCPU / 8 GB), Debian 13,
≤5 annotators.**

---

## 1. What's genuinely good (don't touch)

- **SSH attack surface is tight.** `sshd_config.d/voxhub.conf` pins `ForceCommand`,
  disables PTY/agent/X11/TCP forwarding, and password auth. Per-key restrictions in
  `add-annotator.sh` layer the same via `authorized_keys`. Belt + suspenders.
  (`scripts/deploy/deploy.sh:176-183`, `scripts/deploy/add-annotator.sh:104`)
- **Forced-command whitelist is a hard allow-list**, not a blocklist: `list-stores`,
  `prepare-pull`, `integrate-annotations`, `cleanup`, `healthcheck`. Anything else →
  structured JSON error, exit 1. (`scripts/deploy/voxhub-forced-command.sh:15-44`)
- **stdout/stderr discipline in the server CLI is correct.** Unhandled exceptions
  go to `_write_error()` → structured JSON envelope on stdout, traceback to
  structlog on stderr. The JSON protocol can't be corrupted by a stray `print()`.
  (`packages/voxhub-core/src/voxhub_core/server/cli.py:945-951`)
- **Idempotent deploy.** Re-running `deploy.sh` is safe: `dpkg -s` probes, `cmp -s`
  for the wrapper, `grep -qF` for sshd block, `crontab -l | grep` for cron.
  `sshd -t` validates *before* reload, with rollback on failure
  (`scripts/deploy/deploy.sh:194-201`). This is the level of care we want.
- **Subprocess use is clean.** No `shell=True` anywhere that matters; rsync and ssh
  are invoked with list-form `subprocess.run`. No injection risk in the Python layer.
- **Per-store `filelock` with 60s timeout** scopes contention correctly — different
  annotators touching different stores don't block each other.
- **Log handler is rotating** (`RotatingFileHandler`, 50 MB × 10 = 500 MB cap) with
  configurable stderr level.
  (`packages/voxhub-core/src/voxhub_core/server/logging.py`)
- **GC cron installed** daily at 04:00 with 48h TTL for `vxhb-staging-*` temp dirs.
  (`scripts/deploy/deploy.sh:458`, `_run_gc` in
  `packages/voxhub-core/src/voxhub_core/server/cli.py:954` reaps entries whose
  basename starts with `STAGING_DIR_PREFIX = 'vxhb-staging-'`)

---

## 2. Fix before first production push (deploy-blocking)

### 2a. `prepare-pull --staging-dir` has no path validation — **RESOLVED**

The ``--staging-dir`` flag has been removed entirely from the
``prepare-pull`` surface.  The server is now authoritative over the
staging path: it calls ``tempfile.mkdtemp(dir=settings.storage.staging_dir)``
and returns the chosen path in the response.  Clients never supply
a staging dir.

Operator control over where staging lives is exposed via a new
``[storage].staging_dir`` key in ``server.toml`` (defaults to
``tempfile.gettempdir()``).  ``scripts/deploy/deploy.sh`` accepts a
matching ``--staging-dir`` installer flag and renders it into the
config file.  ``load_settings`` refuses a config where ``staging_dir``
equals ``stores_dir`` as an operator misconfiguration guard.

### 2b. `integrate-annotations` / `prepare-pull` peak memory is not bounded

`packages/voxhub-core/src/voxhub_core/extraction.py:309` does `volume_data = arr[:]` —
the entire raw volume is read into RAM uncompressed so pynrrd can write it as a
contiguous NRRD. Stores are staged sequentially, so peak memory ≈ single largest
store uncompressed. A 512×512×512 `uint16` CT is 256 MB; a 1024³ `float32` is 4 GB.

On a **CX22 (4 GB)** this is the real scaling bottleneck, not CPU. The architecture
doc says "Hetzner 4vCPU" without naming RAM, but the code behavior means:

| VPS          | Safe max single-store uncompressed size |
|--------------|------------------------------------------|
| CX22 (4 GB)  | ~400 MB                                  |
| CX32 (8 GB)  | ~1.5 GB                                  |
| CX42 (16 GB) | ~4 GB                                    |

**Fix (minimum):** add a guard in `stage()` that checks `arr.nbytes` against
available memory via `psutil.virtual_memory().available`, and fail fast with a
clear error instead of OOM-killing the SSH session. **Fix (proper):** stream the
NRRD write with chunked reads — the NRRD "raw" encoding supports it and pynrrd
isn't the only way to write one.

### 2c. No staging-dir confinement on `integrate-annotations` and `cleanup` — **RESOLVED**

``integrate-annotations`` and ``cleanup`` still accept ``staging_dir``
as a positional argument — inherent, since both reference a dir the
server previously issued — but they now funnel every client-echoed
path through ``_validate_echoed_staging_dir``.  That helper rejects
paths which, after ``.resolve()`` (follows symlinks), either fall
outside ``settings.storage.staging_dir`` or lack the
``vxhb-staging-`` prefix.  Rejected calls exit 1 with an
``invalid_staging_dir`` error envelope and never touch disk, so
``cleanup ~/.ssh`` / ``cleanup /mnt/storage/voxhub/data`` are
mechanically impossible even for an SSH principal bypassing the
voxhub-client.  Covered by ``TestValidateEchoedStagingDir``
and ``TestCleanup::test_refuses_path_{without_staging_prefix,outside_staging_root}``.

---

## 3. Should-fix before real annotators start (high-priority)

- **No backup strategy anywhere in the repo or deploy scripts.** — **RESOLVED**
  (launch plan 6.1). Annotations are the only non-reproducible artifact on this
  system (raw volumes can be re-exported from DICOM sources).
  `scripts/deploy/backup.sh` now takes a nightly `rsync -a --link-dest` hardlink
  snapshot of `$STORES_DIR/.meta/` + `$STORES_DIR/*/annotations/` into a
  configurable target (default `<stores-dir parent>/backups`; point it at a
  second disk/volume or `host:/path` via `deploy.sh --backup-target` /
  `VOXHUB_BACKUP_TARGET`), rotates snapshots after 14 days
  (`--retention-days`), and appends one structlog-style JSON line per run to
  `/var/log/voxhub/backup.log`. `deploy.sh` installs the 03:30 cron
  idempotently. Hetzner snapshots on instance + volume remain an operator
  action — checklist §6. Restore drill: §6a.

- **No fsync/error handling on `.meta/provenance.jsonl` writes.** If the disk fills
  mid-push, the provenance line may tear. Provenance is the audit log — torn lines
  should be fatal, not silently continue. Wrap the JSONL append in a fsync'd
  try/except that aborts the integrate on failure.

- **Healthcheck doesn't fail on low disk.** `healthcheck.sh` warns at 75% and fails
  at 90%, but nothing *blocks new pulls* on a full disk. Add a disk-usage
  precondition at the top of `_run_prepare_pull()` that rejects with
  `code=disk_full` at ≥90%. Cheap, stops the class of failures where a pull
  half-fills the disk and the next push can't write provenance.

- **`VOXHUB_SERVER_CONFIG` env var is not set for SSH forced-command sessions.**
  `settings.py` falls back to `~/.config/voxhub/server.toml`, which is written by
  `deploy.sh:297`, so this works by accident — non-login SSH sessions still expand
  `~` to `/home/voxhub/`. It's fine *today*, but it's fragile. Make
  `voxhub-forced-command.sh` `export
  VOXHUB_SERVER_CONFIG=/home/voxhub/.config/voxhub/server.toml` explicitly so it
  doesn't break the day someone changes the home dir.

- **No systemd unit, no journald integration.** Everything is SSH-on-demand. That's
  fine architecturally, but it means when a `prepare-pull` dies, the only
  breadcrumbs are whatever ended up in `/var/log/voxhub/debug.log` before the
  crash. Consider also symlinking that log into `/var/log/` or adding a tiny
  systemd `voxhub-gc.timer` instead of cron (gives you `journalctl -u` for free on
  GC failures).

---

## 4. Medium / would-catch-in-code-review

- **`deploy.sh:137`** pipes `curl https://astral.sh/uv/install.sh | sh`. Standard
  practice, but it should be version-pinned (`curl ... | env UV_VERSION=0.4.18 sh`)
  so redeploys are reproducible. Today the prod uv version is whatever astral
  shipped the morning you deployed.

- **`deploy.sh:238`** does `git reset --hard origin/$BRANCH` on redeploy. Correct
  for a prod checkout, but warn loudly if `$INSTALL_DIR` has uncommitted changes
  (i.e., someone hotfixed on the server). One line:
  `git -C "$INSTALL_DIR" diff --quiet || warn "local changes in $INSTALL_DIR will be discarded"`.

- **Rotating log at 500 MB cap with backupCount=10** is reasonable, but at
  `stderr_level = WARNING` *and* `log_file` catching everything DEBUG+, a chatty
  `integrate-annotations` can rotate surprisingly fast. Either lower `backup_count`
  to 5 on CX22 (250 MB cap), or raise the file log threshold to INFO. Check
  `logging.py` — if the file handler level isn't bounded, it's effectively DEBUG.

- **`prepare-pull` has no server-side timeout.** Client-side `SshRunner.run` sets a
  timeout, but if the SSH layer gets disconnected mid-stage, the server process
  keeps running to completion and the tempdir outlives the client. GC cleans it up
  24-48h later, which is fine — but on a 4 GB box, a stuck staging process is
  holding a full volume in RAM. Add a `signal.alarm()` or `concurrent.futures`
  timeout on the `stage()` call as defense in depth.

- **No server-side integration tests.** `tests/test_workflow.py` drives local
  functions directly and skips the SSH/rsync layer entirely. Before trusting this
  in production, write one e2e test that shells out to
  `ssh localhost voxhub-server list-stores` against a loopback sshd with a
  forced-command. It's tedious but it's the one thing that would have caught
  §2a/§2c.

- **No per-annotator rate limit.** A misbehaving client in a retry loop can spawn
  `integrate-annotations` processes back-to-back. Unlikely at 5 annotators, but
  cheap to add: `flock -n /tmp/voxhub-$USER.lock` in `voxhub-forced-command.sh`
  serializes per-user.

---

## 5. Sizing recommendation

For **2-5 annotators, typical CT volumes ≤ 400 MB uncompressed**:

- **Minimum:** CX22 (2 vCPU, 4 GB, 40 GB disk) + attached volume for `$STORES_DIR`.
  Works but will be tight: a single large push holds ~400 MB resident, and
  log+tmp+zarr competes for the 40 GB local disk.
- **Sweet spot:** **CX32 (4 vCPU, 8 GB, 80 GB disk) + a 100-200 GB volume mounted
  at `/mnt/storage/voxhub`.** Comfortable headroom for 3 concurrent annotators
  doing real work.
- **Only go larger (CX42)** if volumes >1 GB uncompressed are expected, or if
  stores stop being "one CT per annotator session" and become batch work.

**Make sure the volume is in `/etc/fstab` before running deploy.sh** — `deploy.sh`
warns about this but doesn't enforce it, and a rebooted VPS with an unmounted
volume will silently write into the root filesystem.

---

## 6. Go-live checklist

Ordered, do-this-before-annotators-touch-it:

1. **Patch the three path-validation issues** (§2a, §2c). 10 lines of code, 2 tests.
2. **Patch the staging memory guard** (§2b). Minimum: `psutil.virtual_memory().available`
   check with a clean error.
3. **Attach a Hetzner volume, add it to `/etc/fstab`, mount it at
   `/mnt/storage/voxhub`.** Verify with `findmnt`.
4. **Enable Hetzner daily snapshots on the instance + the attached volume**
   (~20% of instance cost). These are the disaster-recovery layer *under* the
   application-level backup cron — you want both.
5. **Run `sudo ./scripts/deploy/deploy.sh --stores-dir /mnt/storage/voxhub/data`**
   (dry-run first). Pass `--backup-target` pointing at a second disk/volume —
   the default (`<stores-dir parent>/backups`) lives on the same volume and
   only protects against bad pushes / operator error, not disk failure.
   After deploy: `sudo crontab -u voxhub -l` must show both the gc and the
   `voxhub-backup.sh` entries; run the backup once by hand
   (`sudo -u voxhub /usr/local/bin/voxhub-backup.sh --stores-dir ... --target ...`)
   and check `/var/log/voxhub/backup.log` for a `backup_completed` line.
6. **Add one test annotator:** `sudo ./scripts/deploy/add-annotator.sh alice alice.pub`.
7. **Smoke test from the client box:**
   - `voxhub remote-catalog voxhub@<ip>`
   - `voxhub pull` → annotate a fake seg in 3D Slicer → `voxhub push`
   - Verify `.meta/provenance.jsonl` has the new entry
   - Verify `$STAGING_DIR/vxhb-staging-*` got cleaned up (defaults to `/tmp`)
8. **Run `scripts/deploy/healthcheck.sh`.** Should be all-green.
9. **Tail logs during the smoke test:** `sudo journalctl -f` in one pane,
   `sudo tail -f /var/log/voxhub/debug.log` in another. Confirm the JSON structure
   is what the log aggregator (if any) expects.
10. **Simulate disk-full and oom:** `stress-ng --vm 1 --vm-bytes 90% --timeout 30s`
    during a `prepare-pull`; `fallocate -l <size>` to fill disk and confirm the
    server returns a structured error rather than crashing.
11. **Add the second and third annotator** and have them run the smoke test in
    parallel. Watch CPU/memory with `htop` — if pegged, you've outgrown CX22.
12. **Run the restore drill (§6a) once** before real annotators push — a backup
    you have never restored from is a hypothesis, not a backup.

---

## 6a. Restore drill (annotations + provenance)

Snapshots under the backup target are plain directory trees
(`<target>/<UTC-stamp>/`, `latest` symlinks the newest); unchanged files are
hardlinks between snapshots, so any single snapshot is a complete,
self-contained copy of `.meta/` and every store's `annotations/`. Raw volumes
are deliberately NOT in the backup — re-export them from DICOM.

Drill (run against a scratch directory first; ~5 minutes):

1. Pick the snapshot: `ls -1 <target>/` and choose the stamp, or use
   `<target>/latest`.
2. Restore into a scratch stores dir and inspect:
   `rsync -a <target>/latest/ /tmp/restore-drill/`
   — verify `/tmp/restore-drill/.meta/provenance.jsonl` parses
   (`voxhub-server validate-attributes` or `python -c "import json,sys;
   [json.loads(l) for l in open(sys.argv[1])]" ...`) and that each
   `<store>.zarr/annotations/<annotator>-<nano>/...` tree you expect is present.
3. For a real restore onto a rebuilt server: re-create the raw stores (DICOM
   re-export / `voxhub export`), stop annotator access (comment the keys in
   `~voxhub/.ssh/authorized_keys`), then
   `rsync -a <target>/<stamp>/ $STORES_DIR/` to lay `.meta/` and the
   `annotations/` trees back into place, and
   `chown -R voxhub:voxhub $STORES_DIR`.
4. Verify: `voxhub-server healthcheck`, then a `list-stores` from a client box
   must show the restored annotations; cross-check one annotation's zarr attrs
   against its `provenance.jsonl` line.
5. Confirm the next nightly backup runs clean against the restored tree
   (`backup_completed` in `/var/log/voxhub/backup.log`).

---

## Bottom line

The code is better than average for a first deployment. The architecture hard walls
(server/local split, protocol versioning, per-store locks, provenance-as-data) are
the kind of decisions usually only seen in the second rewrite. The deploy scripts
are careful.

The three things that would embarrass a senior reviewer are all in the same
category — **trusting client-provided paths on authenticated but not-fully-sandboxed
subcommands**. They're easy fixes. Ship them, then ship the service.

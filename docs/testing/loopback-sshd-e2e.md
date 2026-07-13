# Loopback-sshd End-to-End Suite

Launch plan 1.4 — the final regression gate for the SSH transport. Where the
client loopback suite (`test_pull_e2e.py`, see the README) replaces ssh/rsync
with in-process shims, this suite uses **all three for real**, on localhost,
no root needed:

- a throwaway `sshd` on a free high port, launched as the current user,
- the repo's real `scripts/deploy/voxhub-forced-command.sh` as the per-key
  forced command,
- the real `rrsync` confining the rsync branch to the staging root,
- the real `SshRunner` / `RsyncTransfer` / `_run_pull` client code and the
  real `voxhub-server rpc` dispatch (wire protocol v2).

This is the layer that hid launch findings 1.1–1.3 from every earlier suite,
and the layer that exposed the absolute-staging-path rsync defect (see
"History" below).

## Layout

```
packages/voxhub-client/tests/
└── test_e2e_sshd.py    sshd_loopback fixture (module-scoped) + the suite
```

The fixture is colocated with its single consumer rather than placed in
`conftest.py` (which holds the fixtures shared across test modules).

## Fixture recipe (`sshd_loopback`)

1. `ssh-keygen -t ed25519` host and client keypairs in a module tmpdir.
2. `sshd_config`: free high port (kernel-assigned), `ListenAddress 127.0.0.1`,
   `StrictModes no` (pytest tmpdirs fail sshd's ownership checks),
   `PasswordAuthentication no`, `UsePAM no`, `PermitUserEnvironment VOXHUB_*`,
   absolute paths throughout (sshd requires them).
3. `authorized_keys` mirroring `scripts/deploy/add-annotator.sh`:
   `command="<repo>/scripts/deploy/voxhub-forced-command.sh"`,
   `environment="VOXHUB_ANNOTATOR=alice"`, and the `no-*` restriction flags.
   Additional `environment=` options stand in for what `deploy.sh`
   renders/installs on a real server: `VOXHUB_STAGING_ROOT` (the wrapper's
   `@STAGING_ROOT@` render token fallback), `VOXHUB_SERVER` (the uv venv's
   `voxhub-server` entrypoint), `VOXHUB_SERVER_CONFIG` (a generated
   `server.toml` whose `stores_dir` / `staging_dir` live in the tmpdir), and
   `VOXHUB_RRSYNC` when rrsync was located. sshd strips the environment, so
   these options are the only way configuration reaches the wrapper.
4. A small zarr store seeded via the core test helpers (`_core_helpers.py`).
5. `sshd -D -f <config> -E <log>` via `subprocess.Popen` as the current user;
   the port is polled until it accepts; teardown terminates the daemon.
   `sshd.log` (DEBUG1) in the fixture tmpdir is the first stop when debugging.

Client wiring: `SshRunner` gets the loopback port/identity/known-hosts via
its `ssh_options` constructor parameter; the rsync `-e` string gets the same
options through a test-local `RsyncTransfer` subclass that overrides only
`_ssh_option`. The pull tests monkeypatch the same four module-scope names on
`voxhub_client.cli` as the loopback suite (`SshRunner`, `RsyncTransfer`,
`get_identity`, `get_server`) — production code is unchanged, and the
injected transports are the real ones.

## Test cases

| test | proves |
|---|---|
| `test_list_stores_end_to_end` | RPC through real sshd + wrapper returns the seeded store, `protocol_version` 2 |
| `test_forbidden_subcommand_rejected` | raw `ssh ... 'voxhub-server gc'` → `forbidden` envelope on stdout, exit 1 |
| `test_key_bound_identity_reaches_server` | the key's `environment="VOXHUB_ANNOTATOR=…"` survives sshd + wrapper exec: a disagreeing client-sent `annotator_id` is refused (`identity_mismatch`), the agreeing one succeeds |
| `test_pull_end_to_end` | full `_run_pull`: NRRD lands, checksums verify, manifest + trust sidecar written, staging dir reaped by the cleanup ACK |
| `test_pull_with_spaces_in_dest` | a local destination containing spaces survives the real rsync (guards launch 1.2) |
| `test_rsync_confined_to_staging` | rrsync serves the issued staging session (root-relative name) and refuses `stores_dir` via absolute path or `..` traversal |

## Requirements and skip behavior

Everything is marked `slow` and `e2e`. The whole module skips when `sshd`
(checked on `PATH` plus `/usr/sbin`, `/usr/local/sbin`), `ssh`, or
`ssh-keygen` is missing. The rsync-branch tests additionally skip when
`rsync` or `rrsync` is unavailable; rrsync is located via `PATH`,
`/usr/bin/rrsync`, `/usr/local/bin/rrsync`, the Debian script locations
(`/usr/share/rsync/scripts/`, `/usr/share/doc/rsync/scripts/`), or — on
bullseye/bookworm — gunzipped from `/usr/share/doc/rsync/scripts/rrsync.gz`
into the fixture tmpdir. The RPC-path tests keep running without rrsync.

## Running it

```bash
uv run pytest packages/voxhub-client/tests/test_e2e_sshd.py -v
uv run pytest -m slow -k sshd            # the launch-plan invocation
```

## History

The suite's first real run (2026-07-13) exposed a launch-blocking defect in
the wave-A transport integration: `_run_pull` rsync'd the **absolute**
`staging_dir` from the prepare-pull response, but rrsync — rooted at the
staging root per the forced-command wrapper contract — resolves every
requested path relative to its root, so every real pull failed with rsync
exit 23. Fixed by addressing the session by its staging-root-relative
basename (`voxhub-client/cli.py`); `LoopbackRsyncTransfer` now mirrors
rrsync's root-relative resolution so the shim suite would also catch a
regression.

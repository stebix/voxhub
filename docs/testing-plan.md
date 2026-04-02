# Testing & Quality Assessment Plan for Hetzner VPS Deployment

Status: **Decisions resolved — ready for implementation**

---

## Current State

- Unit tests across all 3 packages (~3100 lines, 14 test files)
- End-to-end workflow tests (dry-run pipeline in `tests/test_workflow.py`)
- Linting (ruff), formatting (ruff format), type checking (pyright)
- `pytest-cov` and `pytest-benchmark` available as dev dependencies but not yet
  wired into CI or enforced

---

## P0 — Must Have Before Deployment

### 1. CI Pipeline (GitHub Actions)

A workflow that runs on every push/PR. This is the foundation for everything else.

**Scope:**
- `ruff check packages/` and `ruff format --check packages/`
- `pyright`
- `pytest --cov` across all three packages + end-to-end tests
- Matrix: Python 3.12 (expand later if needed)

**Decisions:**
- **Separate jobs per package** so failures are visible at a glance.
- **Status badges** in the README for each package.
- **Branch protection enabled** — CI must pass before merging.

### 2. Smoke Test Suite (Post-Deploy Health Check)

A lightweight check runnable on the server after deployment or system updates
to verify the environment is sane.

**Checks:**
- Python version (3.12+)
- All three packages importable (`voxhub_schema`, `voxhub_core`, `voxhub_client`)
- Zarr store creation + read roundtrip on the actual filesystem
- SSH `authorized_keys` configured correctly
- `rsync` available and functional
- Disk space and permissions on the zarr root directory
- Provenance JSONL validation (scan all `.meta/provenance.jsonl` files, verify
  every line parses as valid JSON)

**Decisions:**
- Ships as a **dedicated CLI command**: `voxhub-server healthcheck`.
- **Exit code + stdout** output. No log files — we interact via SSH directly.

### 3. Coverage Enforcement

Prevent coverage from silently eroding as features are added.

**Decisions:**
- **70% threshold per package**, enforced in each package's CI job:
  ```bash
  pytest packages/voxhub-schema/tests --cov=voxhub_schema --cov-fail-under=70
  pytest packages/voxhub-core/tests  --cov=voxhub_core  --cov-fail-under=70
  pytest packages/voxhub-client/tests --cov=voxhub_client --cov-fail-under=70
  ```
- Per-package thresholds fall out naturally from the per-package CI jobs — no
  extra tooling needed. Thresholds can be ratcheted independently later.

---

## P1 — Should Have Before Production Use

### 4. Server Command Integration Tests

The server CLI commands (`list-stores`, `prepare-pull`, `integrate-annotations`,
`cleanup`, `gc`) are the primary attack surface and are currently untested at the
transport/protocol layer.

**Scope:**
- Invoke `voxhub-server` subcommands via `subprocess` against real zarr stores
  on disk
- Verify JSON-over-stdout protocol (correct envelope structure, error codes)
- Verify `protocol_version` is present in every response
- Test error paths: missing store, invalid annotator, malformed input

**Decisions:**
- **Local SSH server** in the test environment if feasible, to mirror production
  more closely. Fall back to subprocess invocation if SSH setup proves too heavy.
- **tmpdir per test** for zarr store fixtures — better isolation, no state leakage.
- **ForceCommand testing** lives here (see Section 5).

### 5. Security Boundary Tests

The server is SSH-exposed. Validate that untrusted input cannot escape its sandbox.

**Scope:**
- **Path traversal**: Crafted `store_name` or `wip_dir` values containing `..`,
  symlinks, or absolute paths must not escape the zarr root
- **Annotator isolation**: Annotator A cannot read or overwrite annotator B's
  annotation directories
- **Malformed input**: Invalid JSON, oversized payloads, binary garbage sent to
  server CLI — must produce clean error envelopes, not tracebacks or crashes

**ForceCommand context:** In production, SSH `ForceCommand` is configured in
`sshd_config` (or `authorized_keys`) to override any client command:
```
Match User voxhub
    ForceCommand /usr/local/bin/voxhub-server "$SSH_ORIGINAL_COMMAND"
```
This prevents bypassing `voxhub-server` entirely — even `ssh server "rm -rf /"`
only executes `voxhub-server` with the original command as argument. This is the
transport-layer defense; the path traversal / malformed input tests here cover the
application-layer defense.

**Decisions:**
- **ForceCommand** is tested as part of the Section 4 SSH integration tests, not
  as a separate item.
- **No fuzzing** — not needed for this use case.

### 6. Concurrent Writer Safety

Multiple annotators pushing simultaneously is the expected production pattern.

**Scope:**
- Two or more `integrate-annotations` calls hitting the same zarr store in parallel
- Verify `filelock` prevents corruption
- Verify both operations eventually succeed (no deadlocks, no silent data loss)
- Test lock timeout / stale lock recovery

**Concurrency model:** Each `voxhub push` triggers a fresh SSH connection, which
spawns an independent `voxhub-server integrate-annotations` process. There is no
long-lived server process or async event loop — concurrency is purely
**multi-process**, coordinated via `filelock`.

**Decisions:**
- **Cross-process tests only** (matches the SSH execution model). Use
  `subprocess.Popen` to launch concurrent `voxhub-server` processes.
- **Test up to 5 concurrent writers** (production is typically 2-3, test slightly
  beyond for robustness).

---

## P2 — Important for Ongoing Quality

### 7. Provenance / Audit Integrity Tests

`.meta/provenance.jsonl` is the audit trail. It must be reliable under all
conditions.

**Scope:**
- Provenance entry is written on every successful integration
- Provenance is written (or at least not corrupted) on partial failures
- Concurrent appends to the same JSONL file produce valid JSONL (no interleaved
  lines, no partial writes)
- Encoding correctness (UTF-8, no mojibake in annotator names)

**Decisions:**
- **fsync after every provenance write.** When writing to a file, the OS buffers
  data in memory and flushes to disk later. `fsync()` forces an immediate flush,
  guaranteeing durability even on power loss or kernel panic. Cost is ~5-10ms per
  write — negligible for 2-5 annotators at low write frequency.
- **No standalone verification tool.** Instead, add a small validation function
  (~20 lines in `audit.py`) that iterates JSONL lines and `json.loads()` each one,
  reporting failures. Wire this into the `voxhub-server healthcheck` command
  (Section 2) for automatic detection on every health check.

### 8. Benchmark Baselines

**Out of scope for initial deployment.** Revisit once the server is running and
we have real workload data.

### 9. Backup / Recovery Tests

**Scope:**
- Zarr store backup + restore roundtrip (all arrays, attrs, provenance intact)
- Restored store passes all existing validation checks
- Annotator sessions can resume after a server restart mid-operation

**Decisions:**
- **Backup strategy:** rsync snapshots to an on-prem server.
- **Frequency:** Daily.
- **Destination:** Separate on-prem server, accessible via rsync.

---

## P3 — Not in Scope

Sections 10 (Large Volume Stress Tests) and 11 (Systemd / Process Management) are
deferred. Focus is on P0-P2 for the initial deployment.

---

## Implementation Order

```
Phase 1 (pre-deploy):     CI pipeline → coverage enforcement → smoke tests
Phase 2 (pre-production): server integration tests → security boundaries → concurrency
Phase 3 (ongoing):        provenance tests → backup/recovery
```

---

## Decisions Summary

| Item                  | Decision                                                    |
|-----------------------|-------------------------------------------------------------|
| CI jobs               | Per-package, with status badges                             |
| Branch protection     | Yes                                                         |
| Smoke test            | `voxhub-server healthcheck` CLI command, stdout + exit code |
| Coverage              | 70% per-package, enforced per CI job                        |
| Server integration    | tmpdir per test, local SSH server if feasible                |
| ForceCommand          | Tested as part of SSH integration tests                     |
| Fuzzing               | Out of scope                                                |
| Concurrency model     | Cross-process only (matches SSH model), up to 5 writers     |
| fsync                 | Yes — cheap insurance for provenance writes                 |
| Provenance validation | Inline function in `audit.py` + healthcheck, no standalone  |
| Benchmarks            | Out of scope for initial deployment                         |
| Backups               | Daily rsync to on-prem server                               |
| P3 items              | Deferred                                                    |

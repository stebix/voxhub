# voxhub-core

Domain library and SSH-invoked server entrypoint for voxhub.

Contains the DICOM pipeline, zarr I/O, staging, annotation integration,
provenance auditing, and the `voxhub-server` CLI that runs on the remote
host via SSH forced command.

---

## Server logging

The server emits structured JSON log lines.  Two simultaneous outputs are
supported: a low-noise stream to stderr (suitable for the systemd journal)
and an optional high-verbosity rotating file for debugging.

### How it works

`voxhub-server` uses [structlog](https://www.structlog.org/) as its
structured logging frontend, backed by Python's stdlib `logging` module.
This lets two handlers receive every record independently:

| Output | Default level | Destination |
|---|---|---|
| stderr | WARNING | systemd journal / shell stderr |
| rotating file | DEBUG | path configured in `server.toml` |

Both outputs render records as newline-delimited JSON, so they can be
processed with the same tooling (e.g. `jq`, log aggregators).

Third-party library records (zarr, pydicom, etc.) are also captured and
rendered as JSON via structlog's `foreign_pre_chain`.

### Configuration file

Settings are read from a TOML file.  Resolution order:

1. Path given by the `VOXHUB_SERVER_CONFIG` environment variable
2. `~/.config/voxhub/server.toml`
3. Built-in defaults (WARNING to stderr, no log file)

#### Full example

```toml
[logging]
# Absolute path for the rotating debug log file.
# Remove or comment out to disable file logging entirely.
log_file = "/var/log/voxhub/debug.log"

# Maximum size of a single log file before rotation (bytes). Default: 50 MB.
log_max_bytes = 52428800

# Number of rotated backup files to keep alongside the active log. Default: 10.
# Total on-disk budget = log_max_bytes × (log_backup_count + 1).
log_backup_count = 10

# Minimum level forwarded to stderr / systemd journal.
# One of: DEBUG, INFO, WARNING, ERROR, CRITICAL. Default: WARNING.
stderr_level = "WARNING"
```

With this config the server keeps up to 550 MB of debug history
(11 × 50 MB) in `/var/log/voxhub/`, while the systemd journal only
receives WARNING and above.

### Defaults (no config file)

Without a config file the server behaves as before: WARNING and above go
to stderr, no log file is written.

### Querying logs

**systemd journal** (stderr stream):

```bash
# Follow live
journalctl -u voxhub-server -f

# Filter to a specific command
journalctl -u voxhub-server | jq 'select(.command == "integrate-annotations")'

# Errors only
journalctl -u voxhub-server -p err
```

**Rotating debug file** (when `log_file` is configured):

```bash
# Tail the active file
tail -f /var/log/voxhub/debug.log | jq .

# Find all failed integrations in the last 100 000 lines
grep -h '' /var/log/voxhub/debug.log* | jq 'select(.event == "seg_integrate_failed")'

# Show all records for a specific annotator session
grep -h '' /var/log/voxhub/debug.log* \
  | jq 'select(.annotator_id == "alice" and .session_id != null)'
```

### Log record fields

Every record includes at minimum:

| Field | Description |
|---|---|
| `event` | Snake-case event name (e.g. `integrate_completed`) |
| `level` | Log level string (`debug`, `info`, `warning`, `error`) |
| `logger` | Module that emitted the record |
| `timestamp` | ISO-8601 UTC timestamp |

Command handlers bind additional context at the start of each invocation
(e.g. `command`, `stores_dir`, `annotator_id`), so all records within a
command carry that context automatically.

---

## Catalog cache admin

The server caches the `list-stores` payload on disk at
`<stores_dir>/.meta/catalog.json`. The cache is refreshed automatically
on a 60 s TTL and invalidated per-store on every in-band write
(`integrate-annotations`). For everything else — manual `rsync` of a new
store, hand-edited metadata, a deleted `*.zarr` directory — use the
`catalog` subcommand to force immediate reconciliation.

```bash
voxhub-server catalog refresh               # rebuild the whole catalog
voxhub-server catalog refresh --store NAME  # re-probe one store
voxhub-server catalog show                  # print the current snapshot
voxhub-server catalog stats                 # cache file age, fingerprint, size
```

Each command emits a single JSON object on stdout. Refresh bumps
`catalog_version`; `stats` reports `status` (`ok` / `missing` /
`corrupt`), `age_s`, `fingerprint_match`, `store_count`, and
`cache_file_size_bytes`. A mismatched fingerprint means the next warm
read will trigger a full rebuild on its own — manual refresh is only
required when the operator needs visibility *before* the next read.

### Provenance vs. logs

Structlog records capture **operational events** (what the server did,
errors, timings).  They are not the authoritative audit trail.

The authoritative data-lineage record is the **provenance JSONL** written
to `<stores_dir>/.meta/provenance.jsonl` — one entry per successful push,
fsynced for durability.  Use that file to answer "who annotated what and
when"; use the log file to answer "what did the server do, and did
anything go wrong".

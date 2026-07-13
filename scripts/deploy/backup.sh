#!/usr/bin/env bash
# backup.sh — nightly hardlink snapshot of voxhub's non-reproducible data.
#
# Annotations + .meta/ (provenance.jsonl) are the only data on a voxhub
# server that cannot be re-created (raw volumes can be re-exported from
# DICOM).  This script rsyncs
#
#     $STORES_DIR/.meta/
#     $STORES_DIR/*/annotations/        (i.e. <store>.zarr/annotations)
#
# into a timestamped snapshot directory under the backup target, using
# ``rsync -a --link-dest`` against the previous snapshot so unchanged
# files are hardlinks (near-zero incremental disk cost).  Snapshots older
# than the retention window are rotated out.  One structlog-style JSON
# line is appended to the log on success or failure.
#
# Usage:
#   backup.sh --stores-dir /mnt/storage/voxhub/data [--target DIR] \
#             [--retention-days N] [--log-file FILE]
#
# Configuration (flags override environment; defaults follow deploy.sh's
# layout):
#   --stores-dir      | VOXHUB_STORES_DIR                (required)
#   --target          | VOXHUB_BACKUP_TARGET             (default:
#                       <stores-dir parent>/backups — point this at a
#                       second disk/volume, or host:/path for a remote
#                       target reachable over ssh as the invoking user)
#   --retention-days  | VOXHUB_BACKUP_RETENTION_DAYS     (default: 14)
#   --log-file        | VOXHUB_BACKUP_LOG                (default:
#                       /var/log/voxhub/backup.log; falls back to stderr
#                       if unwritable)
#
# Cron entry is installed by deploy.sh (idempotent, same pattern as the
# gc cron).  Restore drill: docs/deployment-readiness.md.

set -euo pipefail

STORES_DIR="${VOXHUB_STORES_DIR:-}"
TARGET="${VOXHUB_BACKUP_TARGET:-}"
RETENTION_DAYS="${VOXHUB_BACKUP_RETENTION_DAYS:-14}"
LOG_FILE="${VOXHUB_BACKUP_LOG:-/var/log/voxhub/backup.log}"

usage() {
    sed -n '2,34p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --stores-dir)     STORES_DIR="$2"; shift 2 ;;
        --target)         TARGET="$2"; shift 2 ;;
        --retention-days) RETENTION_DAYS="$2"; shift 2 ;;
        --log-file)       LOG_FILE="$2"; shift 2 ;;
        -h|--help)        usage ;;
        *) echo "backup.sh: unknown option: $1" >&2; exit 2 ;;
    esac
done

# ---------------------------------------------------------------------------
# structlog-style JSON logging (one line per run; success or failure)
# ---------------------------------------------------------------------------

# json_escape <string> — minimal escaper for the values we emit (paths,
# error text).  Backslash first, then quotes; control chars are not
# expected in paths but newlines are stripped defensively.
json_escape() {
    local s=$1
    s=${s//\\/\\\\}
    s=${s//\"/\\\"}
    s=${s//$'\n'/ }
    printf '%s' "$s"
}

# log_json <level> <event> [key value]... — append one JSON line.
log_json() {
    local level=$1 event=$2
    shift 2
    local ts line
    ts="$(date -u +%Y-%m-%dT%H:%M:%S.%6NZ)"
    line="{\"event\": \"$(json_escape "$event")\", \"level\": \"$level\""
    line+=", \"timestamp\": \"$ts\", \"logger\": \"voxhub.backup\""
    while [[ $# -ge 2 ]]; do
        line+=", \"$(json_escape "$1")\": \"$(json_escape "$2")\""
        shift 2
    done
    line+='}'
    if [[ -w "$LOG_FILE" ]] || { [[ ! -e "$LOG_FILE" ]] \
        && mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null \
        && touch "$LOG_FILE" 2>/dev/null; }; then
        printf '%s\n' "$line" >> "$LOG_FILE"
    else
        printf '%s\n' "$line" >&2
    fi
}

FAIL_REASON="unexpected error"
on_error() {
    log_json error backup_failed \
        stores_dir "$STORES_DIR" \
        target "$TARGET" \
        reason "$FAIL_REASON"
}
trap on_error ERR

# ---------------------------------------------------------------------------
# Validate configuration
# ---------------------------------------------------------------------------

if [[ -z "$STORES_DIR" ]]; then
    FAIL_REASON="--stores-dir (or VOXHUB_STORES_DIR) is required"
    echo "backup.sh: $FAIL_REASON" >&2
    false
fi
if [[ ! -d "$STORES_DIR" ]]; then
    FAIL_REASON="stores dir does not exist: $STORES_DIR"
    echo "backup.sh: $FAIL_REASON" >&2
    false
fi
if [[ -z "$TARGET" ]]; then
    TARGET="$(dirname "$STORES_DIR")/backups"
fi
if ! [[ "$RETENTION_DAYS" =~ ^[0-9]+$ ]] || [[ "$RETENTION_DAYS" -lt 1 ]]; then
    FAIL_REASON="--retention-days must be a positive integer: $RETENTION_DAYS"
    echo "backup.sh: $FAIL_REASON" >&2
    false
fi

# Remote target (host:/path): snapshot layout, ``latest`` symlink, and
# rotation are managed over ssh.  ``--link-dest`` is resolved on the
# receiving side, so hardlink dedup works remotely too.  Remote paths
# must not contain spaces.
REMOTE_HOST=""
TARGET_DIR="$TARGET"
if [[ "$TARGET" == *:* && "$TARGET" != /* ]]; then
    REMOTE_HOST="${TARGET%%:*}"
    TARGET_DIR="${TARGET#*:}"
fi

# t_run <cmd...> — run a target-side command locally or over ssh.
t_run() {
    if [[ -n "$REMOTE_HOST" ]]; then
        ssh -o BatchMode=yes "$REMOTE_HOST" "$@"
    else
        "$@"
    fi
}

# ---------------------------------------------------------------------------
# Collect sources (relative to $STORES_DIR via rsync's /./ anchor, so the
# snapshot preserves the <store>.zarr/annotations layout)
# ---------------------------------------------------------------------------

SOURCES=()
if [[ -d "$STORES_DIR/.meta" ]]; then
    SOURCES+=("$STORES_DIR/./.meta")
fi
STORE_COUNT=0
for ann_dir in "$STORES_DIR"/*/annotations; do
    if [[ -d "$ann_dir" ]]; then
        store_name="$(basename "$(dirname "$ann_dir")")"
        SOURCES+=("$STORES_DIR/./$store_name/annotations")
        STORE_COUNT=$((STORE_COUNT + 1))
    fi
done

if [[ ${#SOURCES[@]} -eq 0 ]]; then
    # Nothing to protect yet (fresh deploy).  Not an error: report and
    # exit cleanly so cron stays quiet.
    log_json info backup_skipped_empty \
        stores_dir "$STORES_DIR" \
        target "$TARGET"
    exit 0
fi

# ---------------------------------------------------------------------------
# Snapshot via rsync --link-dest against the previous snapshot
# ---------------------------------------------------------------------------

START_S=$SECONDS
STAMP="$(date -u +%Y%m%dT%H%M%S)"
# Avoid clobbering when two runs land in the same second.
N=1
SNAP_NAME="$STAMP"
while t_run test -e "$TARGET_DIR/$SNAP_NAME"; do
    N=$((N + 1))
    SNAP_NAME="$STAMP-$N"
done
SNAP_DIR="$TARGET_DIR/$SNAP_NAME"

FAIL_REASON="failed to create snapshot dir $SNAP_DIR"
t_run mkdir -p "$SNAP_DIR"

RSYNC_ARGS=(-a --relative)
# ``latest`` points at the previous snapshot; resolve it target-side so
# --link-dest gets an absolute receiving-side path.
if t_run test -d "$TARGET_DIR/latest/"; then
    PREV="$(t_run readlink -f "$TARGET_DIR/latest")"
    if [[ -n "$PREV" ]]; then
        RSYNC_ARGS+=("--link-dest=$PREV")
    fi
fi

FAIL_REASON="rsync to $SNAP_DIR failed"
if [[ -n "$REMOTE_HOST" ]]; then
    rsync "${RSYNC_ARGS[@]}" "${SOURCES[@]}" "$REMOTE_HOST:$SNAP_DIR/"
else
    rsync "${RSYNC_ARGS[@]}" "${SOURCES[@]}" "$SNAP_DIR/"
fi

FAIL_REASON="failed to update latest symlink in $TARGET_DIR"
t_run ln -sfn "$SNAP_NAME" "$TARGET_DIR/latest"

# ---------------------------------------------------------------------------
# Rotation: drop snapshot dirs older than the retention window.  Only
# dirs matching the timestamp pattern are touched — stray files and the
# ``latest`` symlink are never rotation candidates.  find's -mtime +N
# matches age >= N+1 days, hence RETENTION_DAYS - 1 keeps snapshots
# younger than RETENTION_DAYS days.
# ---------------------------------------------------------------------------

FAIL_REASON="rotation in $TARGET_DIR failed"
ROTATED=$(
    t_run find "$TARGET_DIR" -mindepth 1 -maxdepth 1 -type d \
        -name '[0-9]*T[0-9]*' -mtime "+$((RETENTION_DAYS - 1))" \
        -print | wc -l
)
t_run find "$TARGET_DIR" -mindepth 1 -maxdepth 1 -type d \
    -name '[0-9]*T[0-9]*' -mtime "+$((RETENTION_DAYS - 1))" \
    -exec rm -rf {} +

log_json info backup_completed \
    stores_dir "$STORES_DIR" \
    target "$TARGET" \
    snapshot "$SNAP_NAME" \
    stores "$STORE_COUNT" \
    rotated "$ROTATED" \
    retention_days "$RETENTION_DAYS" \
    duration_s "$((SECONDS - START_S))"

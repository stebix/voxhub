#!/usr/bin/env bash
# uninstall.sh — remove a voxhub server deployment so deploy.sh can lay down
# a clean one.  The inverse of deploy.sh, step for step.
#
# Usage:
#   sudo ./uninstall.sh --dry-run            # show the plan, touch nothing
#   sudo ./uninstall.sh                      # full teardown, data preserved
#   sudo ./uninstall.sh --purge-data         # also wipe stores + backups
#   sudo ./uninstall.sh --keep-user --keep-uv
#
# Idempotent — safe to re-run, and safe to run against a half-installed or
# never-installed box (every step skips what isn't there).
#
# What is removed by default
# --------------------------
#   /opt/voxhub                       code + venv
#   /opt/voxhub-uv                    uv-managed python + wheel cache
#   /usr/local/bin/voxhub-server      entrypoint symlink
#   /usr/local/bin/voxhub-forced-command.sh
#   /usr/local/bin/voxhub-backup.sh
#   /usr/local/bin/uv                 (--keep-uv retains)
#   /usr/local/bin/rrsync             ONLY the deploy-installed copy — never
#                                     /usr/bin/rrsync, which belongs to apt's
#                                     rsync package (see step 8)
#   /etc/ssh/sshd_config.d/voxhub.conf
#   /var/log/voxhub
#   the voxhub user's crontab (gc + backup jobs)
#   the voxhub system user and its home  (--keep-user retains)
#   the CONTENTS of the staging dir (ephemeral scratch; the dir itself and
#   any mount underneath it survive)
#
# What is preserved by default
# ----------------------------
#   stores_dir       the zarr stores: volumes, annotations, .meta/provenance
#   backup target    the nightly snapshot tree
#
# ``--purge-data`` destroys both.  It demands the stores path be typed back
# verbatim (or supplied via VOXHUB_PURGE_CONFIRM for non-interactive runs) —
# annotations are the one thing on this box that cannot be re-derived.
#
# Apt packages (rsync, git, curl, ca-certificates) are deliberately left
# installed even on a full teardown: they are shared system tools, and
# removing rsync would break unrelated things on the host.
#
# The UID hand-off
# ----------------
# Removing the user frees its numeric UID, but the preserved data stays owned
# by that number.  Before ``userdel`` this script records the uid/gid to
# ``/var/lib/voxhub/uninstall-state``; deploy.sh reads that file and recreates
# the user with the same numbers, so ownership of the retained stores and
# backup snapshots keeps lining up.  Do not delete the state file between an
# uninstall and the redeploy that follows it.
#
# Test hooks (never set by an operator; mirrors the VOXHUB_SERVER / VOXHUB_RRSYNC
# pattern in voxhub-forced-command.sh):
#   VOXHUB_TEST_ROOT   prefix every system path, and skip the root check
#   VOXHUB_SSHD, VOXHUB_SYSTEMCTL, VOXHUB_CRONTAB, VOXHUB_USERDEL,
#   VOXHUB_PKILL, VOXHUB_PGREP, VOXHUB_ID   override privileged binaries

set -euo pipefail

# ---------------------------------------------------------------------------
# Colours / helpers  (same vocabulary as deploy.sh)
# ---------------------------------------------------------------------------

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

info()  { printf "${CYAN}[INFO]${RESET}  %s\n" "$*"; }
ok()    { printf "${GREEN}[OK]${RESET}    %s\n" "$*"; }
warn()  { printf "${YELLOW}[WARN]${RESET}  %s\n" "$*"; }
fail()  { printf "${RED}[FAIL]${RESET}  %s\n" "$*" >&2; exit 1; }
skip()  { printf "${GREEN}[SKIP]${RESET}  %s (not present)\n" "$*"; }
gone()  { printf "${GREEN}[GONE]${RESET}  %s\n" "$*"; }
kept()  { printf "${CYAN}[KEPT]${RESET}  %s\n" "$*"; }

STEP_NUM=0
step() {
    STEP_NUM=$((STEP_NUM + 1))
    printf "\n${BOLD}── Step %d: %s${RESET}\n" "$STEP_NUM" "$*"
}

# ---------------------------------------------------------------------------
# Paths — all prefixed by VOXHUB_TEST_ROOT (empty in production)
# ---------------------------------------------------------------------------

TEST_ROOT="${VOXHUB_TEST_ROOT:-}"

VOXHUB_USER="voxhub"
INSTALL_DIR="$TEST_ROOT/opt/voxhub"
UV_ROOT="$TEST_ROOT/opt/voxhub-uv"
LOG_DIR="$TEST_ROOT/var/log/voxhub"
BIN_DIR="$TEST_ROOT/usr/local/bin"
SERVER_BIN="$BIN_DIR/voxhub-server"
FORCED_CMD="$BIN_DIR/voxhub-forced-command.sh"
BACKUP_BIN="$BIN_DIR/voxhub-backup.sh"
UV_BIN="$BIN_DIR/uv"
RRSYNC_LOCAL="$BIN_DIR/rrsync"
SSHD_CONF="$TEST_ROOT/etc/ssh/sshd_config.d/voxhub.conf"
STATE_DIR="$TEST_ROOT/var/lib/voxhub"
STATE_FILE="$STATE_DIR/uninstall-state"
ARCHIVE_DIR="$TEST_ROOT/var/backups"

# Privileged binaries, overridable for tests.
SSHD_BIN="${VOXHUB_SSHD:-sshd}"
SYSTEMCTL="${VOXHUB_SYSTEMCTL:-systemctl}"
CRONTAB="${VOXHUB_CRONTAB:-crontab}"
USERDEL="${VOXHUB_USERDEL:-userdel}"
PKILL="${VOXHUB_PKILL:-pkill}"
PGREP="${VOXHUB_PGREP:-pgrep}"
ID_BIN="${VOXHUB_ID:-id}"

# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------

DRY_RUN=false
FORCE=false
PURGE_DATA=false
KEEP_USER=false
KEEP_UV=false
STORES_DIR=""
STAGING_DIR=""
BACKUP_TARGET=""
TERM_GRACE_S="${VOXHUB_TERM_GRACE_S:-5}"

usage() {
    cat <<EOF
Usage: sudo $0 [OPTIONS]

Removes a voxhub server deployment.  Annotation data is preserved unless
--purge-data is given.

Options:
  --dry-run              Print the plan; change nothing
  --force                Proceed even if voxhub-server is currently running
  --purge-data           ALSO delete stores_dir and the backup target.
                         Requires typing the stores path back to confirm
                         (or VOXHUB_PURGE_CONFIRM=<stores-dir> when stdin
                         is not a terminal).
  --keep-user            Keep the '$VOXHUB_USER' user, its home and its
                         authorized_keys (annotators stay onboarded)
  --keep-uv              Keep /usr/local/bin/uv and $UV_ROOT
                         (faster redeploy; keeps a pinned toolchain around)
  --stores-dir <path>    Override the stores dir (default: read from the
                         server config)
  --staging-dir <path>   Override the staging dir (default: read from the
                         server config)
  --backup-target <path> Override the backup target (default:
                         <stores-dir parent>/backups, as in deploy.sh)
  -h, --help             Show this help
EOF
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)       DRY_RUN=true; shift ;;
        --force)         FORCE=true; shift ;;
        --purge-data)    PURGE_DATA=true; shift ;;
        --keep-user)     KEEP_USER=true; shift ;;
        --keep-uv)       KEEP_UV=true; shift ;;
        --stores-dir)    STORES_DIR="$2"; shift 2 ;;
        --staging-dir)   STAGING_DIR="$2"; shift 2 ;;
        --backup-target) BACKUP_TARGET="$2"; shift 2 ;;
        -h|--help)       usage ;;
        *)               fail "Unknown option: $1" ;;
    esac
done

# The root check is skipped under a test root, where every path is a tmpdir
# and the privileged binaries are stubs.
if [[ -z "$TEST_ROOT" && "$EUID" -ne 0 ]]; then
    fail "This script must be run as root (or via sudo)"
fi

if $DRY_RUN; then
    warn "Dry-run mode — nothing will be removed"
fi

# run <cmd...> — execute unless dry-running.
run() {
    if $DRY_RUN; then
        info "(dry-run) $*"
    else
        "$@"
    fi
}

# rm_path <kind> <path> — idempotent removal with uniform reporting.
rm_path() {
    local kind="$1" path="$2"
    if [[ -e "$path" || -L "$path" ]]; then
        run rm -rf -- "$path"
        gone "$kind: $path"
    else
        skip "$kind ($path)"
    fi
}

# ===================================================================
# Step 1: Discover the deployment
# ===================================================================
step "Discover the deployment"

# The user's home is where deploy.sh put the server config.  Resolve it from
# the passwd database when the user exists, else fall back to the layout
# deploy.sh creates.
USER_EXISTS=false
if "$ID_BIN" "$VOXHUB_USER" &>/dev/null; then
    USER_EXISTS=true
fi

if $USER_EXISTS && [[ -z "$TEST_ROOT" ]]; then
    VOXHUB_HOME=$(eval echo "~$VOXHUB_USER")
else
    VOXHUB_HOME="$TEST_ROOT/home/$VOXHUB_USER"
fi

CONFIG_FILE="$VOXHUB_HOME/.config/voxhub/server.toml"

# toml_value <key> <file> — pull a quoted string value out of the flat TOML
# deploy.sh writes.  Not a general TOML parser: it reads exactly the shape we
# author ourselves (key = "value", optional trailing comment).
toml_value() {
    local key="$1" file="$2"
    [[ -f "$file" ]] || return 0
    sed -n "s/^[[:space:]]*$key[[:space:]]*=[[:space:]]*\"\(.*\)\".*/\1/p" "$file" \
        | head -1
}

if [[ -f "$CONFIG_FILE" ]]; then
    info "Reading $CONFIG_FILE"
    [[ -z "$STORES_DIR"  ]] && STORES_DIR="$(toml_value stores_dir "$CONFIG_FILE")"
    [[ -z "$STAGING_DIR" ]] && STAGING_DIR="$(toml_value staging_dir "$CONFIG_FILE")"
else
    warn "No server config at $CONFIG_FILE — relying on flags"
fi

# deploy.sh's default: backups sit beside the stores dir.
if [[ -z "$BACKUP_TARGET" && -n "$STORES_DIR" ]]; then
    BACKUP_TARGET="$(dirname "$STORES_DIR")/backups"
fi

# A stores/staging path of '/' would turn a purge into a machine wipe.  Refuse
# outright rather than trusting a downstream guard.
for path_var in STORES_DIR STAGING_DIR BACKUP_TARGET; do
    case "${!path_var}" in
        /|//) fail "$path_var resolved to '${!path_var}' — refusing to continue" ;;
    esac
done

info "Stores dir:     ${STORES_DIR:-(unknown)}"
info "Staging dir:    ${STAGING_DIR:-(unknown)}"
info "Backup target:  ${BACKUP_TARGET:-(unknown)}"
info "User '$VOXHUB_USER': $($USER_EXISTS && echo present || echo absent)"

if $PURGE_DATA && [[ -z "$STORES_DIR" ]]; then
    fail "--purge-data needs a stores dir, and none could be resolved — pass --stores-dir"
fi

# ===================================================================
# Step 2: Cut off SSH access
# ===================================================================
# First, so no annotator can open a new session against a server we are about
# to dismantle underneath them.
step "Remove sshd configuration"

if [[ -f "$SSHD_CONF" ]]; then
    if $DRY_RUN; then
        info "(dry-run) rm $SSHD_CONF; validate with '$SSHD_BIN -t'; reload sshd"
    else
        SSHD_BACKUP="$(mktemp)"
        cp "$SSHD_CONF" "$SSHD_BACKUP"
        rm -f "$SSHD_CONF"
        # The drop-in was a 'Match User voxhub' block, so removing it cannot
        # lock anyone out — but validate anyway and put it back if sshd
        # disagrees.  A missing sshd binary (containers, minimal images) is a
        # warning, not a failure.
        if command -v "$SSHD_BIN" &>/dev/null; then
            if "$SSHD_BIN" -t 2>/dev/null; then
                if "$SYSTEMCTL" reload sshd 2>/dev/null; then
                    ok "sshd config removed and sshd reloaded"
                else
                    warn "sshd config removed, but 'systemctl reload sshd' failed — reload it manually"
                fi
            else
                cp "$SSHD_BACKUP" "$SSHD_CONF"
                rm -f "$SSHD_BACKUP"
                fail "sshd config invalid WITHOUT $SSHD_CONF — restored it, sshd NOT reloaded"
            fi
        else
            warn "$SSHD_BIN not found — removed $SSHD_CONF without validating; reload sshd yourself"
        fi
        rm -f "$SSHD_BACKUP"
        gone "sshd config: $SSHD_CONF"
    fi
else
    skip "sshd config ($SSHD_CONF)"
fi

# ===================================================================
# Step 3: Remove the cron jobs
# ===================================================================
# Explicitly, not as a side effect of userdel: the gc and backup jobs must
# stop before the binaries they invoke disappear, and --keep-user must not
# leave them behind.
step "Remove cron jobs"

if $USER_EXISTS && "$CRONTAB" -u "$VOXHUB_USER" -l &>/dev/null; then
    info "Removing crontab for '$VOXHUB_USER' (gc + backup)"
    run "$CRONTAB" -u "$VOXHUB_USER" -r
    gone "crontab for '$VOXHUB_USER'"
else
    skip "crontab for '$VOXHUB_USER'"
fi

# ===================================================================
# Step 4: Drain running processes
# ===================================================================
step "Drain running processes"

if ! $USER_EXISTS; then
    skip "processes (user '$VOXHUB_USER' absent)"
elif $DRY_RUN; then
    info "(dry-run) terminate processes owned by '$VOXHUB_USER'"
else
    # An in-flight integrate-annotations is the one thing worth stopping for:
    # killing it mid-write is how a store gets a half-integrated annotation.
    if "$PGREP" -u "$VOXHUB_USER" -f voxhub-server &>/dev/null && ! $FORCE; then
        fail "voxhub-server is running as '$VOXHUB_USER' — an integrate may be in flight.
        Wait for it to finish, or re-run with --force to terminate it."
    fi
    if "$PKILL" -u "$VOXHUB_USER" &>/dev/null; then
        info "Sent TERM to processes owned by '$VOXHUB_USER'; waiting ${TERM_GRACE_S}s"
        sleep "$TERM_GRACE_S"
        if "$PKILL" -KILL -u "$VOXHUB_USER" &>/dev/null; then
            warn "Some processes ignored TERM and were killed"
        fi
        ok "Processes drained"
    else
        skip "processes (none running as '$VOXHUB_USER')"
    fi
fi

# ===================================================================
# Step 5: Archive the irreplaceable bits of the home directory
# ===================================================================
# authorized_keys is the only thing here that cost human coordination to
# assemble.  userdel -r would take it with the home dir, so snapshot it first:
# re-onboarding after a redeploy becomes a copy-back instead of a round of
# "please send me your public key again".
step "Archive authorized_keys + server config"

ARCHIVE_MEMBERS=()
[[ -f "$VOXHUB_HOME/.ssh/authorized_keys" ]] && ARCHIVE_MEMBERS+=('.ssh/authorized_keys')
[[ -f "$CONFIG_FILE" ]] && ARCHIVE_MEMBERS+=('.config/voxhub/server.toml')

ARCHIVE_PATH=""
if [[ ${#ARCHIVE_MEMBERS[@]} -eq 0 ]]; then
    skip "archive (nothing to save under $VOXHUB_HOME)"
else
    STAMP="$(date -u +%Y%m%dT%H%M%S)"
    ARCHIVE_PATH="$ARCHIVE_DIR/voxhub-uninstall-$STAMP.tar.gz"
    info "Archiving ${ARCHIVE_MEMBERS[*]} -> $ARCHIVE_PATH"
    if ! $DRY_RUN; then
        mkdir -p "$ARCHIVE_DIR"
        tar -czf "$ARCHIVE_PATH" -C "$VOXHUB_HOME" "${ARCHIVE_MEMBERS[@]}"
        chmod 600 "$ARCHIVE_PATH"
    fi
    ok "Archived to $ARCHIVE_PATH"
fi

# ===================================================================
# Step 6: Record the uid/gid for the redeploy
# ===================================================================
# See "The UID hand-off" in the header.  Written before userdel; read by
# deploy.sh, which recreates the user with these exact numbers so the
# preserved stores and backup snapshots keep resolving to a real user.
step "Record uid/gid hand-off state"

if ! $USER_EXISTS; then
    skip "uid/gid state (user '$VOXHUB_USER' absent)"
elif $KEEP_USER; then
    skip "uid/gid state (--keep-user: the user survives)"
else
    VOXHUB_UID="$("$ID_BIN" -u "$VOXHUB_USER")"
    VOXHUB_GID="$("$ID_BIN" -g "$VOXHUB_USER")"
    info "Recording uid=$VOXHUB_UID gid=$VOXHUB_GID to $STATE_FILE"
    if ! $DRY_RUN; then
        mkdir -p "$STATE_DIR"
        printf '# written by uninstall.sh — consumed by deploy.sh\nuid=%s\ngid=%s\n' \
            "$VOXHUB_UID" "$VOXHUB_GID" > "$STATE_FILE"
    fi
    ok "uid/gid recorded"
fi

# ===================================================================
# Step 7: Remove code, venv, binaries and logs
# ===================================================================
step "Remove voxhub files"

rm_path 'install dir'      "$INSTALL_DIR"
rm_path 'server symlink'   "$SERVER_BIN"
rm_path 'forced command'   "$FORCED_CMD"
rm_path 'backup script'    "$BACKUP_BIN"
rm_path 'log dir'          "$LOG_DIR"

# ===================================================================
# Step 8: Remove the uv toolchain
# ===================================================================
step "Remove uv toolchain"

if $KEEP_UV; then
    kept "uv toolchain ($UV_BIN, $UV_ROOT) — --keep-uv"
else
    rm_path 'uv binary' "$UV_BIN"
    rm_path 'uv root'   "$UV_ROOT"
fi

# rrsync: deploy.sh only ever *installs* the /usr/local/bin copy, and only on
# releases where apt ships rrsync as a gzipped doc (bullseye/bookworm).  On
# trixie the binary lives at /usr/bin/rrsync and belongs to the rsync package
# — removing that would vandalise apt's file list.  So: only the local copy,
# and never a path outside $BIN_DIR.
if [[ -e "$RRSYNC_LOCAL" ]]; then
    rm_path 'rrsync (deploy-installed copy)' "$RRSYNC_LOCAL"
else
    skip "rrsync (no deploy-installed copy at $RRSYNC_LOCAL)"
fi
if command -v rrsync &>/dev/null; then
    kept "rrsync at $(command -v rrsync) — owned by the apt rsync package, left alone"
fi

info "Apt packages (rsync, git, curl, ca-certificates) left installed — shared system tools"

# ===================================================================
# Step 9: Purge the staging directory
# ===================================================================
# Contents only.  The directory itself may be (or sit on) a mount, and
# deploy.sh recreates it anyway.  Staging is scratch by construction — gc
# reaps it on a 48h TTL — so nothing here is worth preserving, and stale
# staging dirs are exactly the kind of thing that survives a redeploy and
# confuses the next operator.
step "Purge staging directory"

if [[ -n "$STAGING_DIR" && -d "$STAGING_DIR" ]]; then
    ENTRIES=$(find "$STAGING_DIR" -mindepth 1 -maxdepth 1 | wc -l)
    if [[ "$ENTRIES" -eq 0 ]]; then
        skip "staging contents ($STAGING_DIR is empty)"
    else
        info "Removing $ENTRIES entr(y|ies) under $STAGING_DIR"
        run find "$STAGING_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
        gone "staging contents ($ENTRIES removed, $STAGING_DIR kept)"
    fi
else
    skip "staging dir (${STAGING_DIR:-unresolved})"
fi

# ===================================================================
# Step 10: Remove the system user
# ===================================================================
step "Remove system user '$VOXHUB_USER'"

if $KEEP_USER; then
    kept "user '$VOXHUB_USER' and its authorized_keys — --keep-user"
elif ! $USER_EXISTS; then
    skip "user '$VOXHUB_USER'"
else
    info "userdel -r $VOXHUB_USER (removes $VOXHUB_HOME)"
    if $DRY_RUN; then
        info "(dry-run) $USERDEL -r $VOXHUB_USER"
    else
        # -r takes the home and mail spool with it.  A missing home makes
        # userdel exit non-zero even though the account is gone; treat the
        # account's absence afterwards as the real success condition.
        "$USERDEL" -r "$VOXHUB_USER" 2>/dev/null || true
        if "$ID_BIN" "$VOXHUB_USER" &>/dev/null; then
            fail "userdel failed — '$VOXHUB_USER' still exists (processes still running?)"
        fi
        gone "user '$VOXHUB_USER' and home $VOXHUB_HOME"
    fi
fi

# ===================================================================
# Step 11: Data
# ===================================================================
step "Annotation data"

if ! $PURGE_DATA; then
    kept "stores dir:    ${STORES_DIR:-(unresolved)}"
    kept "backup target: ${BACKUP_TARGET:-(unresolved)}"
    info "Pass --purge-data to destroy them."
else
    warn "--purge-data will PERMANENTLY destroy:"
    warn "    $STORES_DIR"
    [[ -n "$BACKUP_TARGET" ]] && warn "    $BACKUP_TARGET"
    warn "Annotations and provenance cannot be re-derived from anything else."

    if $DRY_RUN; then
        info "(dry-run) purge skipped (confirmation not requested)"
    else
        # Confirmation is a verbatim re-type of the stores path — a
        # deliberately awkward gesture for a deliberately irreversible act.
        # Non-interactive callers set VOXHUB_PURGE_CONFIRM to the same value.
        CONFIRM=""
        if [[ -n "${VOXHUB_PURGE_CONFIRM:-}" ]]; then
            CONFIRM="$VOXHUB_PURGE_CONFIRM"
        elif [[ -t 0 ]]; then
            printf "\n  Type the stores path to confirm deletion: "
            read -r CONFIRM
        else
            fail "--purge-data on a non-interactive stdin requires VOXHUB_PURGE_CONFIRM=<stores-dir>"
        fi

        if [[ "$CONFIRM" != "$STORES_DIR" ]]; then
            fail "Confirmation did not match '$STORES_DIR' — nothing was deleted"
        fi

        rm_path 'stores dir' "$STORES_DIR"
        [[ -n "$BACKUP_TARGET" ]] && rm_path 'backup target' "$BACKUP_TARGET"
        rm_path 'uid/gid state' "$STATE_FILE"
    fi
fi

# ===================================================================
# Step 12: Verify
# ===================================================================
step "Verify removal"

RESIDUAL=()
check_gone() {
    local path="$1"
    if [[ -e "$path" || -L "$path" ]]; then
        RESIDUAL+=("$path")
    fi
}

if ! $DRY_RUN; then
    check_gone "$INSTALL_DIR"
    check_gone "$SERVER_BIN"
    check_gone "$FORCED_CMD"
    check_gone "$BACKUP_BIN"
    check_gone "$LOG_DIR"
    check_gone "$SSHD_CONF"
    if ! $KEEP_UV; then
        check_gone "$UV_BIN"
        check_gone "$UV_ROOT"
    fi
    if ! $KEEP_USER && "$ID_BIN" "$VOXHUB_USER" &>/dev/null; then
        RESIDUAL+=("user:$VOXHUB_USER")
    fi

    if [[ ${#RESIDUAL[@]} -gt 0 ]]; then
        for r in "${RESIDUAL[@]}"; do
            warn "still present: $r"
        done
        fail "Uninstall incomplete — ${#RESIDUAL[@]} item(s) remain (see above)"
    fi
    ok "No residual voxhub artifacts"
else
    info "(dry-run) verification skipped"
fi

# ===================================================================
# Summary
# ===================================================================
printf "\n${BOLD}${GREEN}══════════════════════════════════════════════════${RESET}\n"
if $DRY_RUN; then
    printf "${BOLD}${GREEN}  voxhub uninstall — dry run, nothing changed${RESET}\n"
else
    printf "${BOLD}${GREEN}  voxhub server uninstalled${RESET}\n"
fi
printf "${BOLD}${GREEN}══════════════════════════════════════════════════${RESET}\n\n"

if $PURGE_DATA; then
    echo "  Data:          PURGED (stores + backups)"
else
    echo "  Stores kept:   ${STORES_DIR:-(unresolved)}"
    echo "  Backups kept:  ${BACKUP_TARGET:-(unresolved)}"
fi
[[ -n "$ARCHIVE_PATH" ]] && echo "  Key archive:   $ARCHIVE_PATH"
if ! $KEEP_USER && [[ -f "$STATE_FILE" ]]; then
    echo "  uid/gid state: $STATE_FILE (deploy.sh reuses these — do not delete)"
fi

cat <<EOF

  Redeploy with:
    sudo ./deploy.sh --stores-dir ${STORES_DIR:-<path>}

EOF

if ! $KEEP_USER && ! $PURGE_DATA; then
cat <<EOF
  Then restore annotator keys from the archive:
    tar -xzf ${ARCHIVE_PATH:-<archive>} -C /home/$VOXHUB_USER
    chown -R $VOXHUB_USER:$VOXHUB_USER /home/$VOXHUB_USER/.ssh

EOF
fi

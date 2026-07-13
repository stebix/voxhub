#!/usr/bin/env bash
# deploy.sh — One-shot provisioning of a voxhub server on Debian 13 (trixie).
#
# Usage:
#   sudo ./deploy.sh --stores-dir /mnt/storage/voxhub/data
#   sudo ./deploy.sh --stores-dir /mnt/storage/voxhub/data --staging-dir /mnt/storage/voxhub/staging
#   sudo ./deploy.sh --stores-dir /mnt/storage/voxhub/data --repo-url git@github.com:org/voxhub.git
#   sudo ./deploy.sh --stores-dir /mnt/storage/voxhub/data --dry-run
#
# Idempotent — safe to re-run.  Re-running pulls latest code, re-syncs the
# venv, and re-validates.
#
# ``--staging-dir`` is optional.  When omitted, staging defaults to the
# stores-dir volume at ``<stores-dir parent>/staging`` so prepare-pull
# doesn't eat the root disk on small VPS boxes.  The server is
# authoritative over staging paths: clients never specify one.

set -euo pipefail

# ---------------------------------------------------------------------------
# Colours / helpers
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
fail()  { printf "${RED}[FAIL]${RESET}  %s\n" "$*"; exit 1; }
skip()  { printf "${GREEN}[SKIP]${RESET}  %s (already done)\n" "$*"; }

step() {
    STEP_NUM=$((STEP_NUM + 1))
    printf "\n${BOLD}── Step %d: %s${RESET}\n" "$STEP_NUM" "$*"
}

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

STORES_DIR=""
STAGING_DIR=""
BACKUP_TARGET=""
REPO_URL="git@github.com:stebix/voxhub.git"
BRANCH="main"
DRY_RUN=false
INSTALL_DIR="/opt/voxhub"
VOXHUB_USER="voxhub"
LOG_DIR="/var/log/voxhub"
FORCED_CMD="/usr/local/bin/voxhub-forced-command.sh"
# World-readable location for uv-managed Python + cache.  Must be
# traversable by the voxhub user — ``/root/.local/share/uv`` is not.
UV_ROOT="/opt/voxhub-uv"
UV_INSTALL_VERSION="${UV_INSTALL_VERSION:-0.5.11}"
STEP_NUM=0

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

usage() {
    cat <<EOF
Usage: sudo $0 --stores-dir <path> [OPTIONS]

Options:
  --stores-dir <path>    Path to the directory of zarr stores (required)
  --staging-dir <path>   Operator-authoritative staging parent (default:
                         <stores-dir parent>/staging).  Clients can never
                         override this; see docs/architecture.md.
  --backup-target <path> Snapshot target for the nightly annotation backup
                         (default: <stores-dir parent>/backups).  Point
                         this at a second disk/volume — a backup on the
                         same disk only protects against operator error.
  --repo-url <url>       Git clone URL (default: $REPO_URL)
  --branch <name>        Branch to deploy (default: $BRANCH)
  --dry-run              Show what would be done without making changes
  -h, --help             Show this help
EOF
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --stores-dir)  STORES_DIR="$2"; shift 2 ;;
        --staging-dir) STAGING_DIR="$2"; shift 2 ;;
        --backup-target) BACKUP_TARGET="$2"; shift 2 ;;
        --repo-url)    REPO_URL="$2"; shift 2 ;;
        --branch)      BRANCH="$2"; shift 2 ;;
        --dry-run)     DRY_RUN=true; shift ;;
        -h|--help)     usage ;;
        *)             fail "Unknown option: $1" ;;
    esac
done

[[ -z "$STORES_DIR" ]] && fail "--stores-dir is required"
[[ "$EUID" -ne 0 ]]   && fail "This script must be run as root (or via sudo)"

# Default staging dir to a sibling of stores_dir on the same volume —
# keeps prepare-pull off the root disk on small VPS boxes.
if [[ -z "$STAGING_DIR" ]]; then
    STORES_PARENT="$(dirname "$STORES_DIR")"
    STAGING_DIR="$STORES_PARENT/staging"
fi

# Default backup target to a sibling of stores_dir.  Good enough to start
# (protects against bad pushes / operator error); move it to a second
# disk or host via --backup-target for real disk-failure protection.
if [[ -z "$BACKUP_TARGET" ]]; then
    BACKUP_TARGET="$(dirname "$STORES_DIR")/backups"
fi

# Hard guard: settings.py rejects equal paths at load time, but refuse
# earlier here so --dry-run surfaces the misconfiguration.
if [[ "$STORES_DIR" == "$STAGING_DIR" ]]; then
    fail "--stores-dir and --staging-dir must differ"
fi

if $DRY_RUN; then
    warn "Dry-run mode — no changes will be made"
fi

# ---------------------------------------------------------------------------
# Helper: only run if not dry-run
# ---------------------------------------------------------------------------
run() {
    if $DRY_RUN; then
        info "(dry-run) $*"
    else
        "$@"
    fi
}

# ===================================================================
# Step 1: System packages
# ===================================================================
step "Install system packages"

# uv downloads its own python-build-standalone interpreter, so we don't
# need python3 / python3-venv from apt.  curl bootstraps uv itself.
REQUIRED_PKGS=(curl ca-certificates rsync git)
missing=()
for pkg in "${REQUIRED_PKGS[@]}"; do
    if ! dpkg -s "$pkg" &>/dev/null; then
        missing+=("$pkg")
    fi
done

if [[ ${#missing[@]} -gt 0 ]]; then
    info "Installing: ${missing[*]}"
    run apt-get update -qq
    run apt-get install -y -qq "${missing[@]}"
    ok "Installed ${missing[*]}"
else
    skip "System packages"
fi

# ===================================================================
# Step 1b: Provision rrsync
# ===================================================================
step "Provision rrsync"

# rrsync (restricted rsync) confines annotator rsync sessions to the
# staging root — the forced-command wrapper re-execs every rsync request
# through it (see voxhub-forced-command.sh).  Debian ships rrsync with the
# rsync package, but where depends on the release:
#   * trixie and newer: /usr/bin/rrsync (executable, ready to use)
#   * bullseye/bookworm: /usr/share/doc/rsync/scripts/rrsync.gz (gzipped
#     doc copy, not executable) — install a copy to /usr/local/bin.
# Idempotent: once a copy is on PATH (either variant), re-runs skip.
RRSYNC_LOCAL="/usr/local/bin/rrsync"
RRSYNC_DOC_GZ="/usr/share/doc/rsync/scripts/rrsync.gz"
RRSYNC_DOC="/usr/share/doc/rsync/scripts/rrsync"

if command -v rrsync &>/dev/null; then
    skip "rrsync ($(command -v rrsync))"
elif [[ -f "$RRSYNC_DOC_GZ" ]]; then
    info "Installing rrsync from $RRSYNC_DOC_GZ"
    if ! $DRY_RUN; then
        gunzip -c "$RRSYNC_DOC_GZ" > "$RRSYNC_LOCAL"
        chmod 755 "$RRSYNC_LOCAL"
    fi
    ok "Installed rrsync → $RRSYNC_LOCAL"
elif [[ -f "$RRSYNC_DOC" ]]; then
    info "Installing rrsync from $RRSYNC_DOC"
    run cp "$RRSYNC_DOC" "$RRSYNC_LOCAL"
    run chmod 755 "$RRSYNC_LOCAL"
    ok "Installed rrsync → $RRSYNC_LOCAL"
else
    fail "rrsync not found — expected it on PATH or under /usr/share/doc/rsync/scripts/ (ships with the rsync package)"
fi

# ===================================================================
# Step 2: Install uv system-wide
# ===================================================================
step "Install uv"

# Install uv into a world-readable location so both root (during deploy)
# and the voxhub user (during runtime) see the same binary.  The default
# ``curl | sh`` lands in ``$HOME/.local/bin`` — which is ``/root`` under
# ``sudo``, and ``/root`` is mode 0700.  Pin the version so redeploys
# are reproducible.
UV_BIN="/usr/local/bin/uv"

if [[ -x "$UV_BIN" ]]; then
    skip "uv ($($UV_BIN --version))"
elif $DRY_RUN; then
    ok "(dry-run) uv install skipped"
else
    info "Installing uv $UV_INSTALL_VERSION to $UV_BIN"
    INSTALLER=$(mktemp)
    trap 'rm -f "$INSTALLER"' EXIT
    curl -LsSf "https://astral.sh/uv/${UV_INSTALL_VERSION}/install.sh" -o "$INSTALLER"
    # UV_INSTALL_DIR controls where the installer drops the uv binary.
    # UV_UNMANAGED_INSTALL silences the PATH-modification nag.
    env UV_INSTALL_DIR=/usr/local/bin UV_UNMANAGED_INSTALL=1 sh "$INSTALLER"
    rm -f "$INSTALLER"
    trap - EXIT
    if [[ -x "$UV_BIN" ]]; then
        ok "Installed uv ($($UV_BIN --version))"
    else
        fail "uv installation failed — $UV_BIN not present"
    fi
fi

# ===================================================================
# Step 3: Create voxhub system user
# ===================================================================
step "Create system user '$VOXHUB_USER'"

if id "$VOXHUB_USER" &>/dev/null; then
    skip "User '$VOXHUB_USER' exists"
else
    info "Creating system user '$VOXHUB_USER'"
    run useradd \
        --system \
        --shell /usr/sbin/nologin \
        --create-home \
        --home-dir "/home/$VOXHUB_USER" \
        "$VOXHUB_USER"
    ok "Created user '$VOXHUB_USER'"
fi

VOXHUB_HOME=$(eval echo "~$VOXHUB_USER")

# ===================================================================
# Step 4: SSH hardening
# ===================================================================
step "Configure SSH for '$VOXHUB_USER'"

SSHD_CONF="/etc/ssh/sshd_config.d/voxhub.conf"

# PermitUserEnvironment is a scoped allowlist (VOXHUB_ANNOTATOR only, never a
# bare 'yes'): it lets sshd honour the per-key environment="VOXHUB_ANNOTATOR=..."
# option set by add-annotator.sh, which binds each key to an annotator identity.
# The server treats that variable as authoritative for provenance.
SSHD_BLOCK="# voxhub annotator access — managed by deploy.sh
Match User $VOXHUB_USER
    ForceCommand $FORCED_CMD
    PasswordAuthentication no
    AllowAgentForwarding no
    AllowTcpForwarding no
    X11Forwarding no
    PermitTTY no
    PermitUserEnvironment VOXHUB_ANNOTATOR"

# Re-write the config unless BOTH the forced command and the key-bound identity
# allowlist are already present — an older deployment that predates
# PermitUserEnvironment must be upgraded so key-bound identity actually takes
# effect (sshd silently ignores environment= without it).
if [[ -f "$SSHD_CONF" ]] \
    && grep -qF "ForceCommand $FORCED_CMD" "$SSHD_CONF" \
    && grep -qF "PermitUserEnvironment VOXHUB_ANNOTATOR" "$SSHD_CONF"; then
    skip "sshd config ($SSHD_CONF)"
else
    info "Writing $SSHD_CONF"
    if ! $DRY_RUN; then
        printf '%s\n' "$SSHD_BLOCK" > "$SSHD_CONF"
        chmod 644 "$SSHD_CONF"
    fi
    # Validate config before reloading
    if ! $DRY_RUN; then
        if sshd -t 2>/dev/null; then
            systemctl reload sshd
            ok "sshd config installed and reloaded"
        else
            rm -f "$SSHD_CONF"
            fail "sshd config validation failed — removed $SSHD_CONF, sshd NOT reloaded"
        fi
    else
        ok "(dry-run) sshd config"
    fi
fi

# ===================================================================
# Step 5: Install ForceCommand wrapper
# ===================================================================
step "Install ForceCommand wrapper"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WRAPPER_SRC="$SCRIPT_DIR/voxhub-forced-command.sh"

if [[ ! -f "$WRAPPER_SRC" ]]; then
    fail "Cannot find $WRAPPER_SRC — run this script from the scripts/deploy/ directory"
fi

# Render the staging root into the wrapper at install time (replaces the
# @STAGING_ROOT@ token — see the comment block in voxhub-forced-command.sh
# for why install-time rendering was chosen over run-time TOML parsing).
# Pure-bash substitution, no sed: an arbitrary $STAGING_DIR (spaces,
# slashes, &) can never corrupt the rendered script.
WRAPPER_CONTENT="$(<"$WRAPPER_SRC")"
WRAPPER_RENDERED="${WRAPPER_CONTENT//@STAGING_ROOT@/$STAGING_DIR}"

# Idempotency: compare the *rendered* content against the installed file.
# Re-runs with unchanged inputs converge to skip; installs still carrying
# the old allowlist wrapper — or a stale staging root — differ and are
# upgraded in place.
if [[ -f "$FORCED_CMD" ]] && printf '%s\n' "$WRAPPER_RENDERED" | cmp -s - "$FORCED_CMD"; then
    skip "ForceCommand wrapper ($FORCED_CMD)"
else
    info "Installing $FORCED_CMD (staging root: $STAGING_DIR)"
    if ! $DRY_RUN; then
        printf '%s\n' "$WRAPPER_RENDERED" > "$FORCED_CMD"
        chmod 755 "$FORCED_CMD"
    fi
    ok "Installed forced command wrapper"
fi

# ===================================================================
# Step 6: Clone / update repository
# ===================================================================
step "Clone / update repository"

if [[ -d "$INSTALL_DIR/.git" ]]; then
    info "Updating existing clone at $INSTALL_DIR"
    if ! $DRY_RUN; then
        # Warn loudly if an operator hotfixed on the server — the reset
        # below would silently discard it.
        if ! git -C "$INSTALL_DIR" diff --quiet HEAD 2>/dev/null; then
            warn "Uncommitted changes in $INSTALL_DIR will be discarded by reset --hard"
        fi
        git -C "$INSTALL_DIR" fetch origin
        git -C "$INSTALL_DIR" checkout "$BRANCH"
        git -C "$INSTALL_DIR" reset --hard "origin/$BRANCH"
    fi
    ok "Updated to latest origin/$BRANCH"
else
    info "Cloning $REPO_URL ($BRANCH) into $INSTALL_DIR"
    run git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
    ok "Cloned repository"
fi

# The voxhub runtime user must be able to read/execute everything under
# $INSTALL_DIR — including .venv/bin/python and the site-packages tree.
# Chown before ``uv sync`` so the venv is created owned by voxhub from
# the start, and uv's cache/python downloads (below) land in paths we
# control.
run chown -R "$VOXHUB_USER:$VOXHUB_USER" "$INSTALL_DIR"

# ===================================================================
# Step 7: Install Python packages (uv sync)
# ===================================================================
step "Install Python packages"

# Thread the uv workflow through cleanly:
#
#   * UV_PYTHON_INSTALL_DIR — where uv downloads python-build-standalone.
#     Must be world-readable because ``.venv/bin/python`` symlinks into
#     it, and the voxhub user needs to traverse the path at runtime.
#     (/root/.local/share/uv — uv's default under ``sudo`` — is 0700.)
#   * UV_CACHE_DIR — keep uv's wheel cache alongside the python install
#     so redeploys don't redownload.  Also voxhub-readable for future
#     ``uv sync`` re-runs by the voxhub user.
#   * UV_PYTHON_PREFERENCE=only-managed — never fall back to a system
#     interpreter.  Guarantees the .venv shebang points into UV_ROOT,
#     which we control, regardless of what apt happens to ship.
#   * --frozen — respect uv.lock exactly; no implicit resolver drift.
#   * --package voxhub-core — install only the server package from the
#     workspace.  Avoids the ``voxhub`` script-name collision between
#     voxhub-core and voxhub-client (we only need voxhub-server here).
#   * --no-dev — skip the ``dev`` dependency group (ruff, pyright,
#     pytest) on production boxes.
info "Provisioning uv Python + venv under $UV_ROOT"
if ! $DRY_RUN; then
    mkdir -p "$UV_ROOT"
    chown -R "$VOXHUB_USER:$VOXHUB_USER" "$UV_ROOT"
    chmod 755 "$UV_ROOT"
fi

UV_ENV=(
    env
    "UV_PYTHON_INSTALL_DIR=$UV_ROOT/python"
    "UV_CACHE_DIR=$UV_ROOT/cache"
    "UV_PYTHON_PREFERENCE=only-managed"
    "HOME=/home/$VOXHUB_USER"
)

info "Running uv sync in $INSTALL_DIR"
if ! $DRY_RUN; then
    sudo -u "$VOXHUB_USER" -H "${UV_ENV[@]}" "$UV_BIN" sync \
        --project "$INSTALL_DIR" \
        --package voxhub-core \
        --frozen \
        --no-dev
fi

VENV_BIN="$INSTALL_DIR/.venv/bin"

# Symlink only voxhub-server (the single public entrypoint on the
# server) into /usr/local/bin.  We intentionally do not symlink the
# whole .venv: entrypoint scripts carry absolute shebangs pointing
# back into $INSTALL_DIR/.venv/bin/python, so a single symlink is
# sufficient and keeps /usr/local/bin tidy.
if [[ -f "$VENV_BIN/voxhub-server" ]] || $DRY_RUN; then
    run ln -sf "$VENV_BIN/voxhub-server" /usr/local/bin/voxhub-server
    ok "Symlinked voxhub-server → /usr/local/bin/"
else
    fail "voxhub-server not found in $VENV_BIN after uv sync"
fi

# Sanity-check the interpreter is reachable as the voxhub user — this
# is exactly the failure mode that bit us when uv dropped python under
# /root.  Skip in dry-run (nothing to check).
if ! $DRY_RUN; then
    if ! sudo -u "$VOXHUB_USER" test -x "$VENV_BIN/python"; then
        fail "venv python not executable as $VOXHUB_USER — check ownership of $INSTALL_DIR and $UV_ROOT"
    fi
    if ! sudo -u "$VOXHUB_USER" "$VENV_BIN/python" -c 'import voxhub_core' 2>/dev/null; then
        fail "voxhub_core not importable from $VENV_BIN/python as $VOXHUB_USER"
    fi
    ok "venv Python reachable and voxhub_core importable as $VOXHUB_USER"
fi

# ===================================================================
# Step 8: Prepare stores directory
# ===================================================================
step "Prepare stores directory"

# Check if stores directory is on a mount — warn if mount isn't active
STORES_MOUNT=$(df --output=target "$STORES_DIR" 2>/dev/null | tail -1 || true)
if [[ -n "$STORES_MOUNT" && "$STORES_MOUNT" != "/" ]]; then
    info "Stores directory is on mount: $STORES_MOUNT"
    if ! grep -qsF "$STORES_MOUNT" /etc/fstab; then
        warn "Mount $STORES_MOUNT is NOT in /etc/fstab — it may not survive a reboot"
    fi
fi

if [[ -d "$STORES_DIR" ]]; then
    skip "Stores directory ($STORES_DIR)"
else
    info "Creating $STORES_DIR"
    run mkdir -p "$STORES_DIR"
fi

run mkdir -p "$STORES_DIR/.meta"
run chown -R "$VOXHUB_USER:$VOXHUB_USER" "$STORES_DIR"
ok "Stores directory ready at $STORES_DIR"

# ===================================================================
# Step 8b: Prepare staging directory
# ===================================================================
step "Prepare staging directory"

if [[ -d "$STAGING_DIR" ]]; then
    skip "Staging directory ($STAGING_DIR)"
else
    info "Creating $STAGING_DIR"
    run mkdir -p "$STAGING_DIR"
fi

run chown -R "$VOXHUB_USER:$VOXHUB_USER" "$STAGING_DIR"
# Restrict to the voxhub user — world-writable /tmp-like perms would let
# any local account plant a ``vxhb-staging-*`` dir that gc would later
# reap, wasting cycles at best and surprising operators at worst.
run chmod 700 "$STAGING_DIR"
ok "Staging directory ready at $STAGING_DIR"

# ===================================================================
# Step 9: Server configuration (TOML)
# ===================================================================
step "Write server configuration"

CONFIG_DIR="$VOXHUB_HOME/.config/voxhub"
CONFIG_FILE="$CONFIG_DIR/server.toml"

CONFIG_CONTENT="[logging]
log_file = \"$LOG_DIR/debug.log\"
log_max_bytes = 52428800   # 50 MB
log_backup_count = 10
stderr_level = \"WARNING\"

[storage]
stores_dir = \"$STORES_DIR\"
staging_dir = \"$STAGING_DIR\""

if [[ -f "$CONFIG_FILE" ]]; then
    skip "Server config ($CONFIG_FILE)"
else
    info "Writing $CONFIG_FILE"
    if ! $DRY_RUN; then
        mkdir -p "$CONFIG_DIR"
        printf '%s\n' "$CONFIG_CONTENT" > "$CONFIG_FILE"
        chown -R "$VOXHUB_USER:$VOXHUB_USER" "$CONFIG_DIR"
    fi
    ok "Server config written"
fi

# ===================================================================
# Step 10: Log directory
# ===================================================================
step "Create log directory"

if [[ -d "$LOG_DIR" ]]; then
    skip "Log directory ($LOG_DIR)"
else
    run mkdir -p "$LOG_DIR"
    run chown "$VOXHUB_USER:$VOXHUB_USER" "$LOG_DIR"
    ok "Created $LOG_DIR"
fi

# ===================================================================
# Step 11: GC cron job
# ===================================================================
step "Install GC cron job"

GC_CRON="0 4 * * * /usr/local/bin/voxhub-server gc --ttl-hours 48"

if crontab -u "$VOXHUB_USER" -l 2>/dev/null | grep -qF "voxhub-server gc"; then
    skip "GC cron job"
else
    info "Adding daily GC cron (04:00, TTL 48h)"
    if ! $DRY_RUN; then
        (crontab -u "$VOXHUB_USER" -l 2>/dev/null || true; echo "$GC_CRON") \
            | crontab -u "$VOXHUB_USER" -
    fi
    ok "GC cron installed"
fi

# ===================================================================
# Step 11b: Backup script + cron job
# ===================================================================
step "Install backup script + cron job"

BACKUP_SRC="$SCRIPT_DIR/backup.sh"
BACKUP_BIN="/usr/local/bin/voxhub-backup.sh"
BACKUP_LOG="$LOG_DIR/backup.log"

if [[ ! -f "$BACKUP_SRC" ]]; then
    fail "Cannot find $BACKUP_SRC — run this script from the scripts/deploy/ directory"
fi

# Same idempotency pattern as the ForceCommand wrapper: install/upgrade
# in place only when the content differs.
if [[ -f "$BACKUP_BIN" ]] && cmp -s "$BACKUP_SRC" "$BACKUP_BIN"; then
    skip "Backup script ($BACKUP_BIN)"
else
    info "Installing $BACKUP_BIN"
    if ! $DRY_RUN; then
        cp "$BACKUP_SRC" "$BACKUP_BIN"
        chmod 755 "$BACKUP_BIN"
    fi
    ok "Installed backup script"
fi

# The backup target must be writable by the voxhub cron user.
if [[ -d "$BACKUP_TARGET" ]]; then
    skip "Backup target ($BACKUP_TARGET)"
else
    info "Creating $BACKUP_TARGET"
    run mkdir -p "$BACKUP_TARGET"
fi
run chown "$VOXHUB_USER:$VOXHUB_USER" "$BACKUP_TARGET"

BACKUP_CRON="30 3 * * * $BACKUP_BIN --stores-dir $STORES_DIR --target $BACKUP_TARGET --log-file $BACKUP_LOG"

# Same idempotent pattern as the gc cron above.
if crontab -u "$VOXHUB_USER" -l 2>/dev/null | grep -qF "voxhub-backup"; then
    skip "Backup cron job"
else
    info "Adding nightly backup cron (03:30, 14-day retention)"
    if ! $DRY_RUN; then
        (crontab -u "$VOXHUB_USER" -l 2>/dev/null || true; echo "$BACKUP_CRON") \
            | crontab -u "$VOXHUB_USER" -
    fi
    ok "Backup cron installed"
fi

# ===================================================================
# Step 12: SSH key directory
# ===================================================================
step "Prepare SSH authorized_keys"

SSH_DIR="$VOXHUB_HOME/.ssh"
AUTH_KEYS="$SSH_DIR/authorized_keys"

if [[ -f "$AUTH_KEYS" ]]; then
    KEY_COUNT=$(wc -l < "$AUTH_KEYS" | tr -d ' ')
    skip "authorized_keys ($KEY_COUNT key(s) present)"
else
    info "Creating $AUTH_KEYS"
    if ! $DRY_RUN; then
        mkdir -p "$SSH_DIR"
        touch "$AUTH_KEYS"
        chmod 700 "$SSH_DIR"
        chmod 600 "$AUTH_KEYS"
        chown -R "$VOXHUB_USER:$VOXHUB_USER" "$SSH_DIR"
    fi
    ok "SSH directory ready (no keys yet — use add-annotator.sh)"
fi

# ===================================================================
# Step 13: Run healthcheck
# ===================================================================
step "Run healthcheck"

if $DRY_RUN; then
    ok "(dry-run) healthcheck skipped"
else
    info "Running: voxhub-server healthcheck"
    if sudo -u "$VOXHUB_USER" /usr/local/bin/voxhub-server healthcheck; then
        ok "Healthcheck passed"
    else
        warn "Healthcheck reported issues (see output above) — may be expected on fresh install"
    fi
fi

# ===================================================================
# Summary
# ===================================================================
printf "\n${BOLD}${GREEN}══════════════════════════════════════════════════${RESET}\n"
printf "${BOLD}${GREEN}  voxhub server deployment complete${RESET}\n"
printf "${BOLD}${GREEN}══════════════════════════════════════════════════${RESET}\n\n"

cat <<EOF
  Install dir:   $INSTALL_DIR
  uv root:       $UV_ROOT (python + cache, world-readable)
  Stores dir:    $STORES_DIR
  Staging dir:   $STAGING_DIR
  Log dir:       $LOG_DIR
  Server config: $CONFIG_FILE
  SSH config:    $SSHD_CONF
  GC cron:       daily at 04:00 (48h TTL, reaps $STAGING_DIR)
  Backup cron:   daily at 03:30 (annotations + .meta -> $BACKUP_TARGET,
                 hardlink snapshots, 14-day retention, log: $LOG_DIR/backup.log)

  Next steps:
    1. Add annotator keys:
       sudo ./add-annotator.sh <name> <pubkey.pub>

    2. Annotators connect with:
       voxhub pull $VOXHUB_USER@<server-ip> ./local_staging
       voxhub push ./local_staging

EOF

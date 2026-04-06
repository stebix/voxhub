#!/usr/bin/env bash
# deploy.sh — One-shot provisioning of a voxhub server on Debian 13 (trixie).
#
# Usage:
#   sudo ./deploy.sh --zarr-root /mnt/storage/voxhub/data
#   sudo ./deploy.sh --zarr-root /mnt/storage/voxhub/data --repo-url git@github.com:org/voxhub.git
#   sudo ./deploy.sh --zarr-root /mnt/storage/voxhub/data --dry-run
#
# Idempotent — safe to re-run.  Re-running pulls latest code, re-syncs the
# venv, and re-validates.

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

ZARR_ROOT=""
REPO_URL="https://github.com/jnickla1/voxhub.git"
BRANCH="main"
DRY_RUN=false
INSTALL_DIR="/opt/voxhub"
VOXHUB_USER="voxhub"
LOG_DIR="/var/log/voxhub"
FORCED_CMD="/usr/local/bin/voxhub-forced-command.sh"
STEP_NUM=0

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

usage() {
    cat <<EOF
Usage: sudo $0 --zarr-root <path> [OPTIONS]

Options:
  --zarr-root <path>     Path to the zarr store directory (required)
  --repo-url <url>       Git clone URL (default: $REPO_URL)
  --branch <name>        Branch to deploy (default: $BRANCH)
  --dry-run              Show what would be done without making changes
  -h, --help             Show this help
EOF
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --zarr-root)   ZARR_ROOT="$2"; shift 2 ;;
        --repo-url)    REPO_URL="$2"; shift 2 ;;
        --branch)      BRANCH="$2"; shift 2 ;;
        --dry-run)     DRY_RUN=true; shift ;;
        -h|--help)     usage ;;
        *)             fail "Unknown option: $1" ;;
    esac
done

[[ -z "$ZARR_ROOT" ]] && fail "--zarr-root is required"
[[ "$EUID" -ne 0 ]]   && fail "This script must be run as root (or via sudo)"

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

REQUIRED_PKGS=(python3 python3-venv rsync git)
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

# Verify Python >= 3.12
PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
info "Python version: $PYTHON_VERSION"
if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then
    ok "Python >= 3.12"
else
    fail "Python 3.12+ required, found $PYTHON_VERSION"
fi

# ===================================================================
# Step 2: Install uv
# ===================================================================
step "Install uv"

if command -v uv &>/dev/null; then
    skip "uv ($(uv --version))"
else
    info "Installing uv"
    run curl -LsSf https://astral.sh/uv/install.sh | run sh
    # Ensure uv is on PATH for the rest of this script
    export PATH="$HOME/.local/bin:$PATH"
    if command -v uv &>/dev/null; then
        ok "Installed uv ($(uv --version))"
    elif $DRY_RUN; then
        ok "(dry-run) uv install skipped"
    else
        fail "uv installation failed"
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

SSHD_BLOCK="# voxhub annotator access — managed by deploy.sh
Match User $VOXHUB_USER
    ForceCommand $FORCED_CMD
    PasswordAuthentication no
    AllowAgentForwarding no
    AllowTcpForwarding no
    X11Forwarding no
    PermitTTY no"

if [[ -f "$SSHD_CONF" ]] && grep -qF "ForceCommand $FORCED_CMD" "$SSHD_CONF"; then
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

if [[ -f "$FORCED_CMD" ]] && cmp -s "$WRAPPER_SRC" "$FORCED_CMD"; then
    skip "ForceCommand wrapper ($FORCED_CMD)"
else
    info "Installing $FORCED_CMD"
    run cp "$WRAPPER_SRC" "$FORCED_CMD"
    run chmod 755 "$FORCED_CMD"
    ok "Installed forced command wrapper"
fi

# ===================================================================
# Step 6: Clone / update repository
# ===================================================================
step "Clone / update repository"

if [[ -d "$INSTALL_DIR/.git" ]]; then
    info "Updating existing clone at $INSTALL_DIR"
    if ! $DRY_RUN; then
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

# ===================================================================
# Step 7: Install Python packages (uv sync)
# ===================================================================
step "Install Python packages"

info "Running uv sync in $INSTALL_DIR"
if ! $DRY_RUN; then
    (cd "$INSTALL_DIR" && uv sync)
fi

VENV_BIN="$INSTALL_DIR/.venv/bin"

# Symlink voxhub-server into /usr/local/bin
if [[ -f "$VENV_BIN/voxhub-server" ]] || $DRY_RUN; then
    run ln -sf "$VENV_BIN/voxhub-server" /usr/local/bin/voxhub-server
    ok "Symlinked voxhub-server → /usr/local/bin/"
else
    fail "voxhub-server not found in $VENV_BIN after uv sync"
fi

# ===================================================================
# Step 8: Prepare zarr root
# ===================================================================
step "Prepare zarr root directory"

# Check if zarr root is on a mount — warn if mount isn't active
ZARR_MOUNT=$(df --output=target "$ZARR_ROOT" 2>/dev/null | tail -1 || true)
if [[ -n "$ZARR_MOUNT" && "$ZARR_MOUNT" != "/" ]]; then
    info "Zarr root is on mount: $ZARR_MOUNT"
    if ! grep -qsF "$ZARR_MOUNT" /etc/fstab; then
        warn "Mount $ZARR_MOUNT is NOT in /etc/fstab — it may not survive a reboot"
    fi
fi

if [[ -d "$ZARR_ROOT" ]]; then
    skip "Zarr root directory ($ZARR_ROOT)"
else
    info "Creating $ZARR_ROOT"
    run mkdir -p "$ZARR_ROOT"
fi

run mkdir -p "$ZARR_ROOT/.meta"
run chown -R "$VOXHUB_USER:$VOXHUB_USER" "$ZARR_ROOT"
ok "Zarr root ready at $ZARR_ROOT"

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
stderr_level = \"WARNING\""

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
    info "Running: voxhub-server healthcheck $ZARR_ROOT"
    if sudo -u "$VOXHUB_USER" /usr/local/bin/voxhub-server healthcheck "$ZARR_ROOT"; then
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
  Zarr root:     $ZARR_ROOT
  Log dir:       $LOG_DIR
  Server config: $CONFIG_FILE
  SSH config:    $SSHD_CONF
  GC cron:       daily at 04:00 (48h TTL)

  Next steps:
    1. Add annotator keys:
       sudo ./add-annotator.sh <name> <pubkey.pub>

    2. Annotators connect with:
       voxhub pull $VOXHUB_USER@<server-ip>:$ZARR_ROOT ./local_wip
       voxhub push ./local_wip

EOF

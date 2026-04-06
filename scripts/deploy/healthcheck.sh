#!/usr/bin/env bash
# healthcheck.sh — Operational status check for the voxhub server.
#
# Usage:
#   sudo ./healthcheck.sh --zarr-root /mnt/storage/voxhub/data
#   sudo ./healthcheck.sh                    # reads from server.toml
#
# Runs the voxhub-server healthcheck, checks disk space, and verifies the GC
# cron is registered.

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

pass() { printf "  ${GREEN}PASS${RESET}  %s\n" "$*"; }
warn() { printf "  ${YELLOW}WARN${RESET}  %s\n" "$*"; }
err()  { printf "  ${RED}FAIL${RESET}  %s\n" "$*"; ERRORS=$((ERRORS + 1)); }

VOXHUB_USER="voxhub"
ZARR_ROOT=""
ERRORS=0

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

while [[ $# -gt 0 ]]; do
    case "$1" in
        --zarr-root) ZARR_ROOT="$2"; shift 2 ;;
        -h|--help)
            echo "Usage: sudo $0 [--zarr-root <path>]"
            exit 0
            ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

# If no --zarr-root, try to infer from recent invocations or just require it
if [[ -z "$ZARR_ROOT" ]]; then
    echo "Usage: sudo $0 --zarr-root <path>"
    echo "  (--zarr-root is required)"
    exit 1
fi

printf "\n${BOLD}voxhub server healthcheck${RESET}\n"
printf "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}\n\n"

# ---------------------------------------------------------------------------
# 1. voxhub-server binary
# ---------------------------------------------------------------------------

printf "${CYAN}Binary${RESET}\n"

if command -v voxhub-server &>/dev/null; then
    pass "voxhub-server on PATH ($(which voxhub-server))"
else
    err "voxhub-server not found on PATH"
fi

# ---------------------------------------------------------------------------
# 2. voxhub-server healthcheck
# ---------------------------------------------------------------------------

printf "\n${CYAN}Server healthcheck${RESET}\n"

if command -v voxhub-server &>/dev/null; then
    HC_OUTPUT=$(sudo -u "$VOXHUB_USER" voxhub-server healthcheck "$ZARR_ROOT" 2>&1) && HC_EXIT=0 || HC_EXIT=$?
    if [[ $HC_EXIT -eq 0 ]]; then
        pass "voxhub-server healthcheck passed"
    else
        err "voxhub-server healthcheck failed (exit $HC_EXIT)"
        echo "$HC_OUTPUT" | sed 's/^/        /'
    fi
else
    err "Skipped (voxhub-server not available)"
fi

# ---------------------------------------------------------------------------
# 3. Disk space
# ---------------------------------------------------------------------------

printf "\n${CYAN}Disk space${RESET}\n"

if [[ -d "$ZARR_ROOT" ]]; then
    DISK_INFO=$(df -h "$ZARR_ROOT" | tail -1)
    USAGE_PCT=$(echo "$DISK_INFO" | awk '{print $5}' | tr -d '%')
    AVAIL=$(echo "$DISK_INFO" | awk '{print $4}')
    MOUNT=$(echo "$DISK_INFO" | awk '{print $6}')

    if [[ "$USAGE_PCT" -ge 90 ]]; then
        err "Disk usage ${USAGE_PCT}% on $MOUNT ($AVAIL available)"
    elif [[ "$USAGE_PCT" -ge 75 ]]; then
        warn "Disk usage ${USAGE_PCT}% on $MOUNT ($AVAIL available)"
    else
        pass "Disk usage ${USAGE_PCT}% on $MOUNT ($AVAIL available)"
    fi
else
    err "Zarr root not found: $ZARR_ROOT"
fi

# ---------------------------------------------------------------------------
# 4. SSH config
# ---------------------------------------------------------------------------

printf "\n${CYAN}SSH configuration${RESET}\n"

SSHD_CONF="/etc/ssh/sshd_config.d/voxhub.conf"
if [[ -f "$SSHD_CONF" ]]; then
    pass "sshd config present ($SSHD_CONF)"
else
    err "sshd config missing ($SSHD_CONF)"
fi

FORCED_CMD="/usr/local/bin/voxhub-forced-command.sh"
if [[ -x "$FORCED_CMD" ]]; then
    pass "ForceCommand wrapper executable ($FORCED_CMD)"
else
    err "ForceCommand wrapper missing or not executable ($FORCED_CMD)"
fi

# Authorized keys
VOXHUB_HOME=$(eval echo "~$VOXHUB_USER")
AUTH_KEYS="$VOXHUB_HOME/.ssh/authorized_keys"
if [[ -f "$AUTH_KEYS" ]]; then
    KEY_COUNT=$(wc -l < "$AUTH_KEYS" | tr -d ' ')
    if [[ "$KEY_COUNT" -eq 0 ]]; then
        warn "authorized_keys exists but is empty (no annotators)"
    else
        pass "$KEY_COUNT annotator key(s) in authorized_keys"
    fi
else
    warn "authorized_keys not found"
fi

# ---------------------------------------------------------------------------
# 5. GC cron
# ---------------------------------------------------------------------------

printf "\n${CYAN}Maintenance${RESET}\n"

if crontab -u "$VOXHUB_USER" -l 2>/dev/null | grep -q "voxhub-server gc"; then
    GC_LINE=$(crontab -u "$VOXHUB_USER" -l 2>/dev/null | grep "voxhub-server gc")
    pass "GC cron registered: $GC_LINE"
else
    warn "No GC cron job found for $VOXHUB_USER"
fi

# ---------------------------------------------------------------------------
# 6. Log directory
# ---------------------------------------------------------------------------

printf "\n${CYAN}Logging${RESET}\n"

LOG_DIR="/var/log/voxhub"
if [[ -d "$LOG_DIR" ]]; then
    LOG_OWNER=$(stat -c '%U' "$LOG_DIR")
    if [[ "$LOG_OWNER" == "$VOXHUB_USER" ]]; then
        pass "Log directory $LOG_DIR (owner: $VOXHUB_USER)"
    else
        warn "Log directory $LOG_DIR owned by '$LOG_OWNER', expected '$VOXHUB_USER'"
    fi
else
    warn "Log directory $LOG_DIR does not exist"
fi

CONFIG_FILE="$VOXHUB_HOME/.config/voxhub/server.toml"
if [[ -f "$CONFIG_FILE" ]]; then
    pass "Server config present ($CONFIG_FILE)"
else
    warn "Server config not found ($CONFIG_FILE) — using defaults"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo
if [[ $ERRORS -eq 0 ]]; then
    printf "${BOLD}${GREEN}All checks passed.${RESET}\n\n"
    exit 0
else
    printf "${BOLD}${RED}%d check(s) failed.${RESET}\n\n" "$ERRORS"
    exit 1
fi

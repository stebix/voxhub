#!/usr/bin/env bash
# remove-annotator.sh — Revoke an annotator's SSH access to the voxhub server.
#
# Usage:
#   sudo ./remove-annotator.sh alice
#   sudo ./remove-annotator.sh alice --force   # skip confirmation prompt

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BOLD='\033[1m'
RESET='\033[0m'

fail() { printf "${RED}[FAIL]${RESET} %s\n" "$*" >&2; exit 1; }
ok()   { printf "${GREEN}[OK]${RESET}   %s\n" "$*"; }

VOXHUB_USER="voxhub"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

usage() {
    cat <<EOF
Usage: sudo $0 <annotator-name> [--force]

Arguments:
  annotator-name   The annotator label used when the key was added
  --force          Skip confirmation prompt

Removes all keys tagged with annotator:<name> from
~$VOXHUB_USER/.ssh/authorized_keys.
EOF
    exit 0
}

[[ $# -lt 1 ]] && usage
[[ "$1" == "-h" || "$1" == "--help" ]] && usage

ANNOTATOR_NAME="$1"
FORCE=false
[[ "${2:-}" == "--force" ]] && FORCE=true

[[ "$EUID" -ne 0 ]] && fail "This script must be run as root (or via sudo)"

# ---------------------------------------------------------------------------
# Find matching keys
# ---------------------------------------------------------------------------

VOXHUB_HOME=$(eval echo "~$VOXHUB_USER")
AUTH_KEYS="$VOXHUB_HOME/.ssh/authorized_keys"

[[ -f "$AUTH_KEYS" ]] || fail "authorized_keys not found at $AUTH_KEYS — nothing to remove"

# Match on the comment tag (word boundary: space before, space or EOL after)
MATCHING_LINES=$(grep -n "annotator:$ANNOTATOR_NAME " "$AUTH_KEYS" || true)

if [[ -z "$MATCHING_LINES" ]]; then
    fail "No keys found for annotator '$ANNOTATOR_NAME'"
fi

MATCH_COUNT=$(echo "$MATCHING_LINES" | wc -l)

echo
printf "${BOLD}Found %d key(s) for annotator '%s':${RESET}\n\n" "$MATCH_COUNT" "$ANNOTATOR_NAME"

# Show summary of each matching key
while IFS= read -r line; do
    LINE_NUM="${line%%:*}"
    LINE_CONTENT="${line#*:}"
    # Extract key type and comment
    KEY_TYPE=$(echo "$LINE_CONTENT" | grep -oP '(ssh-(ed25519|rsa|ecdsa)|ecdsa-sha2-\S+)' | head -1)
    ADDED_DATE=$(echo "$LINE_CONTENT" | grep -oP 'added:\S+' || echo "added:unknown")
    printf "  Line %-4s  %-16s  %s\n" "$LINE_NUM" "$KEY_TYPE" "$ADDED_DATE"
done <<< "$MATCHING_LINES"
echo

# ---------------------------------------------------------------------------
# Confirm
# ---------------------------------------------------------------------------

if ! $FORCE; then
    printf "Remove %d key(s)? [y/N] " "$MATCH_COUNT"
    read -r CONFIRM
    if [[ "$CONFIRM" != "y" && "$CONFIRM" != "Y" ]]; then
        echo "Aborted."
        exit 0
    fi
fi

# ---------------------------------------------------------------------------
# Remove
# ---------------------------------------------------------------------------

# Use grep -v to remove matching lines, write to temp then replace
TEMP=$(mktemp)
grep -v "annotator:$ANNOTATOR_NAME " "$AUTH_KEYS" > "$TEMP" || true
cp "$TEMP" "$AUTH_KEYS"
rm -f "$TEMP"
chown "$VOXHUB_USER:$VOXHUB_USER" "$AUTH_KEYS"
chmod 600 "$AUTH_KEYS"

ok "Removed $MATCH_COUNT key(s) for annotator '$ANNOTATOR_NAME'"

REMAINING=$(wc -l < "$AUTH_KEYS" | tr -d ' ')
echo "  $REMAINING key(s) remaining in authorized_keys"
echo

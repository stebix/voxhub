#!/usr/bin/env bash
# add-annotator.sh — Add an annotator's SSH public key to the voxhub server.
#
# Usage:
#   sudo ./add-annotator.sh alice /path/to/alice_ed25519.pub
#   sudo ./add-annotator.sh bob /path/to/bob_rsa.pub
#
# The key is written to ~voxhub/.ssh/authorized_keys with per-key forced
# command restrictions and an annotator:<name> comment for easy lookup.

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BOLD='\033[1m'
RESET='\033[0m'

fail() { printf "${RED}[FAIL]${RESET} %s\n" "$*" >&2; exit 1; }
ok()   { printf "${GREEN}[OK]${RESET}   %s\n" "$*"; }
warn() { printf "${YELLOW}[WARN]${RESET} %s\n" "$*"; }

VOXHUB_USER="voxhub"
FORCED_CMD="/usr/local/bin/voxhub-forced-command.sh"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

usage() {
    cat <<EOF
Usage: sudo $0 <annotator-name> <pubkey-file>

Arguments:
  annotator-name   Human-readable label (e.g. alice, bob)
  pubkey-file      Path to the SSH public key file (.pub)

The key is added to ~$VOXHUB_USER/.ssh/authorized_keys with forced-command
restrictions.  The annotator name is stored in the key comment for bookkeeping.
EOF
    exit 0
}

[[ $# -lt 2 ]] && usage
[[ "$1" == "-h" || "$1" == "--help" ]] && usage

ANNOTATOR_NAME="$1"
PUBKEY_FILE="$2"

[[ "$EUID" -ne 0 ]] && fail "This script must be run as root (or via sudo)"

# ---------------------------------------------------------------------------
# Validate inputs
# ---------------------------------------------------------------------------

# Name: alphanumeric, hyphens, underscores only
if [[ ! "$ANNOTATOR_NAME" =~ ^[a-zA-Z0-9_-]+$ ]]; then
    fail "Annotator name must be alphanumeric (hyphens/underscores allowed): '$ANNOTATOR_NAME'"
fi

# Pubkey file exists and is readable
[[ -f "$PUBKEY_FILE" ]] || fail "Public key file not found: $PUBKEY_FILE"
[[ -r "$PUBKEY_FILE" ]] || fail "Public key file not readable: $PUBKEY_FILE"

# Validate it looks like an SSH public key
PUBKEY_CONTENT=$(cat "$PUBKEY_FILE")
if [[ ! "$PUBKEY_CONTENT" =~ ^(ssh-(ed25519|rsa|ecdsa)|ecdsa-sha2) ]]; then
    fail "File does not look like an SSH public key: $PUBKEY_FILE"
fi

# Extract just the key type and key data (strip any existing comment)
KEY_TYPE=$(echo "$PUBKEY_CONTENT" | awk '{print $1}')
KEY_DATA=$(echo "$PUBKEY_CONTENT" | awk '{print $2}')

[[ -z "$KEY_DATA" ]] && fail "Could not parse key data from $PUBKEY_FILE"

# ---------------------------------------------------------------------------
# Check for duplicates
# ---------------------------------------------------------------------------

VOXHUB_HOME=$(eval echo "~$VOXHUB_USER")
AUTH_KEYS="$VOXHUB_HOME/.ssh/authorized_keys"

[[ -f "$AUTH_KEYS" ]] || fail "authorized_keys not found at $AUTH_KEYS — run deploy.sh first"

# Check by key data (not comment)
if grep -qF "$KEY_DATA" "$AUTH_KEYS"; then
    EXISTING_COMMENT=$(grep -F "$KEY_DATA" "$AUTH_KEYS" | head -1 | grep -oP 'annotator:\S+' || echo "(unknown)")
    fail "This key is already registered ($EXISTING_COMMENT)"
fi

# Check by annotator name
if grep -qF "annotator:$ANNOTATOR_NAME " "$AUTH_KEYS"; then
    warn "An existing key is already registered for 'annotator:$ANNOTATOR_NAME'"
    warn "Proceeding — the annotator will have multiple keys"
fi

# ---------------------------------------------------------------------------
# Build authorized_keys entry
# ---------------------------------------------------------------------------

DATE=$(date +%Y-%m-%d)
COMMENT="annotator:$ANNOTATOR_NAME added:$DATE"
KEY_OPTS='command="/usr/local/bin/voxhub-forced-command.sh",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty'

AUTH_LINE="$KEY_OPTS $KEY_TYPE $KEY_DATA $COMMENT"

# ---------------------------------------------------------------------------
# Append
# ---------------------------------------------------------------------------

echo "$AUTH_LINE" >> "$AUTH_KEYS"
chown "$VOXHUB_USER:$VOXHUB_USER" "$AUTH_KEYS"
chmod 600 "$AUTH_KEYS"

# Compute fingerprint for confirmation
FINGERPRINT=$(ssh-keygen -lf "$PUBKEY_FILE" 2>/dev/null | awk '{print $2}' || echo "(unknown)")

ok "Added annotator '$ANNOTATOR_NAME'"
echo
echo "  Name:        $ANNOTATOR_NAME"
echo "  Key type:    $KEY_TYPE"
echo "  Fingerprint: $FINGERPRINT"
echo "  Date:        $DATE"
echo
echo "  The annotator can now connect with:"
echo "    voxhub pull $VOXHUB_USER@<server-ip>:<zarr-root> ./local_wip"
echo

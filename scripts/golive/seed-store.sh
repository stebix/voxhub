#!/usr/bin/env bash
# seed-store.sh — Seed a synthetic, clearly-marked zarr store for the go-live
# test (docs/testing/go-live-test-plan.md, Phase A step A9).
#
# Runs ON THE SERVER, as root (chowns to the voxhub user afterwards):
#
#   sudo ./seed-store.sh --stores-dir /mnt/storage/voxhub/data
#   sudo ./seed-store.sh --stores-dir /mnt/storage/voxhub/data --name golive-store-20260715
#
# The store carries the canonical go-live geometry (shape 10x12x14, float32,
# LPS origin [-5,-6,-7], 0.5 mm isotropic spacing) and deterministic voxel
# content (numpy default_rng seed 20260713), so an uncompressed pull of it
# yields a reproducible raw checksum that the evidence manifest can pin.
# The fixture generator (make-fixtures.sh) hardcodes the SAME geometry —
# change one and you must change the other.

set -euo pipefail

STORES_DIR=""
STORE_NAME="golive-store-$(date -u +%Y%m%d)"
VOXHUB_USER="voxhub"
PYBIN="${VOXHUB_PYBIN:-/opt/voxhub/.venv/bin/python}"

usage() {
    cat <<EOF
Usage: sudo $0 --stores-dir <path> [--name <store-name>]

Options:
  --stores-dir <path>  The server stores directory (required; same value
                       given to deploy.sh)
  --name <name>        Store name without .zarr suffix
                       (default: golive-store-<UTC date>)
Environment:
  VOXHUB_PYBIN         Python interpreter with zarr+numpy
                       (default: /opt/voxhub/.venv/bin/python)
EOF
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --stores-dir) STORES_DIR="$2"; shift 2 ;;
        --name)       STORE_NAME="$2"; shift 2 ;;
        -h|--help)    usage ;;
        *) echo "seed-store.sh: unknown option: $1" >&2; exit 2 ;;
    esac
done

[[ -z "$STORES_DIR" ]] && { echo "seed-store.sh: --stores-dir is required" >&2; exit 2; }
[[ -d "$STORES_DIR" ]] || { echo "seed-store.sh: stores dir not found: $STORES_DIR" >&2; exit 2; }
[[ -x "$PYBIN" ]] || { echo "seed-store.sh: python not found at $PYBIN (set VOXHUB_PYBIN)" >&2; exit 2; }

STORE_PATH="$STORES_DIR/$STORE_NAME.zarr"
if [[ -e "$STORE_PATH" ]]; then
    echo "seed-store.sh: refusing to overwrite existing $STORE_PATH" >&2
    exit 1
fi

"$PYBIN" - "$STORE_PATH" <<'PYEOF'
import sys
import numpy as np
import zarr

store_path = sys.argv[1]

# Canonical go-live geometry — MUST match scripts/golive/make-fixtures.sh.
SHAPE = (10, 12, 14)
ORIGIN_LPS = [-5.0, -6.0, -7.0]
SPACING_MM = [0.5, 0.5, 0.5]

root = zarr.open_group(store_path, mode='w')
raw = root.create_group('raw')
data = np.random.default_rng(20260713).standard_normal(SHAPE).astype('float32')
arr = raw.create_array('full', data=data)
arr.update_attributes(
    {
        'ImagePositionPatient': ORIGIN_LPS,
        'ImageOrientationPatient': [1.0, 0.0, 0.0, 0.0, 1.0, 0.0],
        'PixelSpacing': [SPACING_MM[0], SPACING_MM[1]],
        'computed_slice_spacing_mm': SPACING_MM[2],
        'spacing_mm': list(SPACING_MM),
    }
)
print(f'seeded {store_path}: shape={SHAPE} origin_lps={ORIGIN_LPS} spacing={SPACING_MM}')
PYEOF

chown -R "$VOXHUB_USER:$VOXHUB_USER" "$STORE_PATH"

# Make the new store visible in the catalog immediately (the TTL/fingerprint
# path would pick it up eventually; refreshing here keeps step timing exact).
sudo -u "$VOXHUB_USER" \
    env VOXHUB_SERVER_CONFIG="/home/$VOXHUB_USER/.config/voxhub/server.toml" \
    /usr/local/bin/voxhub-server catalog refresh --store "$STORE_NAME"

echo "OK: $STORE_PATH seeded and catalog refreshed (store name: $STORE_NAME)"

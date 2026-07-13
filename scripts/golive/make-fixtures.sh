#!/usr/bin/env bash
# make-fixtures.sh — Generate the marked annotation fixtures for the go-live
# test (docs/testing/go-live-test-plan.md, Phase B).
#
# Runs ON THE THIRD-PARTY CLIENT HOST inside the repo checkout:
#
#   ./scripts/golive/make-fixtures.sh <out-dir> <annotator-tag>
#   ./scripts/golive/make-fixtures.sh /tmp/golive-fixtures golive-alice
#
# Produces, under <out-dir>:
#
#   <tag>-clean.seg.nrrd   validates with ZERO issues against
#                          inner-ear-structures (labels 1-3, exact names)
#   <tag>-warn.seg.nrrd    exactly ONE warning: label 2 is named
#                          'vestibule-golive-warn' (ontology name mismatch)
#   <tag>-error.seg.nrrd   exactly ONE error: space origin shifted +10 mm
#                          in L (origin mismatch — push must always abort)
#   <tag>-huge.seg.nrrd    hand-written NRRD header declaring 4096^3 int16
#                          (128 GiB decompressed) over ~64 bytes of junk —
#                          trips the server's decompressed-size memory gate
#   <tag>.mrk.json         landmark fixture (three LPS points inside the
#                          volume bbox) for optional landmark rows
#   MANIFEST.sha256        sha256 of every fixture, for the evidence table
#
# Geometry is hardcoded to the canonical go-live store seeded by
# scripts/golive/seed-store.sh (shape 10x12x14, origin [-5,-6,-7], 0.5 mm).
# The label pattern is the fixture sentinel: single voxels at [0,0,0]=1,
# [1,1,1]=2, [2,2,2]=3.
#
# Requires a python with numpy + pynrrd — the voxhub-client venv has both
# (they ship with voxhub-schema).  Run `uv sync --package voxhub-client`
# first, or point VOXHUB_PYBIN at any suitable interpreter.

set -euo pipefail

OUT_DIR="${1:-}"
TAG="${2:-}"
if [[ -z "$OUT_DIR" || -z "$TAG" ]]; then
    echo "Usage: $0 <out-dir> <annotator-tag>   (e.g. $0 /tmp/golive-fixtures golive-alice)" >&2
    exit 2
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PYBIN="${VOXHUB_PYBIN:-$REPO_ROOT/.venv/bin/python}"
[[ -x "$PYBIN" ]] || { echo "make-fixtures.sh: python not found at $PYBIN (run 'uv sync --package voxhub-client' or set VOXHUB_PYBIN)" >&2; exit 2; }

mkdir -p "$OUT_DIR"

"$PYBIN" - "$OUT_DIR" "$TAG" <<'PYEOF'
import json
import sys
from pathlib import Path

import numpy as np
import nrrd

out_dir = Path(sys.argv[1])
tag = sys.argv[2]

# Canonical go-live geometry — MUST match scripts/golive/seed-store.sh.
SHAPE = (10, 12, 14)
ORIGIN_LPS = [-5.0, -6.0, -7.0]
SPACE_DIRECTIONS = [
    [0.0, 0.0, 0.5],
    [0.0, 0.5, 0.0],
    [0.5, 0.0, 0.0],
]

def sentinel_label_map() -> np.ndarray:
    lm = np.zeros(SHAPE, dtype=np.int16)
    lm[0, 0, 0] = 1
    lm[1, 1, 1] = 2
    lm[2, 2, 2] = 3
    return lm

def write_seg(path: Path, names: list[str], origin: list[float]) -> None:
    header: dict[str, object] = {
        'space': 'left-posterior-superior',
        'space origin': origin,
        'space directions': SPACE_DIRECTIONS,
        'kinds': ['domain', 'domain', 'domain'],
        # raw encoding keeps the payload trivially inspectable; note that
        # pynrrd stamps a generation-time comment into every header, so the
        # fixture sha256 digests are per-generation-run: generate ONCE per
        # go-live run and keep MANIFEST.sha256 with the evidence bundle
        # (do not regenerate mid-test).  The server parses raw and gzip
        # encodings identically.
        'encoding': 'raw',
    }
    for i, name in enumerate(names):
        header[f'Segment{i}_ID'] = f's{i}'
        header[f'Segment{i}_Name'] = name
        header[f'Segment{i}_LabelValue'] = str(i + 1)
        header[f'Segment{i}_Color'] = '1 0 0'
    nrrd.write(str(path), sentinel_label_map(), header)

CORRECT = ['cochlea', 'vestibule', 'semicircular_canals']

write_seg(out_dir / f'{tag}-clean.seg.nrrd', CORRECT, ORIGIN_LPS)
write_seg(
    out_dir / f'{tag}-warn.seg.nrrd',
    ['cochlea', 'vestibule-golive-warn', 'semicircular_canals'],
    ORIGIN_LPS,
)
write_seg(
    out_dir / f'{tag}-error.seg.nrrd',
    CORRECT,
    [ORIGIN_LPS[0] + 10.0, ORIGIN_LPS[1], ORIGIN_LPS[2]],
)

# Oversized-header fixture: valid NRRD header, absurd declared size.  Only
# the header is ever read by the server's memory gate (voxhub_core.slicer.
# estimate_seg_nrrd_ram_bytes): 4096^3 * int16 = 128 GiB estimated RAM.
huge = out_dir / f'{tag}-huge.seg.nrrd'
huge.write_bytes(
    b'NRRD0004\n'
    b'# golive oversized fixture\n'
    b'type: short\n'
    b'dimension: 3\n'
    b'sizes: 4096 4096 4096\n'
    b'encoding: gzip\n'
    b'space: left-posterior-superior\n'
    b'space origin: (0,0,0)\n'
    b'space directions: (0,0,0.5) (0,0.5,0) (0.5,0,0)\n'
    b'kinds: domain domain domain\n'
    b'endian: little\n'
    b'\n' + b'\x00' * 64
)

# Landmark fixture: three points inside the volume bbox, LPS.
points = [[-4.5, -5.5, -6.5], [-3.0, -4.0, -5.0], [-1.0, -2.0, -3.0]]
labels = ['golive-lmk-1', 'golive-lmk-2', 'golive-lmk-3']
markup = {
    'markups': [
        {
            'type': 'Fiducial',
            'coordinateSystem': 'LPS',
            'coordinateUnits': 'mm',
            'controlPoints': [
                {'id': str(i), 'label': lbl, 'position': pt}
                for i, (lbl, pt) in enumerate(zip(labels, points, strict=True))
            ],
        }
    ],
}
(out_dir / f'{tag}.mrk.json').write_text(json.dumps(markup, indent=2))

print(f'fixtures written to {out_dir} (tag: {tag})')
PYEOF

# sha256 manifest for the evidence table (bare digests, matching what the
# client sends as ChecksumEntry values).
(
    cd "$OUT_DIR"
    sha256sum "$TAG"-clean.seg.nrrd "$TAG"-warn.seg.nrrd "$TAG"-error.seg.nrrd \
        "$TAG"-huge.seg.nrrd "$TAG".mrk.json > MANIFEST.sha256
)
echo "OK: fixtures + MANIFEST.sha256 in $OUT_DIR"
cat "$OUT_DIR/MANIFEST.sha256"

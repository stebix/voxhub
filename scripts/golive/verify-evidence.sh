#!/usr/bin/env bash
# verify-evidence.sh — Server-side evidence audit for the go-live test
# (docs/testing/go-live-test-plan.md, Phase C).
#
# Runs ON THE SERVER as root:
#
#   sudo ./verify-evidence.sh \
#       --stores-dir /mnt/storage/voxhub/data \
#       --staging-dir /mnt/storage/voxhub/staging \
#       --store golive-store-20260715 \
#       --annotators "golive-alice golive-bob" \
#       --expect-provenance 3 --expect-forced 1 \
#       --backup-target /mnt/backup/voxhub
#
# Asserts, with PASS/FAIL per check (exit 1 on any FAIL):
#   1. provenance.jsonl parses; the go-live store has exactly the expected
#      number of push lines; every line is attributed to an allowed
#      go-live annotator with identity_source == "ssh_key"; exactly the
#      expected number of lines carry forced: true.
#   2. The store's zarr annotation instance groups match the provenance
#      lines one-to-one (no orphan groups, no unrecorded lines).
#   3. The structured log contains the expected happy-path events and the
#      expected adversarial refusal events (skippable via
#      --skip-adversarial for a run that did not execute Phase B.A rows).
#   4. The staging root holds no leftover vxhb-staging-* dirs and no
#      stray re-rooted rsync artifacts.
#   5. (with --backup-target) the newest snapshot contains
#      .meta/provenance.jsonl and the store's annotations tree, and
#      backup.log records backup_completed.
#
# NOTE on negative evidence: refusals emitted by the forced-command
# wrapper itself (code "forbidden") never reach the voxhub debug log —
# the wrapper writes only to the SSH client's stdout.  Their server-side
# trace is sshd's accepted-connection record (journalctl -u ssh) plus
# the ABSENCE of any corresponding rpc_request line.  This script can
# therefore only assert the absence side.

set -uo pipefail

STORES_DIR=""
STAGING_DIR=""
STORE=""
ANNOTATORS=""
EXPECT_PROV=3
EXPECT_FORCED=1
LOG_FILE="/var/log/voxhub/debug.log"
BACKUP_TARGET=""
BACKUP_LOG="/var/log/voxhub/backup.log"
SKIP_ADVERSARIAL=false
PYBIN="${VOXHUB_PYBIN:-/opt/voxhub/.venv/bin/python}"

usage() {
    sed -n '2,36p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --stores-dir)        STORES_DIR="$2"; shift 2 ;;
        --staging-dir)       STAGING_DIR="$2"; shift 2 ;;
        --store)             STORE="$2"; shift 2 ;;
        --annotators)        ANNOTATORS="$2"; shift 2 ;;
        --expect-provenance) EXPECT_PROV="$2"; shift 2 ;;
        --expect-forced)     EXPECT_FORCED="$2"; shift 2 ;;
        --log)               LOG_FILE="$2"; shift 2 ;;
        --backup-target)     BACKUP_TARGET="$2"; shift 2 ;;
        --backup-log)        BACKUP_LOG="$2"; shift 2 ;;
        --skip-adversarial)  SKIP_ADVERSARIAL=true; shift ;;
        -h|--help)           usage ;;
        *) echo "verify-evidence.sh: unknown option: $1" >&2; exit 2 ;;
    esac
done

[[ -z "$STORES_DIR" || -z "$STAGING_DIR" || -z "$STORE" || -z "$ANNOTATORS" ]] && {
    echo "verify-evidence.sh: --stores-dir, --staging-dir, --store, --annotators are required" >&2
    exit 2
}
[[ -x "$PYBIN" ]] || { echo "verify-evidence.sh: python not found at $PYBIN (set VOXHUB_PYBIN)" >&2; exit 2; }

FAILURES=0
pass() { printf 'PASS  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; FAILURES=$((FAILURES + 1)); }
warn() { printf 'WARN  %s\n' "$*"; }

# ---------------------------------------------------------------------------
# 1 + 2: provenance.jsonl <-> zarr annotation groups
# ---------------------------------------------------------------------------

echo "== provenance + zarr state =="

PROV="$STORES_DIR/.meta/provenance.jsonl"
if [[ ! -f "$PROV" ]]; then
    fail "provenance file missing: $PROV"
else
    PYOUT=$("$PYBIN" - "$PROV" "$STORES_DIR" "$STORE" "$EXPECT_PROV" "$EXPECT_FORCED" $ANNOTATORS <<'PYEOF'
import json
import sys
from pathlib import Path

prov_path, stores_dir, store = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
expect_prov, expect_forced = int(sys.argv[4]), int(sys.argv[5])
allowed = set(sys.argv[6:])

ok = True
def report(good: bool, msg: str) -> None:
    global ok
    print(('PASS  ' if good else 'FAIL  ') + msg)
    if not good:
        ok = False

lines = []
malformed = 0
with open(prov_path) as f:
    for raw in f:
        raw = raw.strip()
        if not raw:
            continue
        try:
            lines.append(json.loads(raw))
        except json.JSONDecodeError:
            malformed += 1
report(malformed == 0, f'provenance.jsonl: {malformed} malformed line(s)')

store_lines = [l for l in lines if l.get('store') == store]
report(
    len(store_lines) == expect_prov,
    f'provenance lines for store {store!r}: {len(store_lines)} (expected {expect_prov})',
)

bad_annot = sorted({l.get('annotator_id') for l in store_lines} - allowed)
report(not bad_annot, f'all store lines from allowed annotators (unexpected: {bad_annot})')

non_key = [l for l in store_lines if l.get('identity_source') != 'ssh_key']
report(not non_key, f"identity_source == 'ssh_key' on every store line ({len(non_key)} deviating)")

forced = [l for l in store_lines if l.get('forced') is True]
report(
    len(forced) == expect_forced,
    f'forced: true count: {len(forced)} (expected {expect_forced})',
)

# Whole-file sweep: the go-live run is inaugural, so ANY annotator outside
# the allowed set anywhere in the file is an unexplained artifact.
foreign = sorted({l.get('annotator_id') for l in lines} - allowed)
report(not foreign, f'no foreign annotators anywhere in provenance (found: {foreign})')

# Bijection provenance line <-> zarr instance group.
zarr_ann = stores_dir / f'{store}.zarr' / 'annotations'
groups = set()
if zarr_ann.is_dir():
    for slug_dir in zarr_ann.iterdir():
        if not slug_dir.is_dir():
            continue
        for inst in slug_dir.iterdir():
            if inst.is_dir():
                groups.add(f'annotations/{slug_dir.name}/{inst.name}/data')
recorded = {l.get('annotation_path') for l in store_lines}
report(
    groups == recorded,
    f'zarr groups match provenance lines '
    f'(orphan groups: {sorted(groups - recorded)}; '
    f'unbacked lines: {sorted(recorded - groups)})',
)

slug_prefix_ok = all(
    any(g.split('/')[1].startswith(a + '-') for a in allowed) for g in groups
)
report(slug_prefix_ok, 'every annotation slug carries an allowed go-live annotator prefix')

sys.exit(0 if ok else 1)
PYEOF
    )
    PYEXIT=$?
    printf '%s\n' "$PYOUT"
    PYFAILS=$(printf '%s\n' "$PYOUT" | grep -c '^FAIL' || true)
    if [[ "$PYEXIT" -ne 0 && "$PYFAILS" -eq 0 ]]; then
        # Interpreter crash rather than check failures — still a failure.
        PYFAILS=1
    fi
    FAILURES=$((FAILURES + PYFAILS))
fi

# ---------------------------------------------------------------------------
# 3: structured log events
# ---------------------------------------------------------------------------

echo "== structured log ($LOG_FILE) =="

count_event() {
    # Count across the live log and any rotated siblings.
    grep -h -c "\"event\": \"$1\"" "$LOG_FILE" "$LOG_FILE".* 2>/dev/null \
        | awk '{s+=$1} END {print s+0}'
}

check_min() {
    local event=$1 min=$2 n
    n=$(count_event "$event")
    if [[ "$n" -ge "$min" ]]; then
        pass "event $event: $n (>= $min)"
    else
        fail "event $event: $n (expected >= $min)"
    fi
}

if [[ ! -f "$LOG_FILE" ]]; then
    fail "log file missing: $LOG_FILE"
else
    # Happy-path events (Phase B rows B5-B16).
    check_min rpc_request 1
    check_min list_stores_completed 1
    check_min list_stores_unchanged 1
    check_min prepare_pull_completed 1
    check_min prepare_push_completed 1
    check_min integrate_completed 1
    check_min identity_resolved 1
    check_min staging_dir_reaped 1

    if ! $SKIP_ADVERSARIAL; then
        # Refusal events (Phase B rows A3-A10).
        check_min rpc_protocol_mismatch 1
        check_min identity_mismatch 1
        check_min checksum_mismatch 1
        check_min invalid_staging_content 1
        check_min invalid_staging_dir 1
        check_min integrate_refused_low_memory 1
    fi
fi

# ---------------------------------------------------------------------------
# 4: staging root state
# ---------------------------------------------------------------------------

echo "== staging root ($STAGING_DIR) =="

if [[ ! -d "$STAGING_DIR" ]]; then
    fail "staging dir missing: $STAGING_DIR"
else
    LEFTOVER=$(find "$STAGING_DIR" -mindepth 1 -maxdepth 1 -name 'vxhb-staging-*' | wc -l)
    if [[ "$LEFTOVER" -eq 0 ]]; then
        pass "no leftover vxhb-staging-* dirs"
    else
        fail "$LEFTOVER leftover vxhb-staging-* dir(s) — run 'voxhub-server gc --ttl-hours 0' and investigate"
    fi
    STRAY=$(find "$STAGING_DIR" -mindepth 1 -maxdepth 1 ! -name 'vxhb-staging-*' | wc -l)
    if [[ "$STRAY" -eq 0 ]]; then
        pass "no stray non-staging entries (re-rooted raw rsync artifacts)"
    else
        fail "$STRAY stray entr(ies) under staging root — expected after row A5; delete manually (gc never touches them)"
        find "$STAGING_DIR" -mindepth 1 -maxdepth 1 ! -name 'vxhb-staging-*' | sed 's/^/        /'
    fi
fi

# ---------------------------------------------------------------------------
# 5: backup snapshot
# ---------------------------------------------------------------------------

if [[ -n "$BACKUP_TARGET" ]]; then
    echo "== backup ($BACKUP_TARGET) =="
    if [[ ! -d "$BACKUP_TARGET/latest" && ! -L "$BACKUP_TARGET/latest" ]]; then
        fail "no 'latest' snapshot under $BACKUP_TARGET — run voxhub-backup.sh first"
    else
        if [[ -f "$BACKUP_TARGET/latest/.meta/provenance.jsonl" ]]; then
            pass "snapshot contains .meta/provenance.jsonl"
        else
            fail "snapshot missing .meta/provenance.jsonl"
        fi
        if [[ -d "$BACKUP_TARGET/latest/$STORE.zarr/annotations" ]]; then
            pass "snapshot contains $STORE.zarr/annotations"
        else
            fail "snapshot missing $STORE.zarr/annotations"
        fi
    fi
    if grep -q '"event": "backup_completed"' "$BACKUP_LOG" 2>/dev/null; then
        pass "backup_completed recorded in $BACKUP_LOG"
    else
        fail "no backup_completed line in $BACKUP_LOG"
    fi
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo
if [[ "$FAILURES" -eq 0 ]]; then
    echo "ALL CHECKS PASSED"
    exit 0
else
    echo "$FAILURES CHECK(S) FAILED"
    exit 1
fi

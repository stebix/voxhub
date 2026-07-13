"""Contract tests for the nightly backup script (launch plan 6.1).

Drives the real ``scripts/deploy/backup.sh`` via
``subprocess.run(['bash', script], ...)`` against a tmp stores dir, the
same pattern as ``test_forced_command.py``.  Asserts the three
properties the launch plan pins:

1. ``rsync -a --link-dest`` snapshots: a second run hardlinks unchanged
   files against the first snapshot (``st_nlink > 1``, same inode).
2. Rotation: snapshot dirs older than the retention window are removed;
   younger ones and the ``latest`` symlink survive.
3. One structlog-style JSON line per run on the log, success or failure.

Plan: docs/plans/launch-readiness-implementation-plan.md 6.1;
restore drill: docs/deployment-readiness.md.
"""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

_DEPLOY_DIR = Path(__file__).resolve().parents[3] / 'scripts' / 'deploy'
BACKUP_SCRIPT = _DEPLOY_DIR / 'backup.sh'

_BASH = shutil.which('bash')
_RSYNC = shutil.which('rsync')

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(_BASH is None, reason='bash not available'),
    pytest.mark.skipif(_RSYNC is None, reason='rsync not available'),
    pytest.mark.skipif(not BACKUP_SCRIPT.is_file(), reason='backup.sh not found'),
]


def _build_stores_dir(root: Path) -> Path:
    """A minimal stores layout: one store with an annotation + .meta."""
    stores = root / 'stores'
    ann = stores / 'alpha.zarr' / 'annotations' / 'alice-abc12345' / 'inst-1'
    ann.mkdir(parents=True)
    (ann / 'data').write_bytes(b'x' * 4096)
    meta = stores / '.meta'
    meta.mkdir()
    (meta / 'provenance.jsonl').write_text('{"event": "push"}\n')
    # Raw volume data must NOT be backed up (reproducible from DICOM).
    raw = stores / 'alpha.zarr' / 'raw'
    raw.mkdir()
    (raw / 'full').write_bytes(b'y' * 4096)
    return stores


def _run_backup(
    stores: Path,
    target: Path,
    log_file: Path,
    *extra: str,
) -> subprocess.CompletedProcess[str]:
    assert _BASH is not None
    return subprocess.run(
        [
            _BASH,
            str(BACKUP_SCRIPT),
            '--stores-dir',
            str(stores),
            '--target',
            str(target),
            '--log-file',
            str(log_file),
            *extra,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _snapshots(target: Path) -> list[Path]:
    return sorted(p for p in target.iterdir() if p.is_dir() and not p.is_symlink())


def _log_records(log_file: Path) -> list[dict[str, object]]:
    return [
        json.loads(line) for line in log_file.read_text().splitlines() if line.strip()
    ]


def test_bash_n_syntax(tmp_path):
    """The installed script must at minimum parse (bash -n)."""
    assert _BASH is not None
    proc = subprocess.run(
        [_BASH, '-n', str(BACKUP_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr


def test_two_runs_hardlink_dedup_and_rotation(tmp_path):
    """Run backup.sh twice: unchanged files are hardlinked across
    snapshots (same inode, nlink > 1); a stale snapshot older than the
    retention window is rotated out while fresh ones survive."""
    stores = _build_stores_dir(tmp_path)
    target = tmp_path / 'backups'
    log_file = tmp_path / 'backup.log'

    # Pre-seed a stale snapshot beyond the 14-day default retention.
    stale = target / '20200101T000000'
    stale.mkdir(parents=True)
    old = time.time() - 30 * 86400
    os.utime(stale, (old, old))

    proc1 = _run_backup(stores, target, log_file)
    assert proc1.returncode == 0, proc1.stderr
    # Snapshot names have 1s resolution; force distinct stamps.
    time.sleep(1.1)
    proc2 = _run_backup(stores, target, log_file)
    assert proc2.returncode == 0, proc2.stderr

    snaps = _snapshots(target)
    assert stale not in snaps, 'stale snapshot survived rotation'
    assert len(snaps) == 2

    rel = Path('alpha.zarr') / 'annotations' / 'alice-abc12345' / 'inst-1' / 'data'
    first = snaps[0] / rel
    second = snaps[1] / rel
    assert first.is_file()
    assert second.is_file()

    st1 = first.stat()
    st2 = second.stat()
    assert st1.st_ino == st2.st_ino, 'unchanged file was copied, not hardlinked'
    assert st1.st_nlink > 1

    # The provenance audit log is in every snapshot.
    assert (snaps[1] / '.meta' / 'provenance.jsonl').is_file()
    # Raw volume data is reproducible and must not be snapshotted.
    assert not (snaps[1] / 'alpha.zarr' / 'raw').exists()

    # ``latest`` points at the newest snapshot.
    latest = target / 'latest'
    assert latest.is_symlink()
    assert latest.resolve() == snaps[1].resolve()

    records = _log_records(log_file)
    completed = [r for r in records if r['event'] == 'backup_completed']
    assert len(completed) == 2
    assert all(r['level'] == 'info' for r in completed)
    # The run that rotated the stale snapshot reports it.
    assert completed[0]['rotated'] == '1'


def test_changed_file_is_not_hardlinked(tmp_path):
    """A file modified between runs must be a fresh copy in the second
    snapshot — the previous snapshot's content stays intact."""
    stores = _build_stores_dir(tmp_path)
    target = tmp_path / 'backups'
    log_file = tmp_path / 'backup.log'
    prov = stores / '.meta' / 'provenance.jsonl'

    assert _run_backup(stores, target, log_file).returncode == 0
    prov.write_text('{"event": "push"}\n{"event": "push"}\n')
    time.sleep(1.1)
    assert _run_backup(stores, target, log_file).returncode == 0

    snaps = _snapshots(target)
    old_prov = snaps[0] / '.meta' / 'provenance.jsonl'
    new_prov = snaps[1] / '.meta' / 'provenance.jsonl'
    assert old_prov.stat().st_ino != new_prov.stat().st_ino
    assert old_prov.read_text() == '{"event": "push"}\n'
    assert new_prov.read_text().count('push') == 2


def test_retention_flag_overrides_default(tmp_path):
    """--retention-days controls the rotation window."""
    stores = _build_stores_dir(tmp_path)
    target = tmp_path / 'backups'
    log_file = tmp_path / 'backup.log'

    recent = target / '20250101T000000'
    recent.mkdir(parents=True)
    two_days = time.time() - 2 * 86400
    os.utime(recent, (two_days, two_days))

    proc = _run_backup(stores, target, log_file, '--retention-days', '1')
    assert proc.returncode == 0, proc.stderr
    assert not recent.exists(), '2-day-old snapshot must fall to 1-day retention'


def test_failure_writes_error_json_line(tmp_path):
    """A missing stores dir exits non-zero and logs a structlog-style
    error line (cron observability contract)."""
    log_file = tmp_path / 'backup.log'
    proc = _run_backup(tmp_path / 'absent', tmp_path / 'backups', log_file)
    assert proc.returncode != 0

    records = _log_records(log_file)
    assert len(records) == 1
    assert records[0]['event'] == 'backup_failed'
    assert records[0]['level'] == 'error'
    assert 'reason' in records[0]


def test_empty_stores_dir_skips_cleanly(tmp_path):
    """A fresh deploy with nothing to protect exits 0 and says so —
    cron must stay quiet instead of paging on day one."""
    stores = tmp_path / 'stores'
    stores.mkdir()
    log_file = tmp_path / 'backup.log'

    proc = _run_backup(stores, tmp_path / 'backups', log_file)
    assert proc.returncode == 0, proc.stderr

    records = _log_records(log_file)
    assert len(records) == 1
    assert records[0]['event'] == 'backup_skipped_empty'

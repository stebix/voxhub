"""Contract tests for the server uninstall script.

Drives the real ``scripts/deploy/uninstall.sh`` against a fake filesystem root
(``VOXHUB_TEST_ROOT``) holding the exact layout ``deploy.sh`` produces, with
the privileged binaries it shells out to — ``id``, ``userdel``, ``crontab``,
``pgrep``, ``pkill``, ``sshd``, ``systemctl`` — replaced by recording stubs
passed in by absolute path (``VOXHUB_ID`` and friends).  Same harness shape as
``test_forced_command.py``: no root, no real system mutation, the actual shell
logic under test.

The invariants that matter, and why:

1. **Data survives by default.**  Annotations and ``.meta/provenance.jsonl``
   are the only things on a voxhub box that cannot be re-derived.  An
   uninstall exists to clear *stale code*, and must never be the thing that
   loses them.
2. **``--purge-data`` demands a verbatim confirmation** and leaves the stores
   untouched when it does not match.
3. **The apt-owned ``/usr/bin/rrsync`` is never removed** — only the copy
   ``deploy.sh`` itself installs into ``/usr/local/bin`` (deploy.sh Step 1b).
4. **The uid/gid are recorded before ``userdel``**, so the redeploy can
   reclaim them and preserved data does not end up orphaned.
5. **Re-running is a no-op**, including against a box that was never deployed.

Plan: docs/deployment-readiness.md "Uninstall → redeploy".
"""

import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

_DEPLOY_DIR = Path(__file__).resolve().parents[3] / 'scripts' / 'deploy'
UNINSTALL = _DEPLOY_DIR / 'uninstall.sh'

_BASH = shutil.which('bash')

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(_BASH is None, reason='bash not available'),
    pytest.mark.skipif(not UNINSTALL.is_file(), reason='uninstall script not found'),
]

UID = '997'
GID = '997'

# Each stub records its argv to calls.log, then emulates just enough of the
# real binary for the script's control flow.  User existence and the crontab
# are modelled as marker files so `userdel` / `crontab -r` have observable,
# order-sensitive effects (the script checks `id voxhub` again after userdel).
_STUB = """\
#!{python}
import os, sys
from pathlib import Path

stub = Path(__file__)
state = stub.parent
name = stub.name
argv = sys.argv[1:]
with (state / 'calls.log').open('a') as fh:
    fh.write(' '.join([name, *argv]) + '\\n')

user_marker = state / 'user-exists'
cron_marker = state / 'crontab-spool'
home = Path(os.environ['STUB_HOME'])

if name == 'id':
    if not user_marker.exists():
        sys.exit(1)
    if '-u' in argv:
        print({uid!r})
    elif '-g' in argv:
        print({gid!r})
    sys.exit(0)

if name == 'userdel':
    user_marker.unlink(missing_ok=True)
    if '-r' in argv:
        import shutil as _sh
        _sh.rmtree(home, ignore_errors=True)
    sys.exit(0)

if name == 'crontab':
    if '-l' in argv:
        if not cron_marker.exists():
            sys.exit(1)
        print(cron_marker.read_text(), end='')
        sys.exit(0)
    if '-r' in argv:
        cron_marker.unlink(missing_ok=True)
        sys.exit(0)
    sys.exit(0)

if name == 'pgrep':
    # A running voxhub-server only when the test asks for one.
    sys.exit(0 if os.environ.get('STUB_SERVER_RUNNING') else 1)

if name == 'pkill':
    # Nothing to signal — keeps the drain step from sleeping.
    sys.exit(1)

# sshd -t / systemctl reload: succeed quietly.
sys.exit(0)
"""

_SERVER_TOML = """\
[logging]
log_file = "{root}/var/log/voxhub/debug.log"
stderr_level = "WARNING"

[storage]
stores_dir = "{stores}"
staging_dir = "{staging}"
"""


class FakeRoot:
    """A tmpdir populated with the layout deploy.sh leaves behind."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.install_dir = root / 'opt/voxhub'
        self.uv_root = root / 'opt/voxhub-uv'
        self.bin_dir = root / 'usr/local/bin'
        self.server_bin = self.bin_dir / 'voxhub-server'
        self.forced_cmd = self.bin_dir / 'voxhub-forced-command.sh'
        self.backup_bin = self.bin_dir / 'voxhub-backup.sh'
        self.uv_bin = self.bin_dir / 'uv'
        self.local_rrsync = self.bin_dir / 'rrsync'
        self.apt_rrsync = root / 'usr/bin/rrsync'
        self.sshd_conf = root / 'etc/ssh/sshd_config.d/voxhub.conf'
        self.log_dir = root / 'var/log/voxhub'
        self.state_file = root / 'var/lib/voxhub/uninstall-state'
        self.archive_dir = root / 'var/backups'
        self.home = root / 'home/voxhub'
        self.auth_keys = self.home / '.ssh/authorized_keys'
        self.config = self.home / '.config/voxhub/server.toml'
        self.stores = root / 'srv/voxhub/data'
        self.staging = root / 'srv/voxhub/staging'
        self.backups = root / 'srv/voxhub/backups'
        self.annotation = self.stores / 'patient01.zarr/annotations/alice-abc/seg.json'
        self.provenance = self.stores / '.meta/provenance.jsonl'
        self.stale_staging = self.staging / 'vxhb-staging-deadbeef/chunk.0'
        self.snapshot = self.backups / '20260101T000000/patient01.zarr/keep.json'
        self.stub_dir = root / 'stub'

    def stub_calls(self) -> list[str]:
        log = self.stub_dir / 'calls.log'
        return log.read_text().splitlines() if log.exists() else []

    def user_exists(self) -> bool:
        return (self.stub_dir / 'user-exists').exists()

    def crontab_exists(self) -> bool:
        return (self.stub_dir / 'crontab-spool').exists()

    def archives(self) -> list[Path]:
        if not self.archive_dir.is_dir():
            return []
        return sorted(self.archive_dir.glob('voxhub-uninstall-*.tar.gz'))


def _write(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(mode)


@pytest.fixture
def fake(tmp_path: Path) -> FakeRoot:
    """Materialise a fully deployed voxhub server under a tmp root."""
    f = FakeRoot(tmp_path / 'root')

    # Stubs for every privileged binary the script shells out to.
    f.stub_dir.mkdir(parents=True)
    for name in ('id', 'userdel', 'crontab', 'pgrep', 'pkill', 'sshd', 'systemctl'):
        _write(
            f.stub_dir / name,
            _STUB.format(python=sys.executable, uid=UID, gid=GID),
            mode=0o755,
        )
    # Marker files, deliberately *not* named after any stub in this dir.
    (f.stub_dir / 'user-exists').touch()
    _write(
        f.stub_dir / 'crontab-spool',
        '0 4 * * * /usr/local/bin/voxhub-server gc --ttl-hours 48\n',
    )

    # Code, venv, toolchain, binaries.
    _write(f.install_dir / '.venv/bin/voxhub-server', '#!/bin/sh\n', mode=0o755)
    _write(f.install_dir / '.git/HEAD', 'ref: refs/heads/main\n')
    _write(f.uv_root / 'python/cpython-3.12/bin/python3', '', mode=0o755)
    _write(f.uv_root / 'cache/wheels/blob', '')
    f.bin_dir.mkdir(parents=True, exist_ok=True)
    f.server_bin.symlink_to(f.install_dir / '.venv/bin/voxhub-server')
    _write(f.forced_cmd, '#!/usr/bin/env bash\n', mode=0o755)
    _write(f.backup_bin, '#!/usr/bin/env bash\n', mode=0o755)
    _write(f.uv_bin, '#!/bin/sh\n', mode=0o755)
    _write(f.local_rrsync, '#!/usr/bin/perl\n', mode=0o755)
    # The apt-owned copy: deploy.sh never installs this, and uninstall.sh must
    # never remove it.
    _write(f.apt_rrsync, '#!/usr/bin/perl\n', mode=0o755)

    _write(f.sshd_conf, 'Match User voxhub\n    ForceCommand /usr/local/bin/x\n')
    _write(f.log_dir / 'debug.log', 'log line\n')

    # Home: config + annotator keys.
    _write(f.auth_keys, 'command="..." ssh-ed25519 AAAA alice\n', mode=0o600)
    _write(
        f.config,
        _SERVER_TOML.format(root=f.root, stores=f.stores, staging=f.staging),
    )

    # Data: stores, staging scratch, backup snapshots.
    _write(f.annotation, '{"label": 1}\n')
    _write(f.provenance, '{"event": "push"}\n')
    _write(f.stale_staging, 'stale bytes\n')
    _write(f.snapshot, '{"label": 1}\n')

    return f


def _run(
    fake: FakeRoot,
    *args: str,
    env_extra: dict[str, str] | None = None,
    stdin: str = '',
) -> subprocess.CompletedProcess[str]:
    env = {
        'PATH': '/usr/bin:/bin',
        'VOXHUB_TEST_ROOT': str(fake.root),
        'STUB_HOME': str(fake.home),
        'VOXHUB_ID': str(fake.stub_dir / 'id'),
        'VOXHUB_USERDEL': str(fake.stub_dir / 'userdel'),
        'VOXHUB_CRONTAB': str(fake.stub_dir / 'crontab'),
        'VOXHUB_PGREP': str(fake.stub_dir / 'pgrep'),
        'VOXHUB_PKILL': str(fake.stub_dir / 'pkill'),
        'VOXHUB_SSHD': str(fake.stub_dir / 'sshd'),
        'VOXHUB_SYSTEMCTL': str(fake.stub_dir / 'systemctl'),
    }
    if env_extra:
        env.update(env_extra)
    assert _BASH is not None
    return subprocess.run(
        [_BASH, str(UNINSTALL), *args],
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
    )


# -- dry run -----------------------------------------------------------------


def test_dry_run_changes_nothing(fake):
    """--dry-run prints the plan and leaves every artifact in place."""
    proc = _run(fake, '--dry-run')
    assert proc.returncode == 0, proc.stderr

    for path in (
        fake.install_dir,
        fake.uv_root,
        fake.server_bin,
        fake.forced_cmd,
        fake.backup_bin,
        fake.uv_bin,
        fake.local_rrsync,
        fake.sshd_conf,
        fake.log_dir,
        fake.auth_keys,
        fake.stale_staging,
        fake.annotation,
    ):
        assert path.exists(), f'dry-run removed {path}'

    assert fake.user_exists()
    assert fake.crontab_exists()
    assert not fake.state_file.exists()
    assert fake.archives() == []


# -- full teardown -----------------------------------------------------------


def test_full_teardown_removes_code_toolchain_and_user(fake):
    proc = _run(fake)
    assert proc.returncode == 0, proc.stderr

    for path in (
        fake.install_dir,
        fake.uv_root,
        fake.server_bin,
        fake.forced_cmd,
        fake.backup_bin,
        fake.uv_bin,
        fake.local_rrsync,
        fake.sshd_conf,
        fake.log_dir,
        fake.home,
    ):
        assert not path.exists(), f'{path} survived the uninstall'

    assert not fake.user_exists()
    assert not fake.crontab_exists()


def test_sshd_config_validated_and_reloaded(fake):
    """The drop-in is removed, `sshd -t` validates, and sshd is reloaded — in
    that order, before anything else is torn down."""
    proc = _run(fake)
    assert proc.returncode == 0, proc.stderr

    calls = fake.stub_calls()
    assert 'sshd -t' in calls
    assert 'systemctl reload sshd' in calls
    # SSH access is cut before the user's processes are drained.
    assert calls.index('sshd -t') < calls.index('userdel -r voxhub')


def test_crontab_removed_explicitly(fake):
    """gc + backup crons stop before the binaries they invoke disappear."""
    proc = _run(fake)
    assert proc.returncode == 0, proc.stderr
    assert 'crontab -u voxhub -r' in fake.stub_calls()
    assert not fake.crontab_exists()


# -- data preservation (the point of the whole exercise) ---------------------


def test_annotation_data_preserved_by_default(fake):
    """Stores, provenance and backup snapshots survive an uninstall."""
    proc = _run(fake)
    assert proc.returncode == 0, proc.stderr

    assert fake.annotation.read_text() == '{"label": 1}\n'
    assert fake.provenance.read_text() == '{"event": "push"}\n'
    assert fake.snapshot.exists()
    assert fake.stores.is_dir()
    assert fake.backups.is_dir()


def test_staging_contents_purged_but_directory_kept(fake):
    """Staging is scratch: its contents go, the dir (possibly a mount) stays."""
    proc = _run(fake)
    assert proc.returncode == 0, proc.stderr

    assert fake.staging.is_dir()
    assert not fake.stale_staging.exists()
    assert list(fake.staging.iterdir()) == []


def test_apt_owned_rrsync_is_never_removed(fake):
    """Only deploy.sh's /usr/local/bin copy goes; /usr/bin/rrsync belongs to
    the rsync package and removing it would vandalise apt's file list."""
    proc = _run(fake)
    assert proc.returncode == 0, proc.stderr

    assert not fake.local_rrsync.exists()
    assert fake.apt_rrsync.exists()


# -- the uid hand-off --------------------------------------------------------


def test_uid_gid_recorded_before_userdel(fake):
    """deploy.sh reclaims these numbers so preserved data keeps an owner."""
    proc = _run(fake)
    assert proc.returncode == 0, proc.stderr

    assert fake.state_file.exists()
    state = fake.state_file.read_text()
    assert f'uid={UID}' in state
    assert f'gid={GID}' in state

    # Recorded while the user still existed — id(1) is queried before userdel.
    calls = fake.stub_calls()
    assert calls.index('id -u voxhub') < calls.index('userdel -r voxhub')


def test_keep_user_skips_uid_state(fake):
    """--keep-user: no userdel, so no hand-off needed and keys stay put."""
    proc = _run(fake, '--keep-user')
    assert proc.returncode == 0, proc.stderr

    assert fake.user_exists()
    assert fake.auth_keys.exists()
    assert not fake.state_file.exists()
    assert 'userdel -r voxhub' not in fake.stub_calls()
    # Code still goes.
    assert not fake.install_dir.exists()


# -- archive -----------------------------------------------------------------


def test_authorized_keys_archived_before_home_is_deleted(fake):
    """userdel -r takes the home; the keys must already be safe by then."""
    proc = _run(fake)
    assert proc.returncode == 0, proc.stderr

    archives = fake.archives()
    assert len(archives) == 1
    with tarfile.open(archives[0]) as tar:
        names = tar.getnames()
        assert '.ssh/authorized_keys' in names
        assert '.config/voxhub/server.toml' in names
        member = tar.extractfile('.ssh/authorized_keys')
        assert member is not None
        assert b'alice' in member.read()

    assert not fake.home.exists()


def test_keep_uv_preserves_toolchain(fake):
    proc = _run(fake, '--keep-uv')
    assert proc.returncode == 0, proc.stderr

    assert fake.uv_bin.exists()
    assert fake.uv_root.is_dir()
    assert not fake.install_dir.exists()


# -- drain -------------------------------------------------------------------


def test_running_server_blocks_uninstall(fake):
    """An in-flight integrate must not be killed by accident: refuse, and
    change nothing that matters."""
    proc = _run(fake, env_extra={'STUB_SERVER_RUNNING': '1'})
    assert proc.returncode != 0
    assert 'voxhub-server is running' in proc.stderr

    assert fake.user_exists()
    assert fake.install_dir.exists()
    assert fake.annotation.exists()


def test_force_overrides_running_server(fake):
    proc = _run(fake, '--force', env_extra={'STUB_SERVER_RUNNING': '1'})
    assert proc.returncode == 0, proc.stderr

    assert not fake.install_dir.exists()
    assert not fake.user_exists()


# -- purge-data --------------------------------------------------------------


def test_purge_data_refuses_on_confirmation_mismatch(fake):
    proc = _run(
        fake,
        '--purge-data',
        env_extra={'VOXHUB_PURGE_CONFIRM': '/wrong/path'},
    )
    assert proc.returncode != 0
    assert 'Confirmation did not match' in proc.stderr

    assert fake.annotation.exists()
    assert fake.snapshot.exists()


def test_purge_data_refuses_without_confirmation_on_non_tty(fake):
    """A pipe is not consent — a CI runner cannot purge by omission."""
    proc = _run(fake, '--purge-data')
    assert proc.returncode != 0
    assert 'VOXHUB_PURGE_CONFIRM' in proc.stderr
    assert fake.annotation.exists()


def test_purge_data_with_matching_confirmation_destroys_data(fake):
    proc = _run(
        fake,
        '--purge-data',
        env_extra={'VOXHUB_PURGE_CONFIRM': str(fake.stores)},
    )
    assert proc.returncode == 0, proc.stderr

    assert not fake.stores.exists()
    assert not fake.backups.exists()
    # The uid hand-off is meaningless once the data it protected is gone.
    assert not fake.state_file.exists()


# -- idempotency -------------------------------------------------------------


def test_rerun_is_a_noop(fake):
    first = _run(fake)
    assert first.returncode == 0, first.stderr

    second = _run(fake)
    assert second.returncode == 0, second.stderr
    assert 'SKIP' in second.stdout
    # Data still there after two passes.
    assert fake.annotation.exists()


def test_never_deployed_box_is_not_an_error(tmp_path):
    """Nothing installed, no voxhub user, no config: every step skips."""
    bare = FakeRoot(tmp_path / 'root')
    bare.stub_dir.mkdir(parents=True)
    for name in ('id', 'userdel', 'crontab', 'pgrep', 'pkill', 'sshd', 'systemctl'):
        _write(
            bare.stub_dir / name,
            _STUB.format(python=sys.executable, uid=UID, gid=GID),
            mode=0o755,
        )
    # No user-exists marker, no crontab marker, no files at all.
    proc = _run(bare)
    assert proc.returncode == 0, proc.stderr
    assert 'No residual voxhub artifacts' in proc.stdout

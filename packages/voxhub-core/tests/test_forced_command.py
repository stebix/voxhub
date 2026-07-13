"""Contract tests for the SSH forced-command wrapper.

Drives the real ``scripts/deploy/voxhub-forced-command.sh`` via
``subprocess.run(['bash', wrapper], env=...)`` with stub ``voxhub-server``
and ``rrsync`` executables on ``PATH`` (a tmp bin dir is prepended), so the
tests never depend on the real rrsync binary being installed.  Real-binary
coverage (rrsync actually confining paths to the staging root) lands in the
launch 1.4 loopback-sshd e2e suite.

Pinned two-branch contract
(docs/plans/c-transport-rpc-implementation-plan.md, "Forced-command wrapper
contract"):

1. ``SSH_ORIGINAL_COMMAND`` starts with ``rsync `` → exec rrsync read-only,
   rooted at the staging root.
2. ``SSH_ORIGINAL_COMMAND`` equals ``voxhub-server rpc`` or bare ``rpc`` →
   exec ``voxhub-server rpc`` with stdin/stdout passing through untouched.
3. Anything else — including the legacy bare-subcommand forms — → forbidden
   ``ServerError``-shaped JSON envelope on STDOUT, exit 1, and neither stub
   is ever executed.

The wrapper never tokenizes arguments (there are none), and the key-bound
``VOXHUB_ANNOTATOR`` / ``VOXHUB_SERVER_CONFIG`` environment must survive
into the exec'd server process (architecture plan §B).

Plan: docs/plans/c-transport-rpc-implementation-plan.md Task A3;
architecture-improvement-plan.md §C.3; launch-readiness plan 1.3.
"""

import base64
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from voxhub_schema import PROTOCOL_VERSION

_DEPLOY_DIR = Path(__file__).resolve().parents[3] / 'scripts' / 'deploy'
WRAPPER = _DEPLOY_DIR / 'voxhub-forced-command.sh'

_BASH = shutil.which('bash')

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(_BASH is None, reason='bash not available'),
    pytest.mark.skipif(not WRAPPER.is_file(), reason='wrapper script not found'),
]

# A representative rsync server-side command as the client's RsyncTransfer
# produces it on the remote end.
_RSYNC_CMD = 'rsync --server --sender -vlogDtpre.iLsfxCIvu . /staging/vxhb-staging-abc/'

_STUB_TEMPLATE = """\
#!{python}
import base64, json, os, sys
from pathlib import Path

Path({sentinel!r}).touch()
print(
    json.dumps(
        {{
            'stub': {name!r},
            'argv': sys.argv[1:],
            'stdin_b64': base64.b64encode(sys.stdin.buffer.read()).decode(),
            'VOXHUB_ANNOTATOR': os.environ.get('VOXHUB_ANNOTATOR'),
            'VOXHUB_SERVER_CONFIG': os.environ.get('VOXHUB_SERVER_CONFIG'),
            'SSH_ORIGINAL_COMMAND': os.environ.get('SSH_ORIGINAL_COMMAND'),
        }}
    )
)
"""


def _make_bin_dir(tmp_path: Path) -> Path:
    """Create a bin dir holding stub ``voxhub-server`` and ``rrsync``.

    Each stub touches a ``<name>.ran`` sentinel next to itself, then prints
    argv, base64'd stdin, and selected environment variables as JSON.  The
    shebang is pinned to the running interpreter so the stubs do not depend
    on ``python3`` resolving on the subprocess ``PATH``.
    """
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for name in ('voxhub-server', 'rrsync'):
        stub = bin_dir / name
        sentinel = bin_dir / f'{name}.ran'
        stub.write_text(
            _STUB_TEMPLATE.format(
                python=sys.executable, sentinel=str(sentinel), name=name
            )
        )
        stub.chmod(0o755)
    return bin_dir


def _stub_ran(bin_dir: Path, name: str) -> bool:
    return (bin_dir / f'{name}.ran').exists()


def _run_wrapper(
    bin_dir: Path,
    *,
    ssh_original_command: str | None,
    env_extra: dict[str, str] | None = None,
    stdin: bytes = b'',
) -> subprocess.CompletedProcess[bytes]:
    env: dict[str, str] = {
        # Stub dir first so `command -v voxhub-server` / `command -v rrsync`
        # inside the wrapper resolve to the stubs, never to real binaries.
        'PATH': f'{bin_dir}:/usr/bin:/bin',
    }
    if ssh_original_command is not None:
        env['SSH_ORIGINAL_COMMAND'] = ssh_original_command
    if env_extra:
        env.update(env_extra)
    assert _BASH is not None
    return subprocess.run(
        [_BASH, str(WRAPPER)],
        env=env,
        input=stdin,
        capture_output=True,
    )


def _assert_forbidden(proc: subprocess.CompletedProcess[bytes], bin_dir: Path) -> None:
    """The wrapper refused: envelope on stdout, exit 1, no stub executed."""
    assert proc.returncode == 1
    payload = json.loads(proc.stdout)
    assert payload['error'] is True
    assert payload['code'] == 'forbidden'
    # The wrapper hardcodes the version in its deny() envelope; this pins
    # the literal to voxhub_schema.PROTOCOL_VERSION so a bump cannot ship
    # without updating the wrapper.
    assert payload['protocol_version'] == PROTOCOL_VERSION
    assert not _stub_ran(bin_dir, 'voxhub-server')
    assert not _stub_ran(bin_dir, 'rrsync')


# -- RPC branch --------------------------------------------------------------


@pytest.mark.parametrize('command', ['rpc', 'voxhub-server rpc'])
def test_rpc_forms_exec_server_with_argv_rpc(tmp_path, command):
    """Both accepted rpc spellings exec the server with argv ['rpc']."""
    bin_dir = _make_bin_dir(tmp_path)
    proc = _run_wrapper(bin_dir, ssh_original_command=command)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload['stub'] == 'voxhub-server'
    assert payload['argv'] == ['rpc']
    assert not _stub_ran(bin_dir, 'rrsync')


def test_rpc_stdin_passes_through_byte_identically(tmp_path):
    """The request bytes reach the server unmodified — including non-UTF-8
    bytes and a missing trailing newline."""
    bin_dir = _make_bin_dir(tmp_path)
    request = b'{"protocol_version": 2, "method": "list-stores", "params": {}}'
    request += b'\n\x00\xff\x01 trailing garbage without newline'
    proc = _run_wrapper(bin_dir, ssh_original_command='rpc', stdin=request)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert base64.b64decode(payload['stdin_b64']) == request


def test_env_vars_survive_into_server(tmp_path):
    """VOXHUB_ANNOTATOR and VOXHUB_SERVER_CONFIG (set by sshd / the key's
    environment= option) reach the exec'd server unchanged."""
    bin_dir = _make_bin_dir(tmp_path)
    config = str(tmp_path / 'server.toml')
    proc = _run_wrapper(
        bin_dir,
        ssh_original_command='voxhub-server rpc',
        env_extra={'VOXHUB_ANNOTATOR': 'alice', 'VOXHUB_SERVER_CONFIG': config},
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload['VOXHUB_ANNOTATOR'] == 'alice'
    assert payload['VOXHUB_SERVER_CONFIG'] == config


def test_annotator_env_absent_is_not_fabricated(tmp_path):
    """With no key binding the wrapper must not invent an identity."""
    bin_dir = _make_bin_dir(tmp_path)
    proc = _run_wrapper(bin_dir, ssh_original_command='rpc')
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload['VOXHUB_ANNOTATOR'] is None


# -- rsync branch ------------------------------------------------------------


def test_rsync_execs_rrsync_writable_rooted_at_staging(tmp_path):
    """An rsync server command re-execs through rrsync <staging root>.

    Read-WRITE since launch 4.3 (push uploads into server-issued staging
    dirs) — no ``-ro`` flag.  The symlink threat that kept it read-only
    is handled by the client's ``--no-links`` and the server's
    ``invalid_staging_content`` refusal in integrate-annotations.

    The repo copy of the wrapper is unrendered (deploy.sh injects the
    staging root at install time), so this also covers the
    VOXHUB_STAGING_ROOT env fallback used by tests and the 1.4 e2e suite.
    """
    bin_dir = _make_bin_dir(tmp_path)
    staging = tmp_path / 'staging'
    staging.mkdir()
    proc = _run_wrapper(
        bin_dir,
        ssh_original_command=_RSYNC_CMD,
        env_extra={'VOXHUB_STAGING_ROOT': str(staging)},
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload['stub'] == 'rrsync'
    assert payload['argv'] == [str(staging)]
    # rrsync re-parses SSH_ORIGINAL_COMMAND itself; exec must preserve it.
    assert payload['SSH_ORIGINAL_COMMAND'] == _RSYNC_CMD
    assert not _stub_ran(bin_dir, 'voxhub-server')


def test_rsync_without_staging_root_is_rejected(tmp_path):
    """Unrendered wrapper + no VOXHUB_STAGING_ROOT: refuse loudly with a
    structured envelope instead of handing rrsync a garbage root."""
    bin_dir = _make_bin_dir(tmp_path)
    proc = _run_wrapper(bin_dir, ssh_original_command=_RSYNC_CMD)
    assert proc.returncode == 1
    payload = json.loads(proc.stdout)
    assert payload['error'] is True
    assert payload['code'] == 'server_misconfigured'
    assert not _stub_ran(bin_dir, 'rrsync')
    assert not _stub_ran(bin_dir, 'voxhub-server')


# -- forbidden branch --------------------------------------------------------


@pytest.mark.parametrize(
    'command',
    [
        pytest.param('voxhub-server gc', id='operator-subcommand'),
        pytest.param('voxhub-server gc --ttl-hours 0', id='operator-with-args'),
        pytest.param('prepare-pull --store x', id='legacy-bare-subcommand'),
        pytest.param('list-stores', id='legacy-bare-allowlisted'),
        pytest.param('voxhub-server rpc --extra', id='rpc-with-extra-args'),
        pytest.param('rpc; echo pwned', id='rpc-shell-metachars'),
        pytest.param('; echo pwned', id='shell-metachars'),
        pytest.param('$(id)', id='command-substitution'),
        pytest.param('rsync', id='bare-rsync-no-args'),
        pytest.param('rsyncfoo --server', id='rsync-prefix-not-word'),
        pytest.param('/bin/sh', id='shell'),
        pytest.param('', id='empty'),
        pytest.param(None, id='unset'),
    ],
)
def test_everything_else_is_forbidden(tmp_path, command):
    """Non-contract commands yield the forbidden envelope on stdout, exit 1,
    and never reach either stub — no tokenization, no evaluation."""
    bin_dir = _make_bin_dir(tmp_path)
    staging = tmp_path / 'staging'
    staging.mkdir()
    proc = _run_wrapper(
        bin_dir,
        ssh_original_command=command,
        env_extra={'VOXHUB_STAGING_ROOT': str(staging)},
    )
    _assert_forbidden(proc, bin_dir)
    assert b'pwned' not in proc.stdout
    assert b'uid=' not in proc.stdout

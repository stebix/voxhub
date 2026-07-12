"""Subprocess tests for the SSH forced-command wrapper.

Drives the real ``scripts/deploy/voxhub-forced-command.sh`` via
``subprocess.run(['bash', wrapper], env=...)`` with a stub ``voxhub-server``
that echoes its argv and selected environment variables as JSON.  The focus
here is the architecture-plan §B guarantee: the key-bound ``VOXHUB_ANNOTATOR``
(injected by sshd from the connecting key's ``environment=`` option) survives
untouched into the exec'd server process.

The wrapper's request-format parsing bugs are a separate launch-blocker
(launch 1.1 / architecture C.3, on hold); these tests therefore drive
``SSH_ORIGINAL_COMMAND`` in the *bare subcommand* form the current wrapper
accepts (e.g. ``list-stores``) so they exercise the exec path, not the parser.

Plan: docs/plans/architecture-improvement-plan.md §B
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_DEPLOY_DIR = Path(__file__).resolve().parents[3] / 'scripts' / 'deploy'
WRAPPER = _DEPLOY_DIR / 'voxhub-forced-command.sh'

_BASH = shutil.which('bash')

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(_BASH is None, reason='bash not available'),
    pytest.mark.skipif(not WRAPPER.is_file(), reason='wrapper script not found'),
]


def _make_stub_server(tmp_path: Path) -> Path:
    """Write an executable stub that prints argv + selected env as JSON.

    The shebang is pinned to the running interpreter so the stub does not
    depend on ``python3`` resolving on the subprocess ``PATH``.
    """
    stub = tmp_path / 'voxhub-server-stub'
    stub.write_text(
        f'#!{sys.executable}\n'
        'import json, os, sys\n'
        'print(json.dumps({\n'
        "    'argv': sys.argv[1:],\n"
        "    'VOXHUB_ANNOTATOR': os.environ.get('VOXHUB_ANNOTATOR'),\n"
        '}))\n'
    )
    stub.chmod(0o755)
    return stub


def _run_wrapper(
    tmp_path: Path,
    *,
    ssh_original_command: str | None,
    annotator: str | None,
) -> subprocess.CompletedProcess[str]:
    stub = _make_stub_server(tmp_path)
    env: dict[str, str] = {
        'PATH': f'{tmp_path}',  # deliberately minimal; bash invoked by abs path
        'VOXHUB_SERVER': str(stub),
        'VOXHUB_SERVER_CONFIG': str(tmp_path / 'server.toml'),
    }
    if ssh_original_command is not None:
        env['SSH_ORIGINAL_COMMAND'] = ssh_original_command
    if annotator is not None:
        env['VOXHUB_ANNOTATOR'] = annotator
    assert _BASH is not None
    return subprocess.run(
        [_BASH, str(WRAPPER)],
        env=env,
        capture_output=True,
        text=True,
    )


def test_annotator_env_survives_into_server_process(tmp_path):
    """VOXHUB_ANNOTATOR set by sshd reaches the exec'd server unchanged."""
    proc = _run_wrapper(tmp_path, ssh_original_command='list-stores', annotator='alice')
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload['argv'] == ['list-stores']
    assert payload['VOXHUB_ANNOTATOR'] == 'alice'


def test_annotator_env_absent_is_not_fabricated(tmp_path):
    """With no key binding the wrapper must not invent an identity."""
    proc = _run_wrapper(tmp_path, ssh_original_command='list-stores', annotator=None)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload['argv'] == ['list-stores']
    assert payload['VOXHUB_ANNOTATOR'] is None


def test_forbidden_command_never_reaches_server(tmp_path):
    """An operator-only subcommand is rejected before exec — the stub, which
    would print JSON on stdout, is never run."""
    proc = _run_wrapper(
        tmp_path, ssh_original_command='gc --ttl-hours 0', annotator='alice'
    )
    assert proc.returncode == 1
    assert proc.stdout.strip() == ''
    assert 'forbidden' in proc.stderr

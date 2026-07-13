"""Tests for SSH transport: target parsing, RPC command building, responses."""

import json
import subprocess
import sys
from unittest.mock import patch

import pytest

from voxhub_client.ssh import (
    DEFAULT_TIMEOUT_S,
    METHOD_TIMEOUTS_S,
    RPC_REMOTE_COMMAND,
    ProtocolMismatchError,
    RemoteError,
    SshRunner,
    SshTarget,
)
from voxhub_schema import PROTOCOL_VERSION

# ===================================================================
# SSH TARGET PARSING
# ===================================================================


class TestSshTargetParse:
    def test_user_and_host(self):
        t = SshTarget.parse('alice@server.example.com')
        assert t.user == 'alice'
        assert t.host == 'server.example.com'

    def test_no_user_falls_back_to_os_user(self):
        with patch('voxhub_client.ssh.getpass.getuser', return_value='bob'):
            t = SshTarget.parse('server')
        assert t.user == 'bob'
        assert t.host == 'server'

    def test_legacy_path_suffix_rejected(self):
        """A trailing ``:/path`` is a relic of the old protocol; reject it
        loudly so stale clients can't limp along with wrong expectations."""
        with pytest.raises(ValueError, match='Invalid SSH target'):
            SshTarget.parse('alice@server.example.com:/data/zarr')

    def test_bare_colon_suffix_rejected(self):
        with pytest.raises(ValueError, match='Invalid SSH target'):
            SshTarget.parse('alice@host:')

    def test_empty_host_rejected(self):
        with pytest.raises(ValueError, match='host is empty'):
            SshTarget.parse('alice@')

    def test_ssh_destination_property(self):
        t = SshTarget(user='alice', host='server')
        assert t.ssh_destination == 'alice@server'


# ===================================================================
# SSH RUNNER COMMAND BUILDING
# ===================================================================


class TestSshRunnerCommandBuilding:
    def test_default_port_no_p_flag(self):
        target = SshTarget(user='a', host='h')
        runner = SshRunner(target=target)
        args = runner._ssh_args()
        assert '-p' not in args
        assert 'a@h' in args
        assert args[-1] == '--'

    def test_custom_port_includes_p_flag(self):
        target = SshTarget(user='a', host='h', port=2222)
        runner = SshRunner(target=target)
        args = runner._ssh_args()
        idx = args.index('-p')
        assert args[idx + 1] == '2222'

    def test_batch_mode_enabled(self):
        target = SshTarget(user='a', host='h')
        runner = SshRunner(target=target)
        args = runner._ssh_args()
        assert '-o' in args
        idx = args.index('-o')
        assert args[idx + 1] == 'BatchMode=yes'

    def test_extra_ssh_options_spliced_before_destination(self):
        """The e2e sshd harness needs identity/known-hosts overrides."""
        target = SshTarget(user='a', host='h')
        runner = SshRunner(
            target=target,
            ssh_options=(
                '-i',
                '/tmp/test-key',
                '-o',
                'StrictHostKeyChecking=no',
                '-o',
                'UserKnownHostsFile=/dev/null',
            ),
        )
        args = runner._ssh_args()
        assert '-i' in args
        assert args[args.index('-i') + 1] == '/tmp/test-key'
        assert 'StrictHostKeyChecking=no' in args
        assert 'UserKnownHostsFile=/dev/null' in args
        # Options come before the destination and the ``--`` sentinel.
        assert args.index('-i') < args.index('a@h') < args.index('--')

    def test_no_extra_options_by_default(self):
        target = SshTarget(user='a', host='h')
        runner = SshRunner(target=target)
        assert runner.ssh_options == ()


# ===================================================================
# SSH RUNNER RUN (RPC CONTRACT)
# ===================================================================


def _mock_run_result(stdout='', stderr='', returncode=0):
    """Create a mock subprocess.CompletedProcess."""
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _ok_response(**extra):
    payload = {'protocol_version': PROTOCOL_VERSION}
    payload.update(extra)
    return _mock_run_result(stdout=json.dumps(payload))


class _CapturingRun:
    """subprocess.run replacement that records argv and kwargs."""

    def __init__(self, result):
        self.result = result
        self.cmd: list[str] | None = None
        self.kwargs: dict | None = None

    def __call__(self, cmd, **kwargs):
        self.cmd = list(cmd)
        self.kwargs = kwargs
        return self.result


class TestSshRunnerRun:
    def _runner(self):
        target = SshTarget(user='a', host='h')
        return SshRunner(target=target)

    def test_successful_json_response(self):
        result = _ok_response(data=42)
        with patch('subprocess.run', return_value=result):
            data = self._runner().run('list-stores', {})
        assert data['data'] == 42

    def test_remote_command_is_the_rpc_constant(self):
        """The remote command is exactly ``voxhub-server rpc`` — nothing else."""
        capture = _CapturingRun(_ok_response())
        with patch('subprocess.run', capture):
            self._runner().run('list-stores', {})
        assert capture.cmd is not None
        assert capture.cmd[-1] == RPC_REMOTE_COMMAND
        assert capture.cmd[-1] == 'voxhub-server rpc'
        # Immediately after the ``--`` end-of-options sentinel.
        assert capture.cmd[capture.cmd.index('--') + 1] == RPC_REMOTE_COMMAND

    def test_request_payload_arrives_on_stdin_as_json(self):
        capture = _CapturingRun(_ok_response())
        params = {'store_name': 'patient-001', 'compress': True}
        with patch('subprocess.run', capture):
            self._runner().run('prepare-pull', params)
        assert capture.kwargs is not None
        request = json.loads(capture.kwargs['input'])
        assert request == {
            'protocol_version': PROTOCOL_VERSION,
            'method': 'prepare-pull',
            'params': params,
        }

    def test_injection_shaped_params_never_reach_argv(self):
        """Shell metacharacters travel as inert JSON data, never argv."""
        capture = _CapturingRun(_ok_response())
        hostile = {
            'store_name': '"; echo pwned',
            'staging_dir': '/tmp/has spaces/and * glob',
        }
        with patch('subprocess.run', capture):
            self._runner().run('prepare-pull', hostile)
        assert capture.cmd is not None
        for element in capture.cmd:
            assert 'echo pwned' not in element
            assert '*' not in element
            assert 'has spaces' not in element
        # argv is the fixed transport frame: ssh options + destination
        # + '--' + the constant remote command.
        assert capture.cmd[0] == 'ssh'
        assert capture.cmd[-1] == RPC_REMOTE_COMMAND
        # The hostile bytes round-trip through stdin instead.
        request = json.loads(capture.kwargs['input'])  # type: ignore[index]
        assert request['params'] == hostile

    def test_nonzero_exit_no_stdout_raises_ssh_failed(self):
        result = _mock_run_result(returncode=255, stderr='Connection refused')
        with patch('subprocess.run', return_value=result):
            with pytest.raises(RemoteError, match='Connection refused') as exc:
                self._runner().run('list-stores', {})
            assert exc.value.code == 'ssh_failed'

    def test_malformed_json_raises_parse_error(self):
        result = _mock_run_result(stdout='not json at all')
        with patch('subprocess.run', return_value=result):
            with pytest.raises(RemoteError) as exc:
                self._runner().run('list-stores', {})
            assert exc.value.code == 'parse_error'

    def test_server_error_envelope(self):
        payload = {
            'error': True,
            'code': 'validation_failed',
            'message': 'Shape mismatch',
            'protocol_version': PROTOCOL_VERSION,
        }
        result = _mock_run_result(stdout=json.dumps(payload))
        with patch('subprocess.run', return_value=result):
            with pytest.raises(RemoteError, match='Shape mismatch') as exc:
                self._runner().run('integrate-annotations', {})
            assert exc.value.code == 'validation_failed'
            assert exc.value.protocol_version == PROTOCOL_VERSION

    def test_protocol_mismatch_raises_naming_both_versions(self):
        wrong_version = PROTOCOL_VERSION + 99
        payload = {'protocol_version': wrong_version}
        result = _mock_run_result(stdout=json.dumps(payload))
        with patch('subprocess.run', return_value=result):
            with pytest.raises(ProtocolMismatchError) as exc:
                self._runner().run('list-stores', {})
            assert exc.value.protocol_version == wrong_version
            assert str(wrong_version) in str(exc.value)
            assert str(PROTOCOL_VERSION) in str(exc.value)

    def test_matching_protocol_version_no_error(self):
        result = _ok_response(ok=True)
        with patch('subprocess.run', return_value=result):
            data = self._runner().run('list-stores', {})
        assert data['ok'] is True

    def test_missing_protocol_version_raises(self):
        """A response without the field is a wrong/old binary on the pipe."""
        payload = {'result': 'ok'}
        result = _mock_run_result(stdout=json.dumps(payload))
        with patch('subprocess.run', return_value=result):
            with pytest.raises(RemoteError, match='missing protocol_version') as exc:
                self._runner().run('list-stores', {})
            assert exc.value.code == 'protocol_mismatch'


# ===================================================================
# TIMEOUT POLICY
# ===================================================================


class TestSshRunnerTimeouts:
    def _runner(self):
        target = SshTarget(user='a', host='h')
        return SshRunner(target=target)

    def test_default_timeout_for_short_methods(self):
        capture = _CapturingRun(_ok_response())
        with patch('subprocess.run', capture):
            self._runner().run('list-stores', {})
        assert capture.kwargs is not None
        assert capture.kwargs['timeout'] == DEFAULT_TIMEOUT_S == 60.0

    def test_prepare_pull_uses_long_timeout(self):
        """Staging a full volume takes far longer than the 60 s default."""
        capture = _CapturingRun(_ok_response())
        with patch('subprocess.run', capture):
            self._runner().run('prepare-pull', {'store_name': 'x'})
        assert capture.kwargs is not None
        assert capture.kwargs['timeout'] == METHOD_TIMEOUTS_S['prepare-pull'] == 1800.0

    def test_explicit_timeout_overrides_policy(self):
        capture = _CapturingRun(_ok_response())
        with patch('subprocess.run', capture):
            self._runner().run('prepare-pull', {'store_name': 'x'}, timeout=5.0)
        assert capture.kwargs is not None
        assert capture.kwargs['timeout'] == 5.0

    def test_timeout_surfaces_as_remote_error(self):
        """A stub sleeping past the timeout raises RemoteError, never
        ``TimeoutExpired``."""
        real_run = subprocess.run

        def sleepy_run(cmd, **kwargs):
            # Replace the ssh argv with a local sleeper; keep every kwarg
            # (input, capture_output, text, and crucially timeout) so the
            # real subprocess timeout machinery fires.
            return real_run(
                [sys.executable, '-c', 'import time; time.sleep(5)'],
                **kwargs,
            )

        with patch('subprocess.run', side_effect=sleepy_run):
            with pytest.raises(RemoteError, match=r'timed out after 0\.2') as exc:
                self._runner().run('cleanup', {'staging_dir': '/x'}, timeout=0.2)
            assert exc.value.code == 'timeout'
            assert not isinstance(exc.value, subprocess.TimeoutExpired)

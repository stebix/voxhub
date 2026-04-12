"""Tests for SSH transport: target parsing, command building, response handling."""

import json
import subprocess
from unittest.mock import patch

import pytest

from voxhub_client.ssh import (
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


# ===================================================================
# SSH RUNNER RUN
# ===================================================================


def _mock_run_result(stdout='', stderr='', returncode=0):
    """Create a mock subprocess.CompletedProcess."""
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


class TestSshRunnerRun:
    def _runner(self):
        target = SshTarget(user='a', host='h')
        return SshRunner(target=target)

    def test_successful_json_response(self):
        payload = {'protocol_version': PROTOCOL_VERSION, 'data': 42}
        result = _mock_run_result(stdout=json.dumps(payload))
        with patch('subprocess.run', return_value=result):
            data = self._runner().run('list-stores')
        assert data['data'] == 42

    def test_nonzero_exit_no_stdout_raises_ssh_failed(self):
        result = _mock_run_result(returncode=255, stderr='Connection refused')
        with patch('subprocess.run', return_value=result):
            with pytest.raises(RemoteError, match='Connection refused') as exc:
                self._runner().run('list-stores')
            assert exc.value.code == 'ssh_failed'

    def test_malformed_json_raises_parse_error(self):
        result = _mock_run_result(stdout='not json at all')
        with patch('subprocess.run', return_value=result):
            with pytest.raises(RemoteError) as exc:
                self._runner().run('list-stores')
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
                self._runner().run('integrate')
            assert exc.value.code == 'validation_failed'
            assert exc.value.protocol_version == PROTOCOL_VERSION

    def test_protocol_mismatch_raises(self):
        wrong_version = PROTOCOL_VERSION + 99
        payload = {'protocol_version': wrong_version}
        result = _mock_run_result(stdout=json.dumps(payload))
        with patch('subprocess.run', return_value=result):
            with pytest.raises(ProtocolMismatchError) as exc:
                self._runner().run('list-stores')
            assert exc.value.protocol_version == wrong_version

    def test_matching_protocol_version_no_error(self):
        payload = {'protocol_version': PROTOCOL_VERSION, 'ok': True}
        result = _mock_run_result(stdout=json.dumps(payload))
        with patch('subprocess.run', return_value=result):
            data = self._runner().run('list-stores')
        assert data['ok'] is True

    def test_no_protocol_version_in_response_no_error(self):
        """Responses without protocol_version skip the check."""
        payload = {'result': 'ok'}
        result = _mock_run_result(stdout=json.dumps(payload))
        with patch('subprocess.run', return_value=result):
            data = self._runner().run('some-cmd')
        assert data['result'] == 'ok'


# ===================================================================
# SSH RUNNER MKTEMP
# ===================================================================


class TestSshRunnerMktemp:
    def _runner(self):
        target = SshTarget(user='a', host='h')
        return SshRunner(target=target)

    def test_success_returns_stripped_path(self):
        result = _mock_run_result(stdout='/tmp/dt-push-abcdef\n')
        with patch('subprocess.run', return_value=result):
            path = self._runner().mktemp()
        assert path == '/tmp/dt-push-abcdef'

    def test_failure_raises_remote_error(self):
        result = _mock_run_result(returncode=1, stderr='Permission denied')
        with patch('subprocess.run', return_value=result):
            with pytest.raises(RemoteError, match='Permission denied') as exc:
                self._runner().mktemp()
            assert exc.value.code == 'mktemp_failed'

"""Tests for rsync/scp transfer command construction."""

from unittest.mock import patch

import pytest

from voxhub_client.ssh import SshTarget
from voxhub_client.transfer import RsyncTransfer, ScpFallback


@pytest.fixture
def target():
    return SshTarget(user='alice', host='server')


@pytest.fixture
def target_with_port():
    return SshTarget(user='alice', host='server', port=2222)


# ===================================================================
# RSYNC SSH OPTION
# ===================================================================


class TestRsyncSshOption:
    def test_no_port(self, target):
        xfer = RsyncTransfer(target=target)
        assert xfer._ssh_option() == 'ssh'

    def test_custom_port(self, target_with_port):
        xfer = RsyncTransfer(target=target_with_port)
        assert xfer._ssh_option() == 'ssh -p 2222'


# ===================================================================
# RSYNC COMMAND BUILDING
# ===================================================================


class TestRsyncPull:
    def test_pull_appends_trailing_slashes(self, target):
        xfer = RsyncTransfer(target=target)
        with patch('subprocess.run') as mock:
            xfer.pull('/remote/staging', '/local/staging', progress=False)
        cmd = mock.call_args[0][0]
        # Source and dest should have trailing slashes.
        assert cmd[-1] == '/local/staging/'
        assert cmd[-2].endswith('/remote/staging/')

    def test_pull_existing_trailing_slashes_not_doubled(self, target):
        xfer = RsyncTransfer(target=target)
        with patch('subprocess.run') as mock:
            xfer.pull('/remote/staging/', '/local/staging/', progress=False)
        cmd = mock.call_args[0][0]
        assert not cmd[-1].endswith('//')
        assert not cmd[-2].endswith('//')

    def test_pull_includes_az_flag(self, target):
        xfer = RsyncTransfer(target=target)
        with patch('subprocess.run') as mock:
            xfer.pull('/r', '/l', progress=False)
        cmd = mock.call_args[0][0]
        assert '-az' in cmd

    def test_pull_progress_true(self, target):
        xfer = RsyncTransfer(target=target)
        with patch('subprocess.run') as mock:
            xfer.pull('/r', '/l', progress=True)
        cmd = mock.call_args[0][0]
        assert '--progress' in cmd

    def test_pull_progress_false(self, target):
        xfer = RsyncTransfer(target=target)
        with patch('subprocess.run') as mock:
            xfer.pull('/r', '/l', progress=False)
        cmd = mock.call_args[0][0]
        assert '--progress' not in cmd

    def test_pull_source_is_remote(self, target):
        xfer = RsyncTransfer(target=target)
        with patch('subprocess.run') as mock:
            xfer.pull('/remote/staging', '/local', progress=False)
        cmd = mock.call_args[0][0]
        src = cmd[-2]
        assert src.startswith('alice@server:')

    def test_pull_custom_port_in_ssh_option(self, target_with_port):
        xfer = RsyncTransfer(target=target_with_port)
        with patch('subprocess.run') as mock:
            xfer.pull('/r', '/l', progress=False)
        cmd = mock.call_args[0][0]
        e_idx = cmd.index('-e')
        assert cmd[e_idx + 1] == 'ssh -p 2222'


class TestRsyncPush:
    def test_push_local_source_remote_dest(self, target):
        xfer = RsyncTransfer(target=target)
        with patch('subprocess.run') as mock:
            xfer.push('/local/staging', '/remote/staging', progress=False)
        cmd = mock.call_args[0][0]
        # Local is source (second-to-last), remote is dest (last).
        assert cmd[-2] == '/local/staging/'
        assert cmd[-1].startswith('alice@server:')

    def test_push_appends_trailing_slashes(self, target):
        xfer = RsyncTransfer(target=target)
        with patch('subprocess.run') as mock:
            xfer.push('/local', '/remote', progress=False)
        cmd = mock.call_args[0][0]
        assert cmd[-2].endswith('/')
        assert cmd[-1].endswith('/')


# ===================================================================
# SCP FALLBACK
# ===================================================================


class TestScpFallback:
    def test_pull_command(self, target):
        scp = ScpFallback(target=target)
        with patch('subprocess.run') as mock:
            scp.pull('/remote/file', '/local/dest')
        cmd = mock.call_args[0][0]
        assert cmd[0] == 'scp'
        assert '-r' in cmd
        assert 'alice@server:/remote/file' in cmd
        assert '/local/dest' in cmd

    def test_push_command(self, target):
        scp = ScpFallback(target=target)
        with patch('subprocess.run') as mock:
            scp.push('/local/src', '/remote/dst')
        cmd = mock.call_args[0][0]
        assert cmd[0] == 'scp'
        assert '/local/src' in cmd
        assert 'alice@server:/remote/dst' in cmd

    def test_pull_with_port(self, target_with_port):
        scp = ScpFallback(target=target_with_port)
        with patch('subprocess.run') as mock:
            scp.pull('/r', '/l')
        cmd = mock.call_args[0][0]
        p_idx = cmd.index('-P')
        assert cmd[p_idx + 1] == '2222'

    def test_push_with_port(self, target_with_port):
        scp = ScpFallback(target=target_with_port)
        with patch('subprocess.run') as mock:
            scp.push('/l', '/r')
        cmd = mock.call_args[0][0]
        p_idx = cmd.index('-P')
        assert cmd[p_idx + 1] == '2222'

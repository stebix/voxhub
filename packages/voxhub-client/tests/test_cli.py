"""Tests for CLI utilities and argument parsing."""

import argparse
import hashlib

from voxhub_client.cli import _compute_sha256

# ===================================================================
# SHA-256
# ===================================================================


class TestComputeSha256:
    def test_known_content(self, tmp_path):
        f = tmp_path / 'data.bin'
        f.write_bytes(b'hello world')
        expected = 'sha256:' + hashlib.sha256(b'hello world').hexdigest()
        assert _compute_sha256(f) == expected

    def test_empty_file(self, tmp_path):
        f = tmp_path / 'empty'
        f.write_bytes(b'')
        expected = 'sha256:' + hashlib.sha256(b'').hexdigest()
        assert _compute_sha256(f) == expected


# ===================================================================
# ARG PARSER
# ===================================================================


class TestArgParser:
    def _parse(self, argv):
        """Parse argv through a minimal replica of the CLI subcommands."""
        parser = argparse.ArgumentParser(prog='voxhub')
        subparsers = parser.add_subparsers(dest='command')
        subparsers.add_parser('pull')
        subparsers.add_parser('push')
        subparsers.add_parser('remote-catalog')
        subparsers.add_parser('whoami')
        subparsers.add_parser('set-identity')
        return parser.parse_args(argv)

    def test_pull_subcommand(self):
        args = self._parse(['pull'])
        assert args.command == 'pull'

    def test_push_subcommand(self):
        args = self._parse(['push'])
        assert args.command == 'push'

    def test_whoami_subcommand(self):
        args = self._parse(['whoami'])
        assert args.command == 'whoami'

    def test_set_identity_subcommand(self):
        args = self._parse(['set-identity'])
        assert args.command == 'set-identity'

    def test_remote_catalog_subcommand(self):
        args = self._parse(['remote-catalog'])
        assert args.command == 'remote-catalog'

    def test_no_subcommand(self):
        args = self._parse([])
        assert args.command is None

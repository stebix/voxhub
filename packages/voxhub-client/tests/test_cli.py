"""Tests for the annotator-facing CLI argument parser."""

import argparse


class TestArgParser:
    def _parse(self, argv: list[str]) -> argparse.Namespace:
        """Parse argv through a minimal replica of the CLI subcommands."""
        parser = argparse.ArgumentParser(prog='voxhub')
        subparsers = parser.add_subparsers(dest='command')
        subparsers.add_parser('set-server')
        subparsers.add_parser('set-identity')
        subparsers.add_parser('whoami')
        return parser.parse_args(argv)

    def test_set_server_subcommand(self) -> None:
        args = self._parse(['set-server'])
        assert args.command == 'set-server'

    def test_set_identity_subcommand(self) -> None:
        args = self._parse(['set-identity'])
        assert args.command == 'set-identity'

    def test_whoami_subcommand(self) -> None:
        args = self._parse(['whoami'])
        assert args.command == 'whoami'

    def test_no_subcommand(self) -> None:
        args = self._parse([])
        assert args.command is None

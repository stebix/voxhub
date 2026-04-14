"""Tests for :mod:`voxhub_client.pull_log`."""

import json
import logging

import pytest

from voxhub_client import pull_log


@pytest.fixture
def isolated_log(monkeypatch, tmp_path):
    log_path = tmp_path / 'audit' / 'pulls.jsonl'
    monkeypatch.setattr(pull_log, '_PULL_LOG', log_path)
    return log_path


class TestAppendEntry:
    def test_creates_parent_dirs_and_appends(self, isolated_log):
        assert not isolated_log.parent.exists()
        pull_log.append_entry({'store': 'patient-001', 'dest': '/tmp/x'})
        assert isolated_log.parent.is_dir()
        lines = isolated_log.read_text().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry == {'store': 'patient-001', 'dest': '/tmp/x'}

    def test_two_calls_produce_two_lines(self, isolated_log):
        pull_log.append_entry({'store': 'a'})
        pull_log.append_entry({'store': 'b'})
        lines = isolated_log.read_text().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])['store'] == 'a'
        assert json.loads(lines[1])['store'] == 'b'

    def test_oserror_is_non_fatal(self, monkeypatch, caplog, tmp_path):
        # Point at a path whose parent *cannot* be created (pre-existing file).
        blocker = tmp_path / 'notadir'
        blocker.write_text('i am a file')
        monkeypatch.setattr(pull_log, '_PULL_LOG', blocker / 'sub' / 'pulls.jsonl')

        with caplog.at_level(logging.WARNING):
            # Must not raise.
            pull_log.append_entry({'store': 'x'})

        assert any('failed to append to pull log' in r.message for r in caplog.records)

    def test_entry_has_no_trust_surface(self, isolated_log):
        """A deliberate negative assertion: future refactors must NOT
        re-introduce a hash/digest field in the log.  Trust anchor is the
        in-session sidecar; the log is audit-only."""
        pull_log.append_entry(
            {
                'pulled_at': '2026-04-14T12:00:00+00:00',
                'store': 'patient-001',
                'dest': '/tmp/x',
                'server_host': 'voxhub@server',
                'protocol_version': 1,
            }
        )
        entry = json.loads(isolated_log.read_text().splitlines()[0])
        forbidden = {
            'manifest_sha256',
            'manifest_digest',
            'sha256',
            'trust_digest',
        }
        assert not (set(entry.keys()) & forbidden), (
            'pull log must remain trust-free; sidecar is the anchor'
        )

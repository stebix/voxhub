"""Tests for annotator identity management."""

import json

import pytest

from voxhub_client.identity import Identity, get_identity, get_machine_id, set_identity

# ===================================================================
# IDENTITY MODEL
# ===================================================================


class TestIdentityModel:
    def test_round_trip(self):
        orig = Identity(annotator_id='alice', nano_id='abc12345', machine_id='ff' * 8)
        rt = Identity.from_dict(orig.to_dict())
        assert rt.annotator_id == orig.annotator_id
        assert rt.nano_id == orig.nano_id
        assert rt.machine_id == orig.machine_id

    def test_from_dict_missing_key_raises(self):
        with pytest.raises(KeyError):
            Identity.from_dict({'annotator_id': 'alice', 'nano_id': 'x'})


# ===================================================================
# MACHINE ID
# ===================================================================


class TestMachineId:
    def test_returns_16_char_hex(self):
        mid = get_machine_id()
        assert len(mid) == 16
        int(mid, 16)  # raises ValueError if not hex

    def test_deterministic(self):
        assert get_machine_id() == get_machine_id()


# ===================================================================
# GET / SET IDENTITY (filesystem)
# ===================================================================


class TestGetSetIdentity:
    @pytest.fixture(autouse=True)
    def _redirect_config(self, tmp_path, monkeypatch):
        """Point identity file I/O at tmp_path."""
        config_dir = tmp_path / '.config' / 'voxhub'
        identity_file = config_dir / 'identity.json'
        monkeypatch.setattr('voxhub_client.identity._CONFIG_DIR', config_dir)
        monkeypatch.setattr('voxhub_client.identity._IDENTITY_FILE', identity_file)

    def test_get_before_set_raises(self):
        with pytest.raises(FileNotFoundError, match='Identity not configured'):
            get_identity()

    def test_set_returns_valid_identity(self):
        ident = set_identity('alice')
        assert ident.annotator_id == 'alice'
        assert len(ident.nano_id) == 8
        assert len(ident.machine_id) == 16

    def test_set_then_get_round_trips(self):
        set_identity('bob')
        ident = get_identity()
        assert ident.annotator_id == 'bob'

    def test_set_twice_overwrites(self):
        first = set_identity('alice')
        second = set_identity('carol')
        assert second.annotator_id == 'carol'
        assert second.nano_id != first.nano_id
        current = get_identity()
        assert current.annotator_id == 'carol'

    def test_identity_file_is_valid_json(self, tmp_path):
        set_identity('dave')
        identity_file = tmp_path / '.config' / 'voxhub' / 'identity.json'
        data = json.loads(identity_file.read_text())
        assert data['annotator_id'] == 'dave'
        assert 'nano_id' in data
        assert 'machine_id' in data

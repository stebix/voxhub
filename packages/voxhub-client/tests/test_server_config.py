"""Tests for remote server configuration management."""

import json

import pytest

from voxhub_client.server_config import (
    SERVER_INTERACTION_USER,
    ServerConfig,
    get_server,
    set_server,
)

# ===================================================================
# SERVER CONFIG MODEL
# ===================================================================


class TestServerConfigModel:
    def test_round_trip_no_port(self) -> None:
        orig = ServerConfig(host='annotate.lab.edu')
        rt = ServerConfig.from_dict(orig.to_dict())
        assert rt.host == orig.host
        assert rt.port is None

    def test_round_trip_with_port(self) -> None:
        orig = ServerConfig(host='annotate.lab.edu', port=2222)
        rt = ServerConfig.from_dict(orig.to_dict())
        assert rt.host == orig.host
        assert rt.port == orig.port

    def test_from_dict_missing_host_raises(self) -> None:
        with pytest.raises(KeyError):
            ServerConfig.from_dict({'port': None})

    def test_connection_string_no_port(self) -> None:
        config = ServerConfig(host='annotate.lab.edu')
        assert config.connection_string == f'{SERVER_INTERACTION_USER}@annotate.lab.edu'

    def test_connection_string_with_port(self) -> None:
        config = ServerConfig(host='annotate.lab.edu', port=2222)
        expected = f'{SERVER_INTERACTION_USER}@annotate.lab.edu:2222'
        assert config.connection_string == expected

    def test_to_ssh_target_uses_interaction_user(self) -> None:
        config = ServerConfig(host='annotate.lab.edu')
        target = config.to_ssh_target()
        assert target.user == SERVER_INTERACTION_USER

    def test_to_ssh_target_carries_host_and_port(self) -> None:
        config = ServerConfig(host='annotate.lab.edu', port=2222)
        target = config.to_ssh_target()
        assert target.host == 'annotate.lab.edu'
        assert target.port == 2222

    def test_to_ssh_target_no_port(self) -> None:
        config = ServerConfig(host='annotate.lab.edu')
        target = config.to_ssh_target()
        assert target.port is None


# ===================================================================
# GET / SET SERVER (filesystem)
# ===================================================================


class TestGetSetServer:
    @pytest.fixture(autouse=True)
    def _redirect_config(self, tmp_path, monkeypatch):
        """Point server file I/O at tmp_path."""
        config_dir = tmp_path / '.config' / 'voxhub'
        server_file = config_dir / 'server.json'
        monkeypatch.setattr('voxhub_client.server_config._CONFIG_DIR', config_dir)
        monkeypatch.setattr('voxhub_client.server_config._SERVER_FILE', server_file)

    def test_get_before_set_raises(self) -> None:
        with pytest.raises(FileNotFoundError, match='set-server'):
            get_server()

    def test_set_returns_valid_config(self) -> None:
        config = set_server('annotate.lab.edu')
        assert config.host == 'annotate.lab.edu'
        assert config.port is None

    def test_set_then_get_round_trips(self) -> None:
        set_server('annotate.lab.edu')
        config = get_server()
        assert config.host == 'annotate.lab.edu'

    def test_set_with_port_round_trips(self) -> None:
        set_server('annotate.lab.edu', port=2222)
        config = get_server()
        assert config.port == 2222

    def test_set_twice_overwrites(self) -> None:
        set_server('old-server.edu')
        set_server('new-server.edu')
        config = get_server()
        assert config.host == 'new-server.edu'

    def test_server_file_is_valid_json(self, tmp_path) -> None:
        set_server('annotate.lab.edu', port=2222)
        server_file = tmp_path / '.config' / 'voxhub' / 'server.json'
        data = json.loads(server_file.read_text())
        assert data['host'] == 'annotate.lab.edu'
        assert data['port'] == 2222

    def test_creates_config_dir(self, tmp_path) -> None:
        config_dir = tmp_path / '.config' / 'voxhub'
        assert not config_dir.exists()
        set_server('annotate.lab.edu')
        assert config_dir.is_dir()

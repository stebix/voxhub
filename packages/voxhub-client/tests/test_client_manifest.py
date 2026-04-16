"""Tests for client-side manifest operations."""

import pytest

from voxhub_client.manifest import read_manifest, update_manifest_status, write_manifest
from voxhub_schema import PROTOCOL_VERSION, RemoteManifest, RemoteManifestEntry


def _sample_manifest():
    """Build a minimal valid manifest."""
    return RemoteManifest(
        server_host='alice@server',
        server_stores_dir='/data/zarr',
        protocol_version=PROTOCOL_VERSION,
        pull_session_id='vxhb-staging-abc',
        pulled_at='2026-01-01T00:00:00+00:00',
        stores={
            'store-a': RemoteManifestEntry(
                status='pulled',
                raw_checksum='sha256:aaa',
                shape=[10, 12, 14],
                spacing_mm=[0.5, 0.5, 0.5],
                origin_lps=[-5.0, -6.0, -7.0],
                space_directions=[[0.5, 0, 0], [0, 0.5, 0], [0, 0, 0.5]],
                expected_ontologies=['inner-ear-structures'],
            ),
        },
    )


# ===================================================================
# ROUND-TRIP
# ===================================================================


class TestManifestRoundTrip:
    def test_write_then_read(self, tmp_path):
        orig = _sample_manifest()
        write_manifest(tmp_path, orig)
        rt = read_manifest(tmp_path)
        assert rt.server_host == orig.server_host
        assert rt.server_stores_dir == orig.server_stores_dir
        assert rt.protocol_version == orig.protocol_version
        assert rt.pull_session_id == orig.pull_session_id
        assert 'store-a' in rt.stores
        s = rt.stores['store-a']
        assert s.status == 'pulled'
        assert s.shape == [10, 12, 14]
        assert s.expected_ontologies == ['inner-ear-structures']

    def test_read_missing_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            read_manifest(tmp_path)

    def test_accepts_string_path(self, tmp_path):
        write_manifest(str(tmp_path), _sample_manifest())
        rt = read_manifest(str(tmp_path))
        assert rt.server_host == 'alice@server'


# ===================================================================
# UPDATE STATUS
# ===================================================================


class TestUpdateManifestStatus:
    def test_updates_status(self, tmp_path):
        write_manifest(tmp_path, _sample_manifest())
        update_manifest_status(tmp_path, 'store-a', 'integrated')
        rt = read_manifest(tmp_path)
        assert rt.stores['store-a'].status == 'integrated'

    def test_unknown_store_raises_key_error(self, tmp_path):
        write_manifest(tmp_path, _sample_manifest())
        with pytest.raises(KeyError, match='nonexistent'):
            update_manifest_status(tmp_path, 'nonexistent', 'pushed')

    def test_preserves_other_fields(self, tmp_path):
        write_manifest(tmp_path, _sample_manifest())
        update_manifest_status(tmp_path, 'store-a', 'pushed')
        rt = read_manifest(tmp_path)
        s = rt.stores['store-a']
        assert s.status == 'pushed'
        assert s.raw_checksum == 'sha256:aaa'
        assert s.shape == [10, 12, 14]

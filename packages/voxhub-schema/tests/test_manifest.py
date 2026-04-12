"""Tests for manifest serialization and persistence."""

import pytest

from voxhub_schema.manifest import RemoteManifest, RemoteManifestEntry


def _sample_manifest() -> RemoteManifest:
    return RemoteManifest(
        server_host='server.example.com',
        server_stores_dir='/data/zarr',
        protocol_version=1,
        pull_session_id='abc12345',
        pulled_at='2026-01-01T00:00:00+00:00',
        stores={
            'store-a': RemoteManifestEntry(
                status='pulled',
                raw_checksum='sha256:abc',
                shape=[10, 12, 14],
                spacing_mm=[0.5, 0.5, 0.5],
                origin_lps=[-5.0, -6.0, -7.0],
                space_directions=[[0.5, 0, 0], [0, 0.5, 0], [0, 0, 0.5]],
                expected_ontologies=['inner-ear-structures'],
                included_annotations=['annotations/alice-abc/seg-1'],
            ),
        },
    )


class TestManifestRoundTrip:
    def test_json_round_trip(self):
        m = _sample_manifest()
        text = m.to_json()
        rt = RemoteManifest.from_json(text)
        assert rt.server_host == m.server_host
        assert rt.server_stores_dir == m.server_stores_dir
        assert rt.protocol_version == m.protocol_version
        assert rt.pull_session_id == m.pull_session_id

    def test_stores_preserved(self):
        m = _sample_manifest()
        rt = RemoteManifest.from_json(m.to_json())
        entry = rt.stores['store-a']
        assert entry.status == 'pulled'
        assert entry.shape == [10, 12, 14]
        assert entry.spacing_mm == [0.5, 0.5, 0.5]
        assert entry.origin_lps == [-5.0, -6.0, -7.0]
        assert entry.space_directions == [[0.5, 0, 0], [0, 0.5, 0], [0, 0, 0.5]]
        assert entry.expected_ontologies == ['inner-ear-structures']
        assert entry.included_annotations == ['annotations/alice-abc/seg-1']

    def test_disk_round_trip(self, tmp_path):
        m = _sample_manifest()
        m.write(tmp_path)
        rt = RemoteManifest.read(tmp_path)
        assert rt.server_host == m.server_host
        assert rt.stores['store-a'].shape == [10, 12, 14]

    def test_read_missing_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            RemoteManifest.read(tmp_path / 'missing')


class TestManifestEntry:
    def test_default_included_annotations_empty(self):
        entry = RemoteManifestEntry(
            status='pulled',
            raw_checksum='sha256:abc',
            shape=[10, 12, 14],
            spacing_mm=[0.5, 0.5, 0.5],
            origin_lps=[-5.0, -6.0, -7.0],
            space_directions=[[0.5, 0, 0], [0, 0.5, 0], [0, 0, 0.5]],
            expected_ontologies=[],
        )
        assert entry.included_annotations == []

    def test_from_dict_without_optional_keys(self):
        d = {
            'status': 'pulled',
            'raw_checksum': 'sha256:abc',
            'shape': [10, 12, 14],
            'spacing_mm': [0.5, 0.5, 0.5],
            'origin_lps': [-5.0, -6.0, -7.0],
            'space_directions': [[0.5, 0, 0], [0, 0.5, 0], [0, 0, 0.5]],
        }
        entry = RemoteManifestEntry.from_dict(d)
        assert entry.expected_ontologies == []
        assert entry.included_annotations == []

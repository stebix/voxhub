"""Tests for manifest serialization and persistence."""

import json

import pytest

from voxhub_schema.manifest import (
    ManifestError,
    RemoteManifest,
    RemoteManifestEntry,
)


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


def _minimal_remote_dict() -> dict[str, object]:
    return {
        'server_host': 'h',
        'server_stores_dir': '/s',
        'protocol_version': 1,
        'pull_session_id': 'sess1',
        'pulled_at': '2026-01-01T00:00:00+00:00',
        'stores': {},
    }


class TestRemoteManifestErrors:
    """``ManifestError`` is raised for malformed manifests and chains the cause."""

    def test_from_dict_missing_required_key_raises_manifest_error(self):
        d = _minimal_remote_dict()
        del d['server_host']
        with pytest.raises(ManifestError) as excinfo:
            RemoteManifest.from_dict(d)
        assert isinstance(excinfo.value.__cause__, KeyError)

    def test_from_dict_wrong_typed_protocol_version_raises_manifest_error(self):
        d = _minimal_remote_dict()
        d['protocol_version'] = 'not-an-int'
        with pytest.raises(ManifestError) as excinfo:
            RemoteManifest.from_dict(d)
        assert isinstance(excinfo.value.__cause__, ValueError)

    def test_from_dict_malformed_nested_entry_raises_manifest_error(self):
        d = _minimal_remote_dict()
        d['stores'] = {'store-a': {'status': 'pulled'}}  # missing required keys
        with pytest.raises(ManifestError) as excinfo:
            RemoteManifest.from_dict(d)
        assert isinstance(excinfo.value.__cause__, (KeyError, ManifestError))

    def test_from_json_invalid_json_raises_manifest_error(self):
        with pytest.raises(ManifestError) as excinfo:
            RemoteManifest.from_json('{not valid json')
        assert isinstance(excinfo.value.__cause__, json.JSONDecodeError)

    def test_read_corrupt_file_raises_manifest_error_not_filenotfound(self, tmp_path):
        (tmp_path / '.voxhub_manifest.json').write_text('not json at all')
        with pytest.raises(ManifestError):
            RemoteManifest.read(tmp_path)


class TestRemoteManifestEntryErrors:
    def test_from_dict_missing_key_raises_manifest_error(self):
        d: dict[str, object] = {
            'status': 'pulled',
            # 'raw_checksum' missing
            'shape': [10, 12, 14],
            'spacing_mm': [0.5, 0.5, 0.5],
            'origin_lps': [-5.0, -6.0, -7.0],
            'space_directions': [[0.5, 0, 0], [0, 0.5, 0], [0, 0, 0.5]],
        }
        with pytest.raises(ManifestError) as excinfo:
            RemoteManifestEntry.from_dict(d)
        assert isinstance(excinfo.value.__cause__, KeyError)

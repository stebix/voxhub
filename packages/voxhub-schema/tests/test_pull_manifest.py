"""Tests for :class:`PullManifest` and :class:`PullAnnotationEntry`."""

import json

import pytest

from voxhub_schema.manifest import (
    ManifestError,
    PullAnnotationEntry,
    PullManifest,
)


def _sample_manifest(
    *, annotations: list[PullAnnotationEntry] | None = None
) -> PullManifest:
    return PullManifest(
        protocol_version=1,
        prepared_at='2026-04-14T12:00:00+00:00',
        server_host='server.example.com',
        server_stores_dir='/srv/voxhub/stores',
        store_name='patient-001',
        raw_name='raw.nrrd',
        raw_checksum='sha256:raw-hex',
        shape=[10, 12, 14],
        spacing_mm=[0.5, 0.5, 0.5],
        origin_lps=[-5.0, -6.0, -7.0],
        space_directions=[[0.5, 0, 0], [0, 0.5, 0], [0, 0, 0.5]],
        annotations=annotations or [],
    )


def _seg_entry() -> PullAnnotationEntry:
    return PullAnnotationEntry(
        zarr_source_path=(
            'annotations/alice-xyz45678/inner-ear-structures-20260101-ab12'
        ),
        kind='segmentation',
        ontology='inner-ear-structures',
        ontology_version=1,
        annotator_id='alice',
        integrated_at='2026-01-01T08:00:00+00:00',
        reference_filename=('alice-xyz45678_inner-ear-structures-20260101-ab12.seg.nrrd'),
        reference_checksum='sha256:seg-hex',
    )


def _lmk_entry() -> PullAnnotationEntry:
    return PullAnnotationEntry(
        zarr_source_path=('annotations/alice-xyz45678/inner-ear-landmarks-20260101-cd34'),
        kind='landmarks',
        ontology='inner-ear-landmarks',
        ontology_version=2,
        annotator_id='alice',
        integrated_at='2026-01-01T09:00:00+00:00',
        reference_filename=('alice-xyz45678_inner-ear-landmarks-20260101-cd34.mrk.json'),
        reference_checksum='sha256:lmk-hex',
    )


class TestPullManifestRoundTrip:
    def test_json_round_trip_no_annotations(self):
        m = _sample_manifest()
        rt = PullManifest.from_json(m.to_json())
        assert rt.protocol_version == 1
        assert rt.server_host == 'server.example.com'
        assert rt.server_stores_dir == '/srv/voxhub/stores'
        assert rt.store_name == 'patient-001'
        assert rt.raw_name == 'raw.nrrd'
        assert rt.raw_checksum == 'sha256:raw-hex'
        assert rt.shape == [10, 12, 14]
        assert rt.spacing_mm == [0.5, 0.5, 0.5]
        assert rt.origin_lps == [-5.0, -6.0, -7.0]
        assert rt.space_directions == [[0.5, 0, 0], [0, 0.5, 0], [0, 0, 0.5]]
        assert rt.annotations == []

    def test_json_round_trip_with_segmentation_and_landmarks(self):
        m = _sample_manifest(annotations=[_seg_entry(), _lmk_entry()])
        rt = PullManifest.from_json(m.to_json())
        assert len(rt.annotations) == 2
        seg, lmk = rt.annotations
        assert seg.kind == 'segmentation'
        assert seg.reference_filename.endswith('.seg.nrrd')
        assert seg.reference_checksum == 'sha256:seg-hex'
        assert seg.annotator_id == 'alice'
        assert seg.ontology == 'inner-ear-structures'
        assert seg.ontology_version == 1
        assert lmk.kind == 'landmarks'
        assert lmk.reference_filename.endswith('.mrk.json')
        assert lmk.ontology_version == 2

    def test_disk_round_trip(self, tmp_path):
        m = _sample_manifest(annotations=[_seg_entry()])
        m.write(tmp_path)
        assert (tmp_path / '.voxhub_pull.json').is_file()
        rt = PullManifest.read(tmp_path)
        assert rt.store_name == m.store_name
        assert rt.raw_checksum == m.raw_checksum
        assert rt.annotations[0].reference_filename == (
            'alice-xyz45678_inner-ear-structures-20260101-ab12.seg.nrrd'
        )

    def test_read_missing_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            PullManifest.read(tmp_path)


class TestPullManifestFromDict:
    def test_from_dict_without_annotations_defaults_to_empty(self):
        d = {
            'protocol_version': 1,
            'prepared_at': '2026-04-14T12:00:00+00:00',
            'server_host': 'server.example.com',
            'server_stores_dir': '/srv/voxhub/stores',
            'store_name': 'patient-001',
            'raw_name': 'raw.nrrd',
            'raw_checksum': 'sha256:raw-hex',
            'shape': [10, 12, 14],
            'spacing_mm': [0.5, 0.5, 0.5],
            'origin_lps': [-5.0, -6.0, -7.0],
            'space_directions': [[0.5, 0, 0], [0, 0.5, 0], [0, 0, 0.5]],
        }
        m = PullManifest.from_dict(d)
        assert m.annotations == []

    def test_from_dict_with_null_annotations(self):
        d = {
            'protocol_version': 1,
            'prepared_at': '2026-04-14T12:00:00+00:00',
            'server_host': 'h',
            'server_stores_dir': '/s',
            'store_name': 'x',
            'raw_name': 'raw.nrrd',
            'raw_checksum': 'sha256:x',
            'shape': [1, 1, 1],
            'spacing_mm': [1.0, 1.0, 1.0],
            'origin_lps': [0.0, 0.0, 0.0],
            'space_directions': [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
            'annotations': None,
        }
        m = PullManifest.from_dict(d)
        assert m.annotations == []


class TestPullAnnotationEntry:
    def test_from_dict_segmentation(self):
        d = {
            'zarr_source_path': 'annotations/alice-xyz45678/inst-20260101-ab12',
            'kind': 'segmentation',
            'ontology': 'inner-ear-structures',
            'ontology_version': 1,
            'annotator_id': 'alice',
            'integrated_at': '2026-01-01T08:00:00+00:00',
            'reference_filename': 'alice-xyz45678_inst-20260101-ab12.seg.nrrd',
            'reference_checksum': 'sha256:x',
        }
        e = PullAnnotationEntry.from_dict(d)
        assert e.kind == 'segmentation'
        assert e.ontology_version == 1
        assert e.reference_filename.endswith('.seg.nrrd')

    def test_from_dict_landmarks(self):
        d = {
            'zarr_source_path': 'annotations/alice-xyz45678/inst-20260101-ab12',
            'kind': 'landmarks',
            'ontology': 'inner-ear-landmarks',
            'ontology_version': 2,
            'annotator_id': 'alice',
            'integrated_at': '2026-01-01T09:00:00+00:00',
            'reference_filename': 'alice-xyz45678_inst-20260101-ab12.mrk.json',
            'reference_checksum': 'sha256:y',
        }
        e = PullAnnotationEntry.from_dict(d)
        assert e.kind == 'landmarks'
        assert e.ontology_version == 2


def _minimal_dict() -> dict[str, object]:
    return {
        'protocol_version': 1,
        'prepared_at': '2026-04-14T12:00:00+00:00',
        'server_host': 'h',
        'server_stores_dir': '/s',
        'store_name': 'x',
        'raw_name': 'raw.nrrd',
        'raw_checksum': 'sha256:x',
        'shape': [1, 1, 1],
        'spacing_mm': [1.0, 1.0, 1.0],
        'origin_lps': [0.0, 0.0, 0.0],
        'space_directions': [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
    }


class TestPullManifestErrors:
    """``ManifestError`` is raised for malformed manifests and chains the cause."""

    def test_from_dict_missing_required_key_raises_manifest_error(self):
        d = _minimal_dict()
        del d['raw_checksum']
        with pytest.raises(ManifestError) as excinfo:
            PullManifest.from_dict(d)
        # Original cause preserved for debugging.
        assert isinstance(excinfo.value.__cause__, KeyError)
        assert 'raw_checksum' in str(excinfo.value.__cause__)

    def test_from_dict_wrong_typed_protocol_version_raises_manifest_error(self):
        d = _minimal_dict()
        d['protocol_version'] = 'not-an-int'
        with pytest.raises(ManifestError) as excinfo:
            PullManifest.from_dict(d)
        assert isinstance(excinfo.value.__cause__, ValueError)

    def test_from_dict_malformed_nested_annotation_raises_manifest_error(self):
        d = _minimal_dict()
        # Annotation entry missing required fields.
        d['annotations'] = [{'kind': 'segmentation'}]
        with pytest.raises(ManifestError) as excinfo:
            PullManifest.from_dict(d)
        # The nested error chains through.
        assert isinstance(excinfo.value.__cause__, (KeyError, ManifestError))

    def test_from_json_invalid_json_raises_manifest_error(self):
        with pytest.raises(ManifestError) as excinfo:
            PullManifest.from_json('{not valid json')
        assert isinstance(excinfo.value.__cause__, json.JSONDecodeError)

    def test_from_json_valid_json_but_malformed_schema_raises_manifest_error(self):
        # Valid JSON but missing schema fields.
        with pytest.raises(ManifestError) as excinfo:
            PullManifest.from_json('{"protocol_version": 1}')
        # Schema-level errors chain via from_dict, so cause is KeyError.
        assert isinstance(excinfo.value.__cause__, KeyError)

    def test_read_corrupt_file_raises_manifest_error_not_filenotfound(self, tmp_path):
        (tmp_path / '.voxhub_pull.json').write_text('not json at all')
        with pytest.raises(ManifestError):
            PullManifest.read(tmp_path)

    def test_read_missing_file_still_raises_filenotfounderror(self, tmp_path):
        # Regression guard: missing != malformed, distinct exception types.
        with pytest.raises(FileNotFoundError):
            PullManifest.read(tmp_path)


class TestPullAnnotationEntryErrors:
    def test_from_dict_missing_key_raises_manifest_error(self):
        d: dict[str, object] = {
            'zarr_source_path': 'annotations/alice-xyz45678/inst-20260101-ab12',
            'kind': 'segmentation',
            # 'ontology' missing
            'ontology_version': 1,
            'annotator_id': 'alice',
            'integrated_at': '2026-01-01T08:00:00+00:00',
            'reference_filename': 'x.seg.nrrd',
            'reference_checksum': 'sha256:x',
        }
        with pytest.raises(ManifestError) as excinfo:
            PullAnnotationEntry.from_dict(d)
        assert isinstance(excinfo.value.__cause__, KeyError)

    def test_from_dict_wrong_typed_ontology_version_raises_manifest_error(self):
        d: dict[str, object] = {
            'zarr_source_path': 'annotations/alice-xyz45678/inst-20260101-ab12',
            'kind': 'segmentation',
            'ontology': 'inner-ear-structures',
            'ontology_version': 'not-an-int',
            'annotator_id': 'alice',
            'integrated_at': '2026-01-01T08:00:00+00:00',
            'reference_filename': 'x.seg.nrrd',
            'reference_checksum': 'sha256:x',
        }
        with pytest.raises(ManifestError) as excinfo:
            PullAnnotationEntry.from_dict(d)
        assert isinstance(excinfo.value.__cause__, ValueError)

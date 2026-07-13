"""Tests for protocol model serialization round-trips."""

import json

import pytest

from voxhub_schema.models import (
    PROTOCOL_VERSION,
    AnnotationInfo,
    ChecksumEntry,
    CleanupResponse,
    GcResponse,
    IntegratedAnnotation,
    IntegrateRequest,
    IntegrateResponse,
    IntegrateResult,
    IssueRecord,
    PrepareRequest,
    PrepareResponse,
    ServerError,
    serialize,
)


def _round_trip(obj: object, from_dict_cls: type):
    """Serialize to JSON, parse back, and compare."""
    text = serialize(obj)
    d = json.loads(text)
    return from_dict_cls.from_dict(d)


class TestIssueRecord:
    def test_round_trip(self):
        orig = IssueRecord(severity='error', message='something broke')
        rt = _round_trip(orig, IssueRecord)
        assert rt.severity == orig.severity
        assert rt.message == orig.message


class TestAnnotationInfo:
    def test_round_trip(self):
        orig = AnnotationInfo(
            ontology='inner-ear-structures',
            ontology_version=1,
            annotator_id='alice',
            integrated_at='2026-01-01T00:00:00',
        )
        rt = _round_trip(orig, AnnotationInfo)
        assert rt.ontology == orig.ontology
        assert rt.ontology_version == orig.ontology_version
        assert rt.annotator_id == orig.annotator_id


class TestPrepareResponse:
    def test_round_trip(self):
        orig = PrepareResponse(
            protocol_version=PROTOCOL_VERSION,
            staging_dir='/tmp/staging',
            server_host='server.example.com',
            server_stores_dir='/srv/voxhub/zarr',
            store_name='store-a',
            raw_name='raw.nrrd',
            raw_checksum='sha256:abc',
            shape=[10, 12, 14],
            spacing_mm=[0.5, 0.5, 0.5],
            origin_lps=[-5.0, -6.0, -7.0],
            space_directions=[[0.5, 0, 0], [0, 0.5, 0], [0, 0, 0.5]],
        )
        rt = _round_trip(orig, PrepareResponse)
        assert rt.protocol_version == PROTOCOL_VERSION
        assert rt.server_stores_dir == '/srv/voxhub/zarr'
        assert rt.store_name == 'store-a'
        assert rt.raw_name == 'raw.nrrd'
        assert rt.raw_checksum == 'sha256:abc'
        assert rt.shape == [10, 12, 14]
        assert rt.skipped_annotations == []

    def test_round_trip_with_skipped_annotations(self):
        orig = PrepareResponse(
            protocol_version=PROTOCOL_VERSION,
            staging_dir='/tmp/staging',
            server_host='server.example.com',
            server_stores_dir='/srv/voxhub/zarr',
            store_name='store-a',
            raw_name='raw.nrrd',
            raw_checksum='sha256:abc',
            shape=[10, 12, 14],
            spacing_mm=[0.5, 0.5, 0.5],
            origin_lps=[-5.0, -6.0, -7.0],
            space_directions=[[0.5, 0, 0], [0, 0.5, 0], [0, 0, 0.5]],
            skipped_annotations=[
                {'path': 'annotations/alice-xyz/bad', 'reason': 'malformed'},
            ],
        )
        rt = _round_trip(orig, PrepareResponse)
        assert rt.skipped_annotations == [
            {'path': 'annotations/alice-xyz/bad', 'reason': 'malformed'},
        ]


class TestIntegrateResponse:
    def test_round_trip_preserves_issues(self):
        orig = IntegrateResponse(
            protocol_version=PROTOCOL_VERSION,
            stores={
                'store-a': IntegrateResult(
                    status='ok',
                    annotations=[
                        IntegratedAnnotation(
                            path='annotations/alice-xyz45678/inner-20260101-ab12/data',
                            ontology='inner-ear-structures',
                            ontology_version=1,
                        ),
                    ],
                    issues=[
                        IssueRecord(severity='warning', message='label gap'),
                    ],
                ),
            },
        )
        rt = _round_trip(orig, IntegrateResponse)
        result = rt.stores['store-a']
        assert result.status == 'ok'
        assert len(result.annotations) == 1
        assert result.annotations[0].ontology == 'inner-ear-structures'
        assert result.annotations[0].path.startswith('annotations/alice-')
        assert len(result.issues) == 1
        assert result.issues[0].severity == 'warning'


class TestChecksumEntry:
    def test_round_trip(self):
        orig = ChecksumEntry(path='alpha/segmentation.seg.nrrd', sha256='ab' * 32)
        rt = _round_trip(orig, ChecksumEntry)
        assert rt == orig

    @pytest.mark.parametrize(
        'sha256',
        [
            'AB' * 32,  # uppercase hex
            'ab' * 31,  # too short
            'ab' * 33,  # too long
            'sha256:' + 'ab' * 32,  # packed legacy prefix
            'g' * 64,  # non-hex characters
            '',  # empty
            42,  # not a string
        ],
    )
    def test_rejects_non_64_lowercase_hex(self, sha256):
        with pytest.raises(ValueError, match='64 lowercase hex'):
            ChecksumEntry.from_dict({'path': 'a/b.nrrd', 'sha256': sha256})

    def test_rejects_empty_path(self):
        with pytest.raises(ValueError, match='non-empty'):
            ChecksumEntry.from_dict({'path': '', 'sha256': 'ab' * 32})

    def test_missing_field_raises_key_error(self):
        with pytest.raises(KeyError):
            ChecksumEntry.from_dict({'path': 'a/b.nrrd'})


class TestIntegrateRequest:
    def test_round_trip(self):
        orig = IntegrateRequest(
            staging_dir='/tmp/vxhb-staging-abc',
            annotator_id='alice',
            machine_id='machine-xyz',
            nano_id='deadbeef',
            checksums=[
                ChecksumEntry(path='alpha/segmentation.seg.nrrd', sha256='ab' * 32),
            ],
            expected_ontology=['inner-ear-structures'],
            unconstrained=False,
            force=True,
        )
        rt = _round_trip(orig, IntegrateRequest)
        assert rt == orig

    def test_defaults(self):
        req = IntegrateRequest.from_dict(
            {
                'staging_dir': '/tmp/vxhb-staging-abc',
                'annotator_id': 'alice',
                'machine_id': 'machine-xyz',
                'nano_id': 'deadbeef',
            }
        )
        assert req.checksums == []
        assert req.expected_ontology == []
        assert req.unconstrained is False
        assert req.force is False

    def test_missing_required_field_raises_key_error(self):
        with pytest.raises(KeyError):
            IntegrateRequest.from_dict({'staging_dir': '/tmp/x'})

    def test_bad_checksum_entry_propagates(self):
        with pytest.raises(ValueError, match='64 lowercase hex'):
            IntegrateRequest.from_dict(
                {
                    'staging_dir': '/tmp/vxhb-staging-abc',
                    'annotator_id': 'alice',
                    'machine_id': 'machine-xyz',
                    'nano_id': 'deadbeef',
                    'checksums': [{'path': 'a/b.nrrd', 'sha256': 'nope'}],
                }
            )


class TestServerError:
    def test_round_trip(self):
        orig = ServerError(
            protocol_version=PROTOCOL_VERSION,
            error=True,
            code='validation_failed',
            message='Shape mismatch',
        )
        rt = _round_trip(orig, ServerError)
        assert rt.error is True
        assert rt.code == 'validation_failed'


class TestPrepareRequest:
    def test_optional_fields_serialize_as_none(self):
        req = PrepareRequest(store_name='store-a')
        d = json.loads(serialize(req))
        assert d['store_name'] == 'store-a'
        assert d['include_existing_annotations'] is None
        assert d['compress'] is False
        assert d['annotator_id'] is None

    def test_round_trip(self):
        orig = PrepareRequest(
            store_name='store-a',
            include_existing_annotations=['annotations/alice-xyz45678/inst'],
            compress=True,
            annotator_id='alice',
        )
        rt = _round_trip(orig, PrepareRequest)
        assert rt == orig

    def test_no_client_controllable_staging_dir(self):
        """The staging path is server-authoritative: the request model
        must not grow a staging_dir field again."""
        assert 'staging_dir' not in json.loads(serialize(PrepareRequest('s')))

    def test_missing_store_name_raises_key_error(self):
        with pytest.raises(KeyError):
            PrepareRequest.from_dict({})


class TestCleanupAndGc:
    def test_cleanup_round_trip(self):
        orig = CleanupResponse(protocol_version=PROTOCOL_VERSION, status='ok')
        rt = _round_trip(orig, CleanupResponse)
        assert rt.status == 'ok'

    def test_gc_round_trip(self):
        orig = GcResponse(
            protocol_version=PROTOCOL_VERSION,
            removed=['/tmp/dt-push-abc'],
            count=1,
        )
        rt = _round_trip(orig, GcResponse)
        assert rt.count == 1
        assert rt.removed == ['/tmp/dt-push-abc']


class TestProtocolVersion:
    def test_is_integer(self):
        assert isinstance(PROTOCOL_VERSION, int)

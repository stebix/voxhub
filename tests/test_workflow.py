"""End-to-end workflow tests validating the contract between packages.

Each test exercises the dry-run pipeline: zarr setup → stage (simulate pull)
→ write annotations (simulate Slicer) → preflight validation (client-side)
→ integrate (server-side) → provenance → metadata consistency.

SSH/rsync transport is bypassed — server functions are called directly.
"""

import json

import numpy as np
import pytest
import zarr
from _workflow_helpers import (
    ORIGIN_LPS,
    SHAPE,
    SPACE_DIRECTIONS,
    create_zarr_store,
    write_mrk_json,
    write_seg_nrrd,
)

from voxhub_client.manifest import read_manifest, update_manifest_status, write_manifest
from voxhub_core.integrate import integrate
from voxhub_core.server.provenance import record_provenance
from voxhub_core.staging import extract_spatial_metadata, stage
from voxhub_schema import (
    PROTOCOL_VERSION,
    RemoteManifest,
    RemoteManifestEntry,
    load_ontology,
    validate_lmk_preflight,
    validate_seg_preflight,
)

# -- Fixtures ----------------------------------------------------------------


@pytest.fixture
def zarr_root(tmp_path):
    """Create a zarr root with one store."""
    root = tmp_path / 'zarr_root'
    root.mkdir()
    create_zarr_store(root / 'scan-001.zarr')
    return root


@pytest.fixture
def staging_dir(tmp_path):
    """Empty staging directory for staging / annotation."""
    d = tmp_path / 'staging'
    d.mkdir()
    return d


@pytest.fixture
def inner_ear_ontology():
    return load_ontology('inner-ear-structures')


@pytest.fixture
def landmark_ontology():
    return load_ontology('inner-ear-landmarks')


@pytest.fixture
def unconstrained_ontology():
    return load_ontology('unconstrained')


def _manifest_entry_from_stage(stage_meta: dict) -> RemoteManifestEntry:
    """Build a RemoteManifestEntry from stage() output for one store."""
    return RemoteManifestEntry(
        status='pulled',
        raw_checksum=stage_meta['raw_checksum'],
        shape=stage_meta['shape'],
        spacing_mm=stage_meta['spacing_mm'],
        origin_lps=stage_meta['origin_lps'],
        space_directions=stage_meta['space_directions'],
        expected_ontologies=['inner-ear-structures'],
    )


def _write_valid_seg(store_dir, ontology):
    """Write a seg.nrrd that conforms to inner-ear-structures ontology."""
    lm = np.zeros(SHAPE, dtype=np.int16)
    lm[0, 0, 0] = 1
    lm[1, 1, 1] = 2
    lm[2, 2, 2] = 3
    segments = [
        {'name': 'cochlea', 'label_value': 1},
        {'name': 'vestibule', 'label_value': 2},
        {'name': 'semicircular_canals', 'label_value': 3},
    ]
    return write_seg_nrrd(store_dir / 'segmentation.seg.nrrd', lm, segments)


def _write_valid_lmk(store_dir):
    """Write a mrk.json that conforms to inner-ear-landmarks ontology."""
    pts = [[-1.0, -2.0, -3.0], [-2.0, -3.0, -4.0], [-3.0, -4.0, -5.0]]
    labels = ['round_window', 'oval_window', 'cochlear_apex']
    return write_mrk_json(store_dir / 'landmarks.mrk.json', pts, labels, 'LPS')


# ===================================================================
# SEGMENTATION ROUND TRIP
# ===================================================================


class TestSegmentationRoundTrip:
    """zarr → stage → annotate → preflight → integrate → verify."""

    def test_full_cycle(self, zarr_root, staging_dir, inner_ear_ontology):
        # 1. Stage (simulate pull).
        meta = stage(zarr_root, staging_dir, force=True)
        assert 'scan-001' in meta
        entry = _manifest_entry_from_stage(meta['scan-001'])

        # 2. Write annotation (simulate Slicer).
        store_dir = staging_dir / 'scan-001'
        seg_path = _write_valid_seg(store_dir, inner_ear_ontology)

        # 3. Preflight validation (client-side).
        issues = validate_seg_preflight(seg_path, entry, inner_ear_ontology)
        errors = [i for i in issues if i.severity == 'error']
        assert errors == [], f'Unexpected preflight errors: {errors}'

        # 4. Integrate (server-side).
        result = integrate(
            staging_dir,
            zarr_root,
            annotator_id='alice',
            nano_id='abcd1234',
            ontology=inner_ear_ontology,
        )
        assert all(
            i.severity != 'error' for i in result.get('scan-001', [])
        )

        # 5. Verify annotation in zarr.
        root = zarr.open_group(zarr_root / 'scan-001.zarr', mode='r')
        ann_group = root['annotations']
        # Should have annotator-scoped path.
        assert 'alice-abcd1234' in list(ann_group.group_keys())

    def test_preflight_rejects_wrong_ontology_labels(
        self, zarr_root, staging_dir, inner_ear_ontology
    ):
        meta = stage(zarr_root, staging_dir, force=True)
        entry = _manifest_entry_from_stage(meta['scan-001'])

        # Write seg with labels NOT in the ontology.
        store_dir = staging_dir / 'scan-001'
        lm = np.zeros(SHAPE, dtype=np.int16)
        lm[0, 0, 0] = 1
        segments = [{'name': 'alien_structure', 'label_value': 1}]
        seg_path = write_seg_nrrd(store_dir / 'bad.seg.nrrd', lm, segments)

        issues = validate_seg_preflight(seg_path, entry, inner_ear_ontology)
        errors = [i for i in issues if i.severity == 'error']
        assert len(errors) > 0
        error_text = ' '.join(i.message.lower() for i in errors)
        assert 'vestibule' in error_text or 'not defined' in error_text

    def test_integration_blocked_by_shape_mismatch(self, zarr_root, tmp_path):
        staging = tmp_path / 'staging_bad'
        staging.mkdir()
        store_dir = staging / 'scan-001'
        store_dir.mkdir()

        # Write seg with wrong shape.
        wrong_shape = (5, 5, 5)
        lm = np.zeros(wrong_shape, dtype=np.int16)
        write_seg_nrrd(store_dir / 'segmentation.seg.nrrd', lm, [])

        with pytest.raises(RuntimeError, match='Validation errors'):
            integrate(
                staging,
                zarr_root,
                annotator_id='alice',
                nano_id='abcd1234',
            )


# ===================================================================
# LANDMARK ROUND TRIP
# ===================================================================


class TestLandmarkRoundTrip:
    """zarr → stage → annotate landmarks → preflight → integrate → verify."""

    def test_full_cycle(self, zarr_root, staging_dir, landmark_ontology):
        meta = stage(zarr_root, staging_dir, force=True)
        entry = _manifest_entry_from_stage(meta['scan-001'])
        entry = RemoteManifestEntry(
            status=entry.status,
            raw_checksum=entry.raw_checksum,
            shape=entry.shape,
            spacing_mm=entry.spacing_mm,
            origin_lps=entry.origin_lps,
            space_directions=entry.space_directions,
            expected_ontologies=['inner-ear-landmarks'],
        )

        store_dir = staging_dir / 'scan-001'
        lmk_path = _write_valid_lmk(store_dir)

        issues = validate_lmk_preflight(lmk_path, entry, landmark_ontology)
        errors = [i for i in issues if i.severity == 'error']
        assert errors == [], f'Unexpected preflight errors: {errors}'

        result = integrate(
            staging_dir,
            zarr_root,
            annotator_id='bob',
            nano_id='efgh5678',
            ontology=landmark_ontology,
        )
        assert all(
            i.severity != 'error' for i in result.get('scan-001', [])
        )

        root = zarr.open_group(zarr_root / 'scan-001.zarr', mode='r')
        assert 'bob-efgh5678' in list(root['annotations'].group_keys())

    def test_ras_landmarks_stored_as_lps(self, zarr_root, staging_dir):
        stage(zarr_root, staging_dir, force=True)
        store_dir = staging_dir / 'scan-001'

        # Write landmarks in RAS.
        pts = [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [7.0, 8.0, 9.0]]
        labels = ['round_window', 'oval_window', 'cochlear_apex']
        write_mrk_json(store_dir / 'landmarks.mrk.json', pts, labels, 'RAS')

        integrate(
            staging_dir,
            zarr_root,
            annotator_id='carol',
            nano_id='ijkl9012',
        )

        root = zarr.open_group(zarr_root / 'scan-001.zarr', mode='r')
        ann_group = root['annotations']['carol-ijkl9012']
        # Find the landmark array.
        for sub_key in ann_group.group_keys():
            sub = ann_group[sub_key]
            if 'data' in sub:
                arr = sub['data']
                stored_pts = arr[:]
                attrs = dict(arr.attrs)
                break

        # RAS→LPS negates x and y.
        assert attrs['coordinate_system'] == 'LPS'
        np.testing.assert_allclose(stored_pts[0, 0], -1.0)
        np.testing.assert_allclose(stored_pts[0, 1], -2.0)
        np.testing.assert_allclose(stored_pts[0, 2], 3.0)


# ===================================================================
# SPATIAL METADATA PIPELINE INTEGRITY
# ===================================================================


class TestSpatialMetadataIntegrity:
    """DICOM attrs → zarr → staging → manifest — no drift."""

    def test_metadata_chain_is_consistent(self, zarr_root, staging_dir):
        # 1. Read attrs from zarr, compute spatial metadata.
        root = zarr.open_group(zarr_root / 'scan-001.zarr', mode='r')
        attrs = dict(root['raw']['full'].attrs)
        origin, dirs, spacing = extract_spatial_metadata(attrs)

        # 2. Stage and get metadata.
        meta = stage(zarr_root, staging_dir, force=True)
        stage_info = meta['scan-001']

        # 3. Verify staging output matches direct extraction.
        np.testing.assert_allclose(stage_info['origin_lps'], origin.tolist())
        np.testing.assert_allclose(
            stage_info['space_directions'], dirs.tolist()
        )
        np.testing.assert_allclose(stage_info['spacing_mm'], spacing)
        assert stage_info['shape'] == list(root['raw']['full'].shape)

    def test_corrupted_origin_caught_by_preflight(
        self, zarr_root, staging_dir, inner_ear_ontology
    ):
        meta = stage(zarr_root, staging_dir, force=True)
        entry = _manifest_entry_from_stage(meta['scan-001'])

        store_dir = staging_dir / 'scan-001'
        lm = np.zeros(SHAPE, dtype=np.int16)
        # Write seg with wrong origin (simulates resampled annotation).
        seg_path = write_seg_nrrd(
            store_dir / 'bad_origin.seg.nrrd',
            lm,
            [],
            origin=[99.0, 99.0, 99.0],
        )

        issues = validate_seg_preflight(
            seg_path, entry, inner_ear_ontology
        )
        errors = [i for i in issues if i.severity == 'error']
        assert any('origin' in e.message.lower() for e in errors)


# ===================================================================
# PROVENANCE COMPLETENESS
# ===================================================================


class TestProvenanceCompleteness:
    """After integration + provenance recording, verify metadata."""

    def _integrate_and_record(self, zarr_root, staging_dir, ontology):
        """Run the full integrate → provenance pipeline, return annotation path."""
        stage(zarr_root, staging_dir, force=True)
        store_dir = staging_dir / 'scan-001'
        _write_valid_seg(store_dir, ontology)

        integrate(
            staging_dir,
            zarr_root,
            annotator_id='alice',
            nano_id='abcd1234',
            ontology=ontology,
        )

        # Discover the annotation path that integrate() created.
        root = zarr.open_group(zarr_root / 'scan-001.zarr', mode='r')
        ann = root['annotations']['alice-abcd1234']
        instance_key = next(iter(ann.group_keys()))
        annotation_path = f'annotations/alice-abcd1234/{instance_key}/data'

        # Record provenance (server wrapper responsibility).
        record_provenance(
            zarr_root,
            'scan-001',
            annotation_path,
            annotator_id='alice',
            machine_id='ff' * 8,
            nano_id='abcd1234',
            pull_session_id='dt-pull-test',
            ontology=ontology.name,
            ontology_version=ontology.version,
            source_nrrd_checksum='sha256:test123',
            source_file='segmentation.seg.nrrd',
        )

        return annotation_path

    def test_zarr_attrs_contain_all_provenance_fields(
        self, zarr_root, staging_dir, inner_ear_ontology
    ):
        annotation_path = self._integrate_and_record(
            zarr_root, staging_dir, inner_ear_ontology
        )

        root = zarr.open_group(zarr_root / 'scan-001.zarr', mode='r')
        parts = annotation_path.strip('/').split('/')
        node = root
        for part in parts:
            node = node[part]
        attrs = dict(node.attrs)

        required_fields = {
            'annotator_id',
            'machine_id',
            'nano_id',
            'pull_session_id',
            'ontology',
            'ontology_version',
            'integrated_at',
            'source_nrrd_checksum',
            'source_file',
        }
        missing = required_fields - set(attrs.keys())
        assert missing == set(), f'Missing provenance fields: {missing}'
        assert attrs['annotator_id'] == 'alice'
        assert attrs['ontology'] == 'inner-ear-structures'
        assert attrs['ontology_version'] == 1
        assert attrs['pull_session_id'] == 'dt-pull-test'

    def test_provenance_jsonl_contains_matching_entry(
        self, zarr_root, staging_dir, inner_ear_ontology
    ):
        annotation_path = self._integrate_and_record(
            zarr_root, staging_dir, inner_ear_ontology
        )

        jsonl_path = zarr_root / '.meta' / 'provenance.jsonl'
        assert jsonl_path.exists()

        lines = jsonl_path.read_text().strip().split('\n')
        assert len(lines) >= 1
        record = json.loads(lines[-1])

        assert record['event'] == 'push'
        assert record['store'] == 'scan-001'
        assert record['annotator_id'] == 'alice'
        assert record['ontology'] == 'inner-ear-structures'
        assert record['annotation_path'] == annotation_path


# ===================================================================
# MULTI-ANNOTATOR ISOLATION
# ===================================================================


class TestMultiAnnotatorIsolation:
    """Two annotators push to the same store without conflicts."""

    def test_separate_annotation_paths(self, zarr_root, tmp_path):
        # Annotator 1.
        staging_1 = tmp_path / 'staging_alice'
        staging_1.mkdir()
        stage(zarr_root, staging_1, force=True)
        _write_valid_seg(staging_1 / 'scan-001', load_ontology('inner-ear-structures'))
        integrate(
            staging_1,
            zarr_root,
            annotator_id='alice',
            nano_id='aaaa1111',
            ontology=load_ontology('inner-ear-structures'),
        )

        # Annotator 2.
        staging_2 = tmp_path / 'staging_bob'
        staging_2.mkdir()
        stage(zarr_root, staging_2, force=True)
        _write_valid_seg(staging_2 / 'scan-001', load_ontology('inner-ear-structures'))
        integrate(
            staging_2,
            zarr_root,
            annotator_id='bob',
            nano_id='bbbb2222',
            ontology=load_ontology('inner-ear-structures'),
        )

        root = zarr.open_group(zarr_root / 'scan-001.zarr', mode='r')
        annotator_dirs = set(root['annotations'].group_keys())
        assert 'alice-aaaa1111' in annotator_dirs
        assert 'bob-bbbb2222' in annotator_dirs

    def test_independent_provenance_per_annotator(self, zarr_root, tmp_path):
        ontology = load_ontology('inner-ear-structures')

        for name, nano in [('alice', 'aaaa1111'), ('bob', 'bbbb2222')]:
            staging = tmp_path / f'staging_{name}'
            staging.mkdir()
            stage(zarr_root, staging, force=True)
            _write_valid_seg(staging / 'scan-001', ontology)
            integrate(
                staging,
                zarr_root,
                annotator_id=name,
                nano_id=nano,
                ontology=ontology,
            )

            # Find annotation path.
            root = zarr.open_group(zarr_root / 'scan-001.zarr', mode='r')
            ann = root['annotations'][f'{name}-{nano}']
            instance_key = next(iter(ann.group_keys()))
            ann_path = f'annotations/{name}-{nano}/{instance_key}/data'

            record_provenance(
                zarr_root,
                'scan-001',
                ann_path,
                annotator_id=name,
                machine_id='ff' * 8,
                nano_id=nano,
                pull_session_id=f'dt-pull-{name}',
                ontology=ontology.name,
                ontology_version=ontology.version,
                source_nrrd_checksum='sha256:test',
                source_file='segmentation.seg.nrrd',
            )

        jsonl = (zarr_root / '.meta' / 'provenance.jsonl').read_text().strip()
        records = [json.loads(line) for line in jsonl.split('\n')]
        annotators = {r['annotator_id'] for r in records}
        assert annotators == {'alice', 'bob'}


# ===================================================================
# ONTOLOGY ENFORCEMENT E2E
# ===================================================================


class TestOntologyEnforcementE2E:
    """Ontology constraints enforced across the full pipeline."""

    def test_constrained_missing_required_label(
        self, zarr_root, staging_dir, inner_ear_ontology
    ):
        """Constrained ontology requires cochlea+vestibule+semicircular_canals."""
        meta = stage(zarr_root, staging_dir, force=True)
        entry = _manifest_entry_from_stage(meta['scan-001'])

        store_dir = staging_dir / 'scan-001'
        lm = np.zeros(SHAPE, dtype=np.int16)
        lm[0, 0, 0] = 1
        # Only cochlea — missing vestibule and semicircular_canals.
        segments = [{'name': 'cochlea', 'label_value': 1}]
        seg_path = write_seg_nrrd(
            store_dir / 'incomplete.seg.nrrd', lm, segments
        )

        issues = validate_seg_preflight(
            seg_path, entry, inner_ear_ontology
        )
        errors = [i for i in issues if i.severity == 'error']
        assert len(errors) > 0
        error_text = ' '.join(i.message for i in errors)
        assert 'vestibule' in error_text or 'semicircular' in error_text

    def test_unconstrained_non_sequential_labels(
        self, zarr_root, staging_dir, unconstrained_ontology
    ):
        meta = stage(zarr_root, staging_dir, force=True)
        entry = _manifest_entry_from_stage(meta['scan-001'])
        entry = RemoteManifestEntry(
            status=entry.status,
            raw_checksum=entry.raw_checksum,
            shape=entry.shape,
            spacing_mm=entry.spacing_mm,
            origin_lps=entry.origin_lps,
            space_directions=entry.space_directions,
            expected_ontologies=['unconstrained'],
        )

        store_dir = staging_dir / 'scan-001'
        lm = np.zeros(SHAPE, dtype=np.int16)
        lm[0, 0, 0] = 1
        lm[1, 1, 1] = 5  # Non-sequential: jumps from 1 to 5.
        segments = [
            {'name': 'a', 'label_value': 1},
            {'name': 'b', 'label_value': 5},
        ]
        seg_path = write_seg_nrrd(
            store_dir / 'non_seq.seg.nrrd', lm, segments
        )

        issues = validate_seg_preflight(
            seg_path, entry, unconstrained_ontology
        )
        errors = [i for i in issues if i.severity == 'error']
        assert len(errors) > 0
        assert any('sequential' in e.message.lower() for e in errors)


# ===================================================================
# MANIFEST WORKFLOW
# ===================================================================


class TestManifestWorkflow:
    """Manifest lifecycle: stage → write → read → update."""

    def test_manifest_round_trip_through_staging(self, zarr_root, staging_dir):
        meta = stage(zarr_root, staging_dir, force=True)
        info = meta['scan-001']

        manifest = RemoteManifest(
            server_host='alice@server',
            server_zarr_root='/data/zarr',
            protocol_version=PROTOCOL_VERSION,
            pull_session_id='dt-pull-test',
            pulled_at='2026-01-01T00:00:00+00:00',
            stores={
                'scan-001': _manifest_entry_from_stage(info),
            },
        )
        write_manifest(staging_dir, manifest)
        rt = read_manifest(staging_dir)

        assert rt.protocol_version == PROTOCOL_VERSION
        assert rt.pull_session_id == 'dt-pull-test'
        s = rt.stores['scan-001']
        np.testing.assert_allclose(s.origin_lps, ORIGIN_LPS)
        np.testing.assert_allclose(s.space_directions, SPACE_DIRECTIONS)
        assert s.shape == list(SHAPE)
        assert s.raw_checksum.startswith('sha256:')

    def test_manifest_status_lifecycle(self, zarr_root, staging_dir):
        meta = stage(zarr_root, staging_dir, force=True)
        info = meta['scan-001']

        manifest = RemoteManifest(
            server_host='alice@server',
            server_zarr_root='/data/zarr',
            protocol_version=PROTOCOL_VERSION,
            pull_session_id='dt-pull-test',
            pulled_at='2026-01-01T00:00:00+00:00',
            stores={
                'scan-001': _manifest_entry_from_stage(info),
            },
        )
        write_manifest(staging_dir, manifest)

        # After pull: status is 'pulled'.
        assert read_manifest(staging_dir).stores['scan-001'].status == 'pulled'

        # After push+integration: update to 'integrated'.
        update_manifest_status(staging_dir, 'scan-001', 'integrated')
        assert (
            read_manifest(staging_dir).stores['scan-001'].status == 'integrated'
        )

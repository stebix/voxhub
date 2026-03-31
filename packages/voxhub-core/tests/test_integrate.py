"""Tests for annotation integration into zarr stores.

Covers validation, ontology enforcement, annotation writing, and
the annotator-scoped path convention.  Uses synthetic zarr stores
and programmatically built annotation files.
"""


import numpy as np
import pytest
import zarr
from _core_helpers import (
    ORIGIN_LPS,
    SHAPE,
    SPACE_DIRECTIONS,
    SPACING_MM,
    build_wip_dir,
    create_zarr_store,
)

from voxhub_core.integrate import (
    find_annotation_files,
    integrate,
    validate_landmarks,
    validate_segmentation,
    write_landmarks_to_zarr,
    write_segmentation_to_zarr,
)
from voxhub_core.slicer import LandmarkData, Segment, SegmentationData
from voxhub_schema.models import IssueRecord


def _errors(issues: list[IssueRecord]) -> list[IssueRecord]:
    return [i for i in issues if i.severity == 'error']


def _manifest_entry() -> dict[str, object]:
    """Stage-style metadata dict matching canonical test geometry."""
    return {
        'shape': list(SHAPE),
        'origin_lps': ORIGIN_LPS,
        'space_directions': SPACE_DIRECTIONS,
        'spacing_mm': SPACING_MM,
    }


def _valid_seg() -> SegmentationData:
    lm = np.zeros(SHAPE, dtype=np.int16)
    lm[0, 0, 0] = 1
    lm[1, 1, 1] = 2
    lm[2, 2, 2] = 3
    return SegmentationData(
        label_map=lm,
        segments=[
            Segment(id='s0', name='cochlea', label_value=1, color=(1, 0, 0)),
            Segment(id='s1', name='vestibule', label_value=2, color=(0, 1, 0)),
            Segment(
                id='s2', name='semicircular_canals', label_value=3, color=(0, 0, 1)
            ),
        ],
        space_origin=np.array(ORIGIN_LPS),
        space_directions=np.array(SPACE_DIRECTIONS),
    )


def _valid_lmk_lps() -> LandmarkData:
    return LandmarkData(
        points=np.array([[-4.0, -5.0, -6.0], [-3.0, -4.0, -5.0], [-2.0, -3.0, -4.0]]),
        labels=['round_window', 'oval_window', 'cochlear_apex'],
        coordinate_system='LPS',
    )


# ===================================================================
# validate_segmentation
# ===================================================================


class TestValidateSegmentation:
    def test_valid_seg_no_errors(self):
        issues = validate_segmentation(_valid_seg(), _manifest_entry())
        assert _errors(issues) == []

    def test_shape_mismatch(self):
        seg = _valid_seg()
        entry = _manifest_entry()
        entry['shape'] = [8, 12, 14]
        issues = validate_segmentation(seg, entry)
        assert any('shape' in e.message.lower() for e in _errors(issues))

    def test_origin_mismatch(self):
        seg = _valid_seg()
        entry = _manifest_entry()
        entry['origin_lps'] = [0.0, 0.0, 0.0]
        issues = validate_segmentation(seg, entry)
        assert any('origin' in e.message.lower() for e in _errors(issues))

    def test_directions_mismatch(self):
        seg = _valid_seg()
        entry = _manifest_entry()
        entry['space_directions'] = [[1.0, 0, 0], [0, 1.0, 0], [0, 0, 1.0]]
        issues = validate_segmentation(seg, entry)
        assert any('direction' in e.message.lower() for e in _errors(issues))

    def test_negative_labels(self):
        seg = _valid_seg()
        seg.label_map[0, 0, 0] = -1
        issues = validate_segmentation(seg, _manifest_entry())
        assert any('negative' in e.message.lower() for e in _errors(issues))


# ===================================================================
# validate_landmarks
# ===================================================================


class TestValidateLandmarks:
    def test_valid_lps_no_errors(self, landmark_ontology):
        issues = validate_landmarks(
            _valid_lmk_lps(), _manifest_entry(), ontology=landmark_ontology
        )
        assert _errors(issues) == []

    def test_unknown_coordinate_system(self):
        lmk = LandmarkData(
            points=np.array([[0, 0, 0]]),
            labels=['pt'],
            coordinate_system='XYZ',
        )
        issues = validate_landmarks(lmk, _manifest_entry())
        assert any('coordinate' in e.message.lower() for e in _errors(issues))

    def test_duplicate_labels(self):
        lmk = LandmarkData(
            points=np.array([[0, 0, 0], [1, 1, 1]]),
            labels=['same', 'same'],
            coordinate_system='LPS',
        )
        issues = validate_landmarks(lmk, _manifest_entry())
        assert any('duplicate' in e.message.lower() for e in _errors(issues))

    def test_missing_ontology_point(self, landmark_ontology):
        lmk = LandmarkData(
            points=np.array([[0, 0, 0], [1, 1, 1]]),
            labels=['round_window', 'oval_window'],  # missing cochlear_apex
            coordinate_system='LPS',
        )
        issues = validate_landmarks(
            lmk, _manifest_entry(), ontology=landmark_ontology
        )
        assert any('cochlear_apex' in e.message for e in _errors(issues))

    def test_extra_ontology_point(self, landmark_ontology):
        lmk = LandmarkData(
            points=np.array([[0, 0, 0], [1, 1, 1], [2, 2, 2], [3, 3, 3]]),
            labels=['round_window', 'oval_window', 'cochlear_apex', 'bonus'],
            coordinate_system='LPS',
        )
        issues = validate_landmarks(
            lmk, _manifest_entry(), ontology=landmark_ontology
        )
        assert any('bonus' in e.message for e in _errors(issues))

    def test_no_ontology_skips_point_check(self):
        lmk = LandmarkData(
            points=np.array([[0, 0, 0]]),
            labels=['anything_goes'],
            coordinate_system='LPS',
        )
        issues = validate_landmarks(lmk, _manifest_entry(), ontology=None)
        ontology_errors = [
            e for e in _errors(issues) if 'ontology' in e.message.lower()
        ]
        assert ontology_errors == []


# ===================================================================
# write_segmentation_to_zarr
# ===================================================================


class TestWriteSegmentationToZarr:
    def test_writes_to_annotator_scoped_path(self, tmp_path, inner_ear_ontology):
        store = create_zarr_store(tmp_path / 'store.zarr')
        seg = _valid_seg()
        group_path = 'annotations/alice-abc123/inner-ear-structures-20260101-xyzw/data'

        write_segmentation_to_zarr(
            store, seg, group_path, ontology=inner_ear_ontology
        )

        root = zarr.open_group(store, mode='r')
        arr = root['annotations']['alice-abc123'][
            'inner-ear-structures-20260101-xyzw'
        ]['data']
        assert arr.shape == SHAPE
        assert int(arr[0, 0, 0]) == 1
        assert int(arr[1, 1, 1]) == 2

    def test_records_ontology_in_attrs(self, tmp_path, inner_ear_ontology):
        store = create_zarr_store(tmp_path / 'store.zarr')
        seg = _valid_seg()
        group_path = 'annotations/alice-abc/seg-20260101-xyzw/data'

        write_segmentation_to_zarr(
            store, seg, group_path, ontology=inner_ear_ontology
        )

        root = zarr.open_group(store, mode='r')
        arr = root['annotations']['alice-abc']['seg-20260101-xyzw']['data']
        a = dict(arr.attrs)
        assert a['ontology'] == 'inner-ear-structures'
        assert a['ontology_version'] == 1
        assert 'integrated_at' in a
        assert 'segments' in a

    def test_existing_path_raises_without_force(self, tmp_path, inner_ear_ontology):
        store = create_zarr_store(tmp_path / 'store.zarr')
        seg = _valid_seg()
        group_path = 'annotations/alice-abc/seg/data'
        write_segmentation_to_zarr(store, seg, group_path, ontology=inner_ear_ontology)
        with pytest.raises(FileExistsError):
            write_segmentation_to_zarr(
                store, seg, group_path, ontology=inner_ear_ontology
            )

    def test_force_overwrites(self, tmp_path, inner_ear_ontology):
        store = create_zarr_store(tmp_path / 'store.zarr')
        seg = _valid_seg()
        group_path = 'annotations/alice-abc/seg/data'
        write_segmentation_to_zarr(store, seg, group_path, ontology=inner_ear_ontology)
        # Modify the seg and force-write.
        seg.label_map[5, 5, 5] = 1
        write_segmentation_to_zarr(
            store, seg, group_path, ontology=inner_ear_ontology, force=True
        )
        root = zarr.open_group(store, mode='r')
        arr = root['annotations']['alice-abc']['seg']['data']
        assert int(arr[5, 5, 5]) == 1

    def test_dtype_uint8_when_labels_small(self, tmp_path):
        store = create_zarr_store(tmp_path / 'store.zarr')
        seg = _valid_seg()  # max label = 3
        group_path = 'annotations/a/s/data'
        write_segmentation_to_zarr(store, seg, group_path)
        root = zarr.open_group(store, mode='r')
        arr = root['annotations']['a']['s']['data']
        assert arr.dtype == np.uint8


# ===================================================================
# write_landmarks_to_zarr
# ===================================================================


class TestWriteLandmarksToZarr:
    def test_writes_lps_points(self, tmp_path, landmark_ontology):
        store = create_zarr_store(tmp_path / 'store.zarr')
        lmk = _valid_lmk_lps()
        group_path = 'annotations/alice-abc/lmk-20260101-xyzw/data'

        write_landmarks_to_zarr(
            store, lmk, group_path, ontology=landmark_ontology
        )

        root = zarr.open_group(store, mode='r')
        arr = root['annotations']['alice-abc']['lmk-20260101-xyzw']['data']
        np.testing.assert_allclose(arr[:], lmk.points)

    def test_converts_ras_to_lps_on_write(self, tmp_path, landmark_ontology):
        store = create_zarr_store(tmp_path / 'store.zarr')
        ras_points = np.array([[4.0, 5.0, -6.0], [3.0, 4.0, -5.0], [2.0, 3.0, -4.0]])
        lmk = LandmarkData(
            points=ras_points,
            labels=['round_window', 'oval_window', 'cochlear_apex'],
            coordinate_system='RAS',
        )
        group_path = 'annotations/alice-abc/lmk/data'
        write_landmarks_to_zarr(store, lmk, group_path, ontology=landmark_ontology)

        root = zarr.open_group(store, mode='r')
        arr = root['annotations']['alice-abc']['lmk']['data']
        stored = arr[:]
        # RAS→LPS: negate x and y.
        expected = ras_points.copy()
        expected[:, 0] *= -1
        expected[:, 1] *= -1
        np.testing.assert_allclose(stored, expected)
        assert dict(arr.attrs)['coordinate_system'] == 'LPS'
        assert dict(arr.attrs)['original_coordinate_system'] == 'RAS'

    def test_records_ontology_in_attrs(self, tmp_path, landmark_ontology):
        store = create_zarr_store(tmp_path / 'store.zarr')
        lmk = _valid_lmk_lps()
        group_path = 'annotations/a/l/data'
        write_landmarks_to_zarr(store, lmk, group_path, ontology=landmark_ontology)

        root = zarr.open_group(store, mode='r')
        a = dict(root['annotations']['a']['l']['data'].attrs)
        assert a['ontology'] == 'inner-ear-landmarks'
        assert a['ontology_version'] == 1
        assert a['labels'] == ['round_window', 'oval_window', 'cochlear_apex']


# ===================================================================
# find_annotation_files
# ===================================================================


class TestFindAnnotationFiles:
    def test_finds_seg_and_lmk(self, tmp_path):
        (tmp_path / 'seg.seg.nrrd').touch()
        (tmp_path / 'lmk.mrk.json').touch()
        seg, lmk = find_annotation_files(tmp_path)
        assert seg is not None
        assert seg.name == 'seg.seg.nrrd'
        assert lmk is not None
        assert lmk.name == 'lmk.mrk.json'

    def test_finds_none_when_empty(self, tmp_path):
        seg, lmk = find_annotation_files(tmp_path)
        assert seg is None
        assert lmk is None

    def test_finds_seg_only(self, tmp_path):
        (tmp_path / 'seg.seg.nrrd').touch()
        seg, lmk = find_annotation_files(tmp_path)
        assert seg is not None
        assert lmk is None


# ===================================================================
# integrate() — full local workflow
# ===================================================================


class TestIntegrate:
    def _setup(self, tmp_path, *, seg=True, lmk=False, ontology=None):
        """Set up zarr store + WIP directory for integration."""
        zarr_root = tmp_path / 'zarr'
        zarr_root.mkdir()
        create_zarr_store(zarr_root / 'mystore.zarr')

        wip = tmp_path / 'wip'
        seg_lm = None
        seg_segments = None
        lmk_pts = None
        lmk_labels = None

        if seg:
            seg_lm = np.zeros(SHAPE, dtype=np.int16)
            seg_lm[0, 0, 0] = 1
            seg_lm[1, 1, 1] = 2
            seg_lm[2, 2, 2] = 3
            seg_segments = [
                {'name': 'cochlea', 'label_value': 1},
                {'name': 'vestibule', 'label_value': 2},
                {'name': 'semicircular_canals', 'label_value': 3},
            ]

        if lmk:
            lmk_pts = [[-4.0, -5.0, -6.0], [-3.0, -4.0, -5.0], [-2.0, -3.0, -4.0]]
            lmk_labels = ['round_window', 'oval_window', 'cochlear_apex']

        build_wip_dir(
            wip,
            'mystore',
            seg_label_map=seg_lm,
            seg_segments=seg_segments,
            lmk_points=lmk_pts,
            lmk_labels=lmk_labels,
        )
        return zarr_root, wip

    def test_integrates_valid_segmentation(self, tmp_path, inner_ear_ontology):
        zarr_root, wip = self._setup(tmp_path)
        issues = integrate(
            wip,
            zarr_root,
            annotator_id='alice',
            nano_id='abc12345',
            ontology=inner_ear_ontology,
        )
        # No errors.
        for store_issues in issues.values():
            assert _errors(store_issues) == []

        # Annotation was written.
        root = zarr.open_group(zarr_root / 'mystore.zarr', mode='r')
        ann = root['annotations']
        # Should have alice-abc12345 group.
        assert 'alice-abc12345' in list(ann)

    def test_integrates_valid_landmarks(self, tmp_path, landmark_ontology):
        zarr_root, wip = self._setup(tmp_path, seg=False, lmk=True)
        issues = integrate(
            wip,
            zarr_root,
            annotator_id='alice',
            nano_id='abc12345',
            ontology=landmark_ontology,
        )
        for store_issues in issues.values():
            assert _errors(store_issues) == []

        root = zarr.open_group(zarr_root / 'mystore.zarr', mode='r')
        assert 'alice-abc12345' in list(root['annotations'])

    def test_validate_only_does_not_write(self, tmp_path, inner_ear_ontology):
        zarr_root, wip = self._setup(tmp_path)
        integrate(
            wip,
            zarr_root,
            annotator_id='alice',
            nano_id='abc12345',
            ontology=inner_ear_ontology,
            validate_only=True,
        )
        root = zarr.open_group(zarr_root / 'mystore.zarr', mode='r')
        assert 'annotations' not in list(root)

    def test_validation_errors_block_integration(self, tmp_path, inner_ear_ontology):
        """A seg with wrong shape should block integration."""
        zarr_root = tmp_path / 'zarr'
        zarr_root.mkdir()
        create_zarr_store(zarr_root / 'mystore.zarr')

        wip = tmp_path / 'wip'
        wrong_shape = (8, 12, 14)
        build_wip_dir(
            wip,
            'mystore',
            seg_label_map=np.zeros(wrong_shape, dtype=np.int16),
            seg_segments=[],
        )
        with pytest.raises(RuntimeError, match='Validation errors'):
            integrate(
                wip,
                zarr_root,
                annotator_id='alice',
                nano_id='abc12345',
                ontology=inner_ear_ontology,
            )

    def test_force_integrates_despite_warnings(self, tmp_path, inner_ear_ontology):
        """With force=True, stores without errors still get integrated
        even when other stores have errors."""
        zarr_root, wip = self._setup(tmp_path)
        integrate(
            wip,
            zarr_root,
            annotator_id='alice',
            nano_id='abc12345',
            ontology=inner_ear_ontology,
            force=True,
        )
        # Should still succeed.
        root = zarr.open_group(zarr_root / 'mystore.zarr', mode='r')
        assert 'annotations' in list(root)

    def test_multi_annotator_isolation(self, tmp_path, inner_ear_ontology):
        """Two annotators integrating to the same store get separate paths."""
        zarr_root, wip1 = self._setup(tmp_path)

        # First annotator.
        integrate(
            wip1,
            zarr_root,
            annotator_id='alice',
            nano_id='aaa11111',
            ontology=inner_ear_ontology,
        )

        # Second annotator with fresh WIP.
        wip2 = tmp_path / 'wip2'
        build_wip_dir(
            wip2,
            'mystore',
            seg_label_map=np.zeros(SHAPE, dtype=np.int16),
            seg_segments=[],
        )
        integrate(
            wip2,
            zarr_root,
            annotator_id='bob',
            nano_id='bbb22222',
            ontology=inner_ear_ontology,
            force=True,
        )

        root = zarr.open_group(zarr_root / 'mystore.zarr', mode='r')
        annotators = list(root['annotations'])
        assert 'alice-aaa11111' in annotators
        assert 'bob-bbb22222' in annotators

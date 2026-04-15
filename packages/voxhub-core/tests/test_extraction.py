"""Tests for :mod:`voxhub_core.extraction`."""

import hashlib

import numpy as np
import pytest
import zarr
from _core_helpers import (
    ORIGIN_LPS,
    SHAPE,
    SPACE_DIRECTIONS,
    create_zarr_store,
    populate_store_annotation,
)

from voxhub_core.extraction import (
    ExtractionError,
    extract_landmarks,
    extract_segmentation,
)
from voxhub_core.slicer import parse_mrk_json, parse_seg_nrrd

# -- Segmentation ------------------------------------------------------------


def _seed_segmentation(zarr_path, *, segments):
    """Populate a store with a 3D label map + segments attr."""
    # Use populate_store_annotation to set up the annotator-slug path + attrs.
    ann_path = populate_store_annotation(
        zarr_path,
        kind='segmentation',
        segments=segments,
    )
    # Overwrite the label map with deterministic non-zero content so we can
    # round-trip the label values through the .seg.nrrd.
    root = zarr.open_group(zarr_path, mode='r+')
    arr_path = f'{ann_path}/data'
    label_map = np.zeros(SHAPE, dtype=np.int16)
    label_map[0:3, 0:3, 0:3] = 1
    label_map[5:8, 5:8, 5:8] = 2
    del root[arr_path]
    arr = root.create_array(arr_path, data=label_map)
    arr.update_attributes(
        {
            'segments': segments,
            'coordinate_system': 'LPS',
            'integrated_at': '2026-01-01T00:00:00+00:00',
            'source_file': 'segmentation.seg.nrrd',
            'ontology': 'inner-ear-structures',
            'ontology_version': 1,
        }
    )
    return ann_path


_SAMPLE_SEGMENTS: list[dict] = [
    {
        'id': 'seg_0',
        'name': 'cochlea',
        'label_value': 1,
        'color': [1.0, 0.0, 0.0],
    },
    {
        'id': 'seg_1',
        'name': 'vestibule',
        'label_value': 2,
        'color': [0.0, 1.0, 0.0],
    },
]


class TestExtractSegmentation:
    def test_produces_parseable_seg_nrrd(self, tmp_path):
        zarr_path = tmp_path / 'store.zarr'
        create_zarr_store(zarr_path)
        ann_path = _seed_segmentation(zarr_path, segments=_SAMPLE_SEGMENTS)

        dest = tmp_path / 'out.seg.nrrd'
        extract_segmentation(zarr_path, f'{ann_path}/data', dest)

        parsed = parse_seg_nrrd(dest)
        assert parsed.label_map.shape == SHAPE
        assert set(np.unique(parsed.label_map).tolist()) >= {0, 1, 2}
        names = sorted(s.name for s in parsed.segments)
        assert names == ['cochlea', 'vestibule']
        # Spatial metadata round-trips.
        np.testing.assert_allclose(parsed.space_origin, ORIGIN_LPS)
        np.testing.assert_allclose(parsed.space_directions, SPACE_DIRECTIONS)

    def test_returns_matching_sha256(self, tmp_path):
        zarr_path = tmp_path / 'store.zarr'
        create_zarr_store(zarr_path)
        ann_path = _seed_segmentation(zarr_path, segments=_SAMPLE_SEGMENTS)

        dest = tmp_path / 'out.seg.nrrd'
        digest = extract_segmentation(zarr_path, f'{ann_path}/data', dest)

        expected = 'sha256:' + hashlib.sha256(dest.read_bytes()).hexdigest()
        assert digest == expected

    def test_missing_array_raises(self, tmp_path):
        zarr_path = tmp_path / 'store.zarr'
        create_zarr_store(zarr_path)
        dest = tmp_path / 'out.seg.nrrd'
        with pytest.raises(ExtractionError, match='not found'):
            extract_segmentation(
                zarr_path,
                'annotations/alice-xyz45678/missing/data',
                dest,
            )

    def test_non_integer_label_map_raises(self, tmp_path):
        zarr_path = tmp_path / 'store.zarr'
        create_zarr_store(zarr_path)
        ann_path = populate_store_annotation(
            zarr_path, kind='segmentation', segments=_SAMPLE_SEGMENTS
        )
        # Overwrite the array with a float dtype.
        root = zarr.open_group(zarr_path, mode='r+')
        del root[f'{ann_path}/data']
        arr = root.create_array(
            f'{ann_path}/data',
            data=np.zeros(SHAPE, dtype=np.float32),
        )
        arr.update_attributes({'segments': _SAMPLE_SEGMENTS})

        dest = tmp_path / 'out.seg.nrrd'
        with pytest.raises(ExtractionError, match='integer dtype'):
            extract_segmentation(zarr_path, f'{ann_path}/data', dest)

    def test_missing_segments_attr_raises(self, tmp_path):
        zarr_path = tmp_path / 'store.zarr'
        create_zarr_store(zarr_path)
        ann_path = populate_store_annotation(
            zarr_path, kind='segmentation', segments=None, labels=None
        )
        dest = tmp_path / 'out.seg.nrrd'
        with pytest.raises(ExtractionError, match="'segments' attribute"):
            extract_segmentation(zarr_path, f'{ann_path}/data', dest)

    def test_malformed_segments_entry_raises(self, tmp_path):
        zarr_path = tmp_path / 'store.zarr'
        create_zarr_store(zarr_path)
        bad_segments: list[dict] = [
            {'id': 'x', 'name': 'n', 'label_value': 1},  # missing 'color'
        ]
        ann_path = populate_store_annotation(
            zarr_path, kind='segmentation', segments=bad_segments
        )
        dest = tmp_path / 'out.seg.nrrd'
        with pytest.raises(ExtractionError, match='missing keys'):
            extract_segmentation(zarr_path, f'{ann_path}/data', dest)


# -- Landmarks ---------------------------------------------------------------


def _seed_landmarks(zarr_path, *, labels, points):
    ann_path = populate_store_annotation(
        zarr_path,
        kind='landmarks',
        labels=labels,
    )
    root = zarr.open_group(zarr_path, mode='r+')
    del root[f'{ann_path}/data']
    arr = root.create_array(f'{ann_path}/data', data=points)
    arr.update_attributes(
        {
            'labels': labels,
            'coordinate_system': 'LPS',
            'original_coordinate_system': 'RAS',
            'integrated_at': '2026-01-01T00:00:00+00:00',
            'source_file': 'landmarks.mrk.json',
            'ontology': 'inner-ear-landmarks',
            'ontology_version': 1,
        }
    )
    return ann_path


class TestExtractLandmarks:
    def test_produces_parseable_mrk_json(self, tmp_path):
        zarr_path = tmp_path / 'store.zarr'
        create_zarr_store(zarr_path)
        labels = ['apex', 'base', 'round-window']
        points = np.array(
            [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [7.0, 8.0, 9.0]],
            dtype=np.float64,
        )
        ann_path = _seed_landmarks(zarr_path, labels=labels, points=points)

        dest = tmp_path / 'out.mrk.json'
        extract_landmarks(zarr_path, f'{ann_path}/data', dest)

        parsed = parse_mrk_json(dest)
        assert parsed.labels == labels
        assert parsed.coordinate_system == 'LPS'
        np.testing.assert_allclose(parsed.points, points)

    def test_returns_matching_sha256(self, tmp_path):
        zarr_path = tmp_path / 'store.zarr'
        create_zarr_store(zarr_path)
        labels = ['p1', 'p2']
        points = np.array([[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]], dtype=np.float64)
        ann_path = _seed_landmarks(zarr_path, labels=labels, points=points)

        dest = tmp_path / 'out.mrk.json'
        digest = extract_landmarks(zarr_path, f'{ann_path}/data', dest)

        expected = 'sha256:' + hashlib.sha256(dest.read_bytes()).hexdigest()
        assert digest == expected

    def test_missing_array_raises(self, tmp_path):
        zarr_path = tmp_path / 'store.zarr'
        create_zarr_store(zarr_path)
        dest = tmp_path / 'out.mrk.json'
        with pytest.raises(ExtractionError, match='not found'):
            extract_landmarks(
                zarr_path,
                'annotations/alice-xyz45678/missing/data',
                dest,
            )

    def test_missing_labels_attr_raises(self, tmp_path):
        zarr_path = tmp_path / 'store.zarr'
        create_zarr_store(zarr_path)
        ann_path = populate_store_annotation(
            zarr_path, kind='landmarks', labels=None, segments=None
        )
        # Replace the array with a (N,3) shape to pass the shape check.
        root = zarr.open_group(zarr_path, mode='r+')
        del root[f'{ann_path}/data']
        root.create_array(
            f'{ann_path}/data',
            data=np.zeros((2, 3), dtype=np.float64),
        )
        dest = tmp_path / 'out.mrk.json'
        with pytest.raises(ExtractionError, match="'labels' attribute"):
            extract_landmarks(zarr_path, f'{ann_path}/data', dest)

    def test_shape_mismatch_raises(self, tmp_path):
        zarr_path = tmp_path / 'store.zarr'
        create_zarr_store(zarr_path)
        # Points as (2, 2) — invalid.
        ann_path = populate_store_annotation(
            zarr_path, kind='landmarks', labels=['a', 'b']
        )
        root = zarr.open_group(zarr_path, mode='r+')
        del root[f'{ann_path}/data']
        arr = root.create_array(
            f'{ann_path}/data',
            data=np.zeros((2, 2), dtype=np.float64),
        )
        arr.update_attributes({'labels': ['a', 'b']})

        dest = tmp_path / 'out.mrk.json'
        with pytest.raises(ExtractionError, match=r'shape \(N, 3\)'):
            extract_landmarks(zarr_path, f'{ann_path}/data', dest)

    def test_labels_points_count_mismatch_raises(self, tmp_path):
        zarr_path = tmp_path / 'store.zarr'
        create_zarr_store(zarr_path)
        # Two points, three labels.
        ann_path = populate_store_annotation(
            zarr_path, kind='landmarks', labels=['a', 'b', 'c']
        )
        root = zarr.open_group(zarr_path, mode='r+')
        del root[f'{ann_path}/data']
        arr = root.create_array(
            f'{ann_path}/data',
            data=np.zeros((2, 3), dtype=np.float64),
        )
        arr.update_attributes({'labels': ['a', 'b', 'c']})

        dest = tmp_path / 'out.mrk.json'
        with pytest.raises(ExtractionError, match='does not match'):
            extract_landmarks(zarr_path, f'{ann_path}/data', dest)

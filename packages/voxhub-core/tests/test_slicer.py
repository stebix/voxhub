"""Tests for Slicer format parsers and writers."""


import numpy as np
import pytest
from _core_helpers import ORIGIN_LPS, SHAPE, SPACE_DIRECTIONS
from _core_helpers import write_mrk_json as _write_mrk
from _core_helpers import write_seg_nrrd as _write_seg

from voxhub_core.slicer import (
    build_seg_nrrd_header,
    parse_mrk_json,
    parse_seg_nrrd,
    write_mrk_json,
)

# ===================================================================
# SEGMENTATION PARSING
# ===================================================================


class TestParseSegNrrd:
    def test_parses_label_map_shape(self, tmp_path):
        lm = np.zeros(SHAPE, dtype=np.int16)
        lm[0, 0, 0] = 1
        segments = [{'name': 'region', 'label_value': 1}]
        path = _write_seg(tmp_path / 'test.seg.nrrd', lm, segments)
        result = parse_seg_nrrd(path)
        assert result.label_map.shape == SHAPE

    def test_parses_segment_metadata(self, tmp_path):
        lm = np.zeros(SHAPE, dtype=np.int16)
        lm[0, 0, 0] = 1
        lm[1, 1, 1] = 2
        segments = [
            {'name': 'cochlea', 'label_value': 1, 'id': 'seg_0'},
            {'name': 'vestibule', 'label_value': 2, 'id': 'seg_1'},
        ]
        path = _write_seg(tmp_path / 'test.seg.nrrd', lm, segments)
        result = parse_seg_nrrd(path)
        assert len(result.segments) == 2
        assert result.segments[0].name == 'cochlea'
        assert result.segments[0].label_value == 1
        assert result.segments[1].name == 'vestibule'
        assert result.segments[1].label_value == 2

    def test_parses_spatial_metadata(self, tmp_path):
        lm = np.zeros(SHAPE, dtype=np.int16)
        path = _write_seg(tmp_path / 'test.seg.nrrd', lm, [])
        result = parse_seg_nrrd(path)
        np.testing.assert_allclose(result.space_origin, ORIGIN_LPS)
        np.testing.assert_allclose(result.space_directions, SPACE_DIRECTIONS)

    def test_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            parse_seg_nrrd(tmp_path / 'nonexistent.seg.nrrd')

    def test_label_values_preserved(self, tmp_path):
        """Exact integer labels survive the write-parse round trip."""
        lm = np.zeros(SHAPE, dtype=np.int16)
        lm[0, 0, 0] = 3
        lm[1, 1, 1] = 7
        segments = [
            {'name': 'a', 'label_value': 3},
            {'name': 'b', 'label_value': 7},
        ]
        path = _write_seg(tmp_path / 'test.seg.nrrd', lm, segments)
        result = parse_seg_nrrd(path)
        assert int(result.label_map[0, 0, 0]) == 3
        assert int(result.label_map[1, 1, 1]) == 7


# ===================================================================
# LANDMARK PARSING
# ===================================================================


class TestParseMrkJson:
    def test_parses_points_and_labels(self, tmp_path):
        pts = [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]
        labels = ['round_window', 'oval_window']
        path = _write_mrk(tmp_path / 'test.mrk.json', pts, labels, 'LPS')
        result = parse_mrk_json(path)
        assert result.labels == labels
        np.testing.assert_allclose(result.points, pts)
        assert result.coordinate_system == 'LPS'

    def test_parses_ras_coordinate_system(self, tmp_path):
        path = _write_mrk(
            tmp_path / 'test.mrk.json',
            [[1.0, 2.0, 3.0]],
            ['pt'],
            'RAS',
        )
        result = parse_mrk_json(path)
        assert result.coordinate_system == 'RAS'

    def test_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            parse_mrk_json(tmp_path / 'nonexistent.mrk.json')

    def test_invalid_json_raises(self, tmp_path):
        path = tmp_path / 'bad.mrk.json'
        path.write_text('{"no_markups": true}')
        with pytest.raises(ValueError, match='controlPoints'):
            parse_mrk_json(path)

    def test_empty_control_points(self, tmp_path):
        path = _write_mrk(tmp_path / 'test.mrk.json', [], [], 'LPS')
        result = parse_mrk_json(path)
        assert len(result.labels) == 0
        assert result.points.shape == (0, 3)


# ===================================================================
# LANDMARK WRITE → PARSE ROUND TRIP
# ===================================================================


class TestMrkJsonRoundTrip:
    def test_write_then_parse_preserves_data(self, tmp_path):
        pts = np.array([[1.1, 2.2, 3.3], [-4.4, -5.5, -6.6]])
        labels = ['round_window', 'oval_window']
        path = tmp_path / 'test.mrk.json'
        write_mrk_json(path, pts, labels, 'LPS')
        result = parse_mrk_json(path)
        np.testing.assert_allclose(result.points, pts)
        assert result.labels == labels
        assert result.coordinate_system == 'LPS'


# ===================================================================
# SEG NRRD HEADER BUILDER
# ===================================================================


class TestBuildSegNrrdHeader:
    def test_contains_slicer_fields(self):
        origin = np.array(ORIGIN_LPS)
        dirs = np.array(SPACE_DIRECTIONS)
        lm = np.zeros(SHAPE, dtype=np.int16)
        lm[0, 0, 0] = 1
        segments = [
            {
                'id': 'seg_0',
                'name': 'cochlea',
                'label_value': 1,
                'color': (1.0, 0.0, 0.0),
            },
        ]
        header = build_seg_nrrd_header(SHAPE, origin, dirs, segments, lm)
        assert header['space'] == 'left-posterior-superior'
        assert 'Segment0_Name' in header
        assert header['Segment0_Name'] == 'cochlea'
        assert 'Segment0_LabelValue' in header

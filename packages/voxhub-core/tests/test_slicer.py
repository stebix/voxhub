"""Tests for Slicer format parsers and writers."""

import json

import nrrd
import numpy as np
import pytest
from _core_helpers import ORIGIN_LPS, SHAPE, SPACE_DIRECTIONS
from _core_helpers import write_mrk_json as _write_mrk
from _core_helpers import write_seg_nrrd as _write_seg

from voxhub_core.slicer import (
    MrkJsonParseError,
    SegNrrdParseError,
    SlicerParseError,
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
        header = build_seg_nrrd_header(origin, dirs, segments, lm)
        assert header['space'] == 'left-posterior-superior'
        assert 'Segment0_Name' in header
        assert header['Segment0_Name'] == 'cochlea'
        assert 'Segment0_LabelValue' in header


# ===================================================================
# DEFENSIVE PARSING: SEG NRRD FAILURE PATHS
# ===================================================================


def _write_raw_seg_nrrd(path, label_map, header):
    """Low-level seg.nrrd writer that lets tests produce malformed headers."""
    nrrd.write(str(path), label_map, header)
    return path


class TestParseSegNrrdErrors:
    def test_exception_hierarchy(self):
        assert issubclass(SegNrrdParseError, SlicerParseError)
        assert issubclass(SlicerParseError, ValueError)

    def test_corrupt_file_raises(self, tmp_path):
        path = tmp_path / 'bad.seg.nrrd'
        path.write_bytes(b'not an nrrd file')
        with pytest.raises(SegNrrdParseError, match='failed to read NRRD'):
            parse_seg_nrrd(path)

    def test_non_integer_label_map_raises(self, tmp_path):
        lm = np.zeros(SHAPE, dtype=np.float32)
        path = _write_seg(tmp_path / 'test.seg.nrrd', lm, [])
        with pytest.raises(SegNrrdParseError, match='integer dtype'):
            parse_seg_nrrd(path)

    def test_missing_label_value_raises(self, tmp_path):
        lm = np.zeros(SHAPE, dtype=np.int16)
        lm[0, 0, 0] = 1
        header = {
            'space': 'left-posterior-superior',
            'space origin': ORIGIN_LPS,
            'space directions': SPACE_DIRECTIONS,
            'kinds': ['domain', 'domain', 'domain'],
            'Segment0_ID': 's0',
            'Segment0_Name': 'region',
            'Segment0_Color': '0.5 0.5 0.5',
            # LabelValue intentionally omitted
        }
        path = _write_raw_seg_nrrd(tmp_path / 'test.seg.nrrd', lm, header)
        with pytest.raises(SegNrrdParseError, match='Segment0_LabelValue') as exc:
            parse_seg_nrrd(path)
        assert exc.value.field == 'Segment0_LabelValue'
        assert exc.value.path == path

    def test_non_integer_label_value_raises(self, tmp_path):
        lm = np.zeros(SHAPE, dtype=np.int16)
        lm[0, 0, 0] = 1
        header = {
            'space': 'left-posterior-superior',
            'space origin': ORIGIN_LPS,
            'space directions': SPACE_DIRECTIONS,
            'kinds': ['domain', 'domain', 'domain'],
            'Segment0_ID': 's0',
            'Segment0_Name': 'region',
            'Segment0_Color': '0.5 0.5 0.5',
            'Segment0_LabelValue': 'not-a-number',
        }
        path = _write_raw_seg_nrrd(tmp_path / 'test.seg.nrrd', lm, header)
        with pytest.raises(SegNrrdParseError, match='not an integer'):
            parse_seg_nrrd(path)

    def test_missing_color_raises(self, tmp_path):
        lm = np.zeros(SHAPE, dtype=np.int16)
        lm[0, 0, 0] = 1
        header = {
            'space': 'left-posterior-superior',
            'space origin': ORIGIN_LPS,
            'space directions': SPACE_DIRECTIONS,
            'kinds': ['domain', 'domain', 'domain'],
            'Segment0_ID': 's0',
            'Segment0_Name': 'region',
            'Segment0_LabelValue': '1',
            # Color intentionally omitted
        }
        path = _write_raw_seg_nrrd(tmp_path / 'test.seg.nrrd', lm, header)
        with pytest.raises(SegNrrdParseError, match='Segment0_Color'):
            parse_seg_nrrd(path)

    def test_malformed_color_raises(self, tmp_path):
        lm = np.zeros(SHAPE, dtype=np.int16)
        lm[0, 0, 0] = 1
        segments = [
            {'id': 's0', 'name': 'region', 'label_value': 1, 'color': '1 0'},
        ]
        path = _write_seg(tmp_path / 'test.seg.nrrd', lm, segments)
        with pytest.raises(SegNrrdParseError, match='3 components'):
            parse_seg_nrrd(path)


# ===================================================================
# DEFENSIVE PARSING: MRK JSON FAILURE PATHS
# ===================================================================


class TestParseMrkJsonErrors:
    def test_exception_hierarchy(self):
        assert issubclass(MrkJsonParseError, SlicerParseError)
        assert issubclass(SlicerParseError, ValueError)

    def test_malformed_json_raises(self, tmp_path):
        path = tmp_path / 'bad.mrk.json'
        path.write_text('{not valid json')
        with pytest.raises(MrkJsonParseError, match='invalid JSON'):
            parse_mrk_json(path)

    def test_non_dict_root_raises(self, tmp_path):
        path = tmp_path / 'bad.mrk.json'
        path.write_text('[1, 2, 3]')
        with pytest.raises(MrkJsonParseError, match='must be an object'):
            parse_mrk_json(path)

    def test_invalid_coord_system_raises(self, tmp_path):
        path = tmp_path / 'bad.mrk.json'
        path.write_text(
            json.dumps(
                {
                    'markups': [
                        {
                            'coordinateSystem': 'XYZ',
                            'controlPoints': [],
                        }
                    ]
                }
            )
        )
        with pytest.raises(MrkJsonParseError, match=r'LPS.*RAS') as exc:
            parse_mrk_json(path)
        assert exc.value.field == 'coordinateSystem'

    def test_missing_coord_system_raises(self, tmp_path):
        path = tmp_path / 'bad.mrk.json'
        path.write_text(json.dumps({'markups': [{'controlPoints': []}]}))
        with pytest.raises(MrkJsonParseError, match='coordinateSystem'):
            parse_mrk_json(path)

    def test_position_wrong_length_raises(self, tmp_path):
        path = tmp_path / 'bad.mrk.json'
        path.write_text(
            json.dumps(
                {
                    'markups': [
                        {
                            'coordinateSystem': 'LPS',
                            'controlPoints': [{'label': 'a', 'position': [1.0, 2.0]}],
                        }
                    ]
                }
            )
        )
        with pytest.raises(MrkJsonParseError, match='3-element') as exc:
            parse_mrk_json(path)
        assert exc.value.field == 'controlPoints[0].position'

    def test_position_non_numeric_raises(self, tmp_path):
        path = tmp_path / 'bad.mrk.json'
        path.write_text(
            json.dumps(
                {
                    'markups': [
                        {
                            'coordinateSystem': 'LPS',
                            'controlPoints': [
                                {'label': 'a', 'position': [1.0, 2.0, 'oops']}
                            ],
                        }
                    ]
                }
            )
        )
        with pytest.raises(MrkJsonParseError, match='non-numeric'):
            parse_mrk_json(path)

    def test_missing_label_raises(self, tmp_path):
        path = tmp_path / 'bad.mrk.json'
        path.write_text(
            json.dumps(
                {
                    'markups': [
                        {
                            'coordinateSystem': 'LPS',
                            'controlPoints': [{'position': [1.0, 2.0, 3.0]}],
                        }
                    ]
                }
            )
        )
        with pytest.raises(MrkJsonParseError, match="'label'"):
            parse_mrk_json(path)

    def test_exception_carries_context(self, tmp_path):
        path = tmp_path / 'bad.mrk.json'
        path.write_text('{"no_markups": true}')
        with pytest.raises(MrkJsonParseError) as exc:
            parse_mrk_json(path)
        assert exc.value.path == path
        assert exc.value.reason  # non-empty

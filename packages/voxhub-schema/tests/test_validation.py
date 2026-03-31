"""Tests for client-side pre-flight validation.

These tests verify that the validation gate correctly enforces ontology
conformance, spatial consistency, and data integrity for both
segmentation NRRD files and landmark JSON files *before* they are
pushed to the server.

The test data is built programmatically — no fixture files on disk.
"""


import numpy as np
from _schema_helpers import (
    ORIGIN_LPS,
    SHAPE,
    write_mrk_json,
    write_seg_nrrd,
)

from voxhub_schema.models import IssueRecord
from voxhub_schema.validation import validate_lmk_preflight, validate_seg_preflight

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _errors(issues: list[IssueRecord]) -> list[IssueRecord]:
    return [i for i in issues if i.severity == 'error']


def _warnings(issues: list[IssueRecord]) -> list[IssueRecord]:
    return [i for i in issues if i.severity == 'warning']


def _make_inner_ear_label_map() -> np.ndarray:
    """Label map with all four inner-ear-structures labels present."""
    lm = np.zeros(SHAPE, dtype=np.int16)
    lm[0, 0, 0] = 1  # cochlea
    lm[1, 1, 1] = 2  # vestibule
    lm[2, 2, 2] = 3  # semicircular_canals
    return lm


_INNER_EAR_SEGMENTS = [
    {'name': 'cochlea', 'label_value': 1},
    {'name': 'vestibule', 'label_value': 2},
    {'name': 'semicircular_canals', 'label_value': 3},
]


def _make_fluid_space_label_map() -> np.ndarray:
    lm = np.zeros(SHAPE, dtype=np.int16)
    lm[0:3, 0:3, 0:3] = 1
    return lm


_FLUID_SPACE_SEGMENTS = [
    {'name': 'inner_ear_total_fluid_space', 'label_value': 1},
]


# ===================================================================
# SEGMENTATION — HAPPY PATHS
# ===================================================================


class TestSegPreflightHappyPath:
    """Valid segmentations that should produce no errors."""

    def test_valid_constrained_segmentation(
        self, tmp_path, manifest_entry, inner_ear_ontology
    ):
        seg = write_seg_nrrd(
            tmp_path / 'test.seg.nrrd',
            _make_inner_ear_label_map(),
            _INNER_EAR_SEGMENTS,
        )
        issues = validate_seg_preflight(seg, manifest_entry, inner_ear_ontology)
        assert _errors(issues) == []

    def test_valid_unconstrained_segmentation(
        self, tmp_path, manifest_entry, unconstrained_ontology
    ):
        lm = np.zeros(SHAPE, dtype=np.int16)
        lm[1, 1, 1] = 1
        lm[2, 2, 2] = 2
        segments = [
            {'name': 'region_a', 'label_value': 1},
            {'name': 'region_b', 'label_value': 2},
        ]
        seg = write_seg_nrrd(tmp_path / 'test.seg.nrrd', lm, segments)
        issues = validate_seg_preflight(seg, manifest_entry, unconstrained_ontology)
        assert _errors(issues) == []

    def test_valid_single_channel_segmentation(
        self, tmp_path, manifest_entry, fluid_space_ontology
    ):
        seg = write_seg_nrrd(
            tmp_path / 'test.seg.nrrd',
            _make_fluid_space_label_map(),
            _FLUID_SPACE_SEGMENTS,
        )
        issues = validate_seg_preflight(seg, manifest_entry, fluid_space_ontology)
        assert _errors(issues) == []

    def test_background_only_is_valid_for_unconstrained(
        self, tmp_path, manifest_entry, unconstrained_ontology
    ):
        """A volume containing only background (label 0) is valid."""
        lm = np.zeros(SHAPE, dtype=np.int16)
        seg = write_seg_nrrd(tmp_path / 'test.seg.nrrd', lm, [])
        issues = validate_seg_preflight(seg, manifest_entry, unconstrained_ontology)
        assert _errors(issues) == []


# ===================================================================
# SEGMENTATION — SHAPE ENFORCEMENT
# ===================================================================


class TestSegShapeEnforcement:
    def test_wrong_shape_is_error(
        self, tmp_path, manifest_entry, inner_ear_ontology
    ):
        wrong_shape = (8, 12, 14)  # z differs
        lm = np.zeros(wrong_shape, dtype=np.int16)
        seg = write_seg_nrrd(tmp_path / 'test.seg.nrrd', lm, [])
        issues = validate_seg_preflight(seg, manifest_entry, inner_ear_ontology)
        errors = _errors(issues)
        assert len(errors) >= 1
        assert any('shape' in e.message.lower() for e in errors)


# ===================================================================
# SEGMENTATION — SPATIAL ENFORCEMENT
# ===================================================================


class TestSegSpatialEnforcement:
    def test_origin_within_tolerance_passes(
        self, tmp_path, manifest_entry, inner_ear_ontology
    ):
        nudge = 0.005  # within default 0.01 mm tolerance
        seg = write_seg_nrrd(
            tmp_path / 'test.seg.nrrd',
            _make_inner_ear_label_map(),
            _INNER_EAR_SEGMENTS,
            origin=[o + nudge for o in ORIGIN_LPS],
        )
        issues = validate_seg_preflight(seg, manifest_entry, inner_ear_ontology)
        origin_errors = [
            e for e in _errors(issues) if 'origin' in e.message.lower()
        ]
        assert origin_errors == []

    def test_origin_beyond_tolerance_is_error(
        self, tmp_path, manifest_entry, inner_ear_ontology
    ):
        drift = 0.05  # well beyond 0.01 mm
        seg = write_seg_nrrd(
            tmp_path / 'test.seg.nrrd',
            _make_inner_ear_label_map(),
            _INNER_EAR_SEGMENTS,
            origin=[o + drift for o in ORIGIN_LPS],
        )
        issues = validate_seg_preflight(seg, manifest_entry, inner_ear_ontology)
        errors = _errors(issues)
        assert any('origin' in e.message.lower() for e in errors)

    def test_custom_tolerance_respected(
        self, tmp_path, manifest_entry, inner_ear_ontology
    ):
        """A drift that exceeds a tight tolerance should fail."""
        drift = 0.005
        seg = write_seg_nrrd(
            tmp_path / 'test.seg.nrrd',
            _make_inner_ear_label_map(),
            _INNER_EAR_SEGMENTS,
            origin=[o + drift for o in ORIGIN_LPS],
        )
        issues = validate_seg_preflight(
            seg, manifest_entry, inner_ear_ontology, spatial_tolerance=0.001
        )
        assert any('origin' in e.message.lower() for e in _errors(issues))

    def test_space_directions_mismatch_is_error(
        self, tmp_path, manifest_entry, inner_ear_ontology
    ):
        wrong_dirs = [[0.6, 0.0, 0.0], [0.0, 0.5, 0.0], [0.0, 0.0, 0.5]]
        seg = write_seg_nrrd(
            tmp_path / 'test.seg.nrrd',
            _make_inner_ear_label_map(),
            _INNER_EAR_SEGMENTS,
            space_directions=wrong_dirs,
        )
        issues = validate_seg_preflight(seg, manifest_entry, inner_ear_ontology)
        errors = _errors(issues)
        assert any('direction' in e.message.lower() for e in errors)


# ===================================================================
# SEGMENTATION — VALUE INTEGRITY
# ===================================================================


class TestSegValueIntegrity:
    def test_negative_labels_are_error(
        self, tmp_path, manifest_entry, unconstrained_ontology
    ):
        lm = np.zeros(SHAPE, dtype=np.int16)
        lm[0, 0, 0] = -1
        seg = write_seg_nrrd(tmp_path / 'test.seg.nrrd', lm, [])
        issues = validate_seg_preflight(seg, manifest_entry, unconstrained_ontology)
        assert any('negative' in e.message.lower() for e in _errors(issues))

    def test_file_not_found_is_error(
        self, tmp_path, manifest_entry, inner_ear_ontology
    ):
        missing = tmp_path / 'nonexistent.seg.nrrd'
        issues = validate_seg_preflight(
            missing, manifest_entry, inner_ear_ontology
        )
        assert len(_errors(issues)) == 1
        assert 'not found' in _errors(issues)[0].message.lower()


# ===================================================================
# SEGMENTATION — LABEL INTEGRITY WARNINGS
# ===================================================================


class TestSegLabelIntegrity:
    def test_labels_in_volume_without_segment_definition_warns(
        self, tmp_path, manifest_entry, unconstrained_ontology
    ):
        """Labels present in the voxel data but absent from the header."""
        lm = np.zeros(SHAPE, dtype=np.int16)
        lm[0, 0, 0] = 1
        lm[1, 1, 1] = 2
        # Define only label 1 in header; label 2 is "undefined".
        segments = [{'name': 'region_a', 'label_value': 1}]
        seg = write_seg_nrrd(tmp_path / 'test.seg.nrrd', lm, segments)
        issues = validate_seg_preflight(seg, manifest_entry, unconstrained_ontology)
        warns = _warnings(issues)
        assert any('no segment definition' in w.message.lower() for w in warns)

    def test_gap_in_label_sequence_warns(
        self, tmp_path, manifest_entry, unconstrained_ontology
    ):
        """Labels 1, 3 present but 2 missing from header — gap warning."""
        lm = np.zeros(SHAPE, dtype=np.int16)
        lm[0, 0, 0] = 1
        lm[1, 1, 1] = 3
        segments = [
            {'name': 'a', 'label_value': 1},
            {'name': 'c', 'label_value': 3},
        ]
        seg = write_seg_nrrd(tmp_path / 'test.seg.nrrd', lm, segments)
        issues = validate_seg_preflight(seg, manifest_entry, unconstrained_ontology)
        assert any('gap' in w.message.lower() for w in _warnings(issues))


# ===================================================================
# SEGMENTATION — CONSTRAINED ONTOLOGY ENFORCEMENT
# ===================================================================


class TestSegConstrainedOntology:
    """Verify that constrained ontologies enforce their label contract."""

    def test_missing_required_label_is_error(
        self, tmp_path, manifest_entry, inner_ear_ontology
    ):
        """Omitting 'semicircular_canals' (label 3) should error."""
        lm = np.zeros(SHAPE, dtype=np.int16)
        lm[0, 0, 0] = 1
        lm[1, 1, 1] = 2
        # Only cochlea and vestibule — missing semicircular_canals.
        segments = [
            {'name': 'cochlea', 'label_value': 1},
            {'name': 'vestibule', 'label_value': 2},
        ]
        seg = write_seg_nrrd(tmp_path / 'test.seg.nrrd', lm, segments)
        issues = validate_seg_preflight(seg, manifest_entry, inner_ear_ontology)
        errors = _errors(issues)
        assert any('semicircular_canals' in e.message for e in errors)

    def test_extra_label_not_in_ontology_is_error(
        self, tmp_path, manifest_entry, inner_ear_ontology
    ):
        """A label value that the ontology doesn't define should error."""
        lm = _make_inner_ear_label_map()
        lm[3, 3, 3] = 4  # extra label
        segments = [
            *_INNER_EAR_SEGMENTS,
            {'name': 'extra_structure', 'label_value': 4},
        ]
        seg = write_seg_nrrd(tmp_path / 'test.seg.nrrd', lm, segments)
        issues = validate_seg_preflight(seg, manifest_entry, inner_ear_ontology)
        errors = _errors(issues)
        assert any('not in the ontology' in e.message for e in errors)

    def test_label_name_mismatch_is_warning(
        self, tmp_path, manifest_entry, inner_ear_ontology
    ):
        """Correct label value but wrong name should warn, not error."""
        lm = _make_inner_ear_label_map()
        # Use wrong name for label 1 (should be 'cochlea').
        segments = [
            {'name': 'WRONG_NAME', 'label_value': 1},
            {'name': 'vestibule', 'label_value': 2},
            {'name': 'semicircular_canals', 'label_value': 3},
        ]
        seg = write_seg_nrrd(tmp_path / 'test.seg.nrrd', lm, segments)
        issues = validate_seg_preflight(seg, manifest_entry, inner_ear_ontology)
        # Name mismatch = warning, not error.
        assert any('name mismatch' in w.message.lower() for w in _warnings(issues))
        # No errors about missing labels (all values are present).
        missing_errors = [
            e for e in _errors(issues) if 'not defined' in e.message
        ]
        assert missing_errors == []

    def test_all_labels_present_and_named_correctly(
        self, tmp_path, manifest_entry, inner_ear_ontology
    ):
        """Fully conformant segmentation — no ontology issues at all."""
        seg = write_seg_nrrd(
            tmp_path / 'test.seg.nrrd',
            _make_inner_ear_label_map(),
            _INNER_EAR_SEGMENTS,
        )
        issues = validate_seg_preflight(seg, manifest_entry, inner_ear_ontology)
        ontology_issues = [
            i for i in issues
            if 'ontology' in i.message.lower()
            or 'not in the' in i.message.lower()
            or 'not defined' in i.message.lower()
            or 'name mismatch' in i.message.lower()
        ]
        assert ontology_issues == []

    def test_single_channel_ontology_rejects_extra_label(
        self, tmp_path, manifest_entry, fluid_space_ontology
    ):
        """fluid-space ontology has labels {0, 1} — label 2 should error."""
        lm = _make_fluid_space_label_map()
        lm[5, 5, 5] = 2  # extra
        segments = [
            *_FLUID_SPACE_SEGMENTS,
            {'name': 'unknown', 'label_value': 2},
        ]
        seg = write_seg_nrrd(tmp_path / 'test.seg.nrrd', lm, segments)
        issues = validate_seg_preflight(seg, manifest_entry, fluid_space_ontology)
        assert any('not in the ontology' in e.message for e in _errors(issues))


# ===================================================================
# SEGMENTATION — UNCONSTRAINED ONTOLOGY ENFORCEMENT
# ===================================================================


class TestSegUnconstrainedOntology:
    """Structural constraints for the unconstrained ontology."""

    def test_non_sequential_labels_violate_constraint(
        self, tmp_path, manifest_entry, unconstrained_ontology
    ):
        """Labels [0, 1, 3] (gap at 2) violate sequential_from_zero."""
        lm = np.zeros(SHAPE, dtype=np.int16)
        lm[0, 0, 0] = 1
        lm[1, 1, 1] = 3  # skips 2
        segments = [
            {'name': 'a', 'label_value': 1},
            {'name': 'c', 'label_value': 3},
        ]
        seg = write_seg_nrrd(tmp_path / 'test.seg.nrrd', lm, segments)
        issues = validate_seg_preflight(seg, manifest_entry, unconstrained_ontology)
        assert any('sequential' in e.message.lower() for e in _errors(issues))

    def test_missing_background_at_zero_violates_constraint(
        self, tmp_path, manifest_entry, unconstrained_ontology
    ):
        """A volume with no 0 label violates background_at_zero."""
        lm = np.ones(SHAPE, dtype=np.int16)  # all 1, no 0
        segments = [{'name': 'fill', 'label_value': 1}]
        seg = write_seg_nrrd(tmp_path / 'test.seg.nrrd', lm, segments)
        issues = validate_seg_preflight(seg, manifest_entry, unconstrained_ontology)
        assert any('background' in e.message.lower() for e in _errors(issues))

    def test_valid_sequential_labels_pass(
        self, tmp_path, manifest_entry, unconstrained_ontology
    ):
        """Labels [0, 1, 2] satisfy all three constraints."""
        lm = np.zeros(SHAPE, dtype=np.int16)
        lm[0, 0, 0] = 1
        lm[1, 1, 1] = 2
        segments = [
            {'name': 'a', 'label_value': 1},
            {'name': 'b', 'label_value': 2},
        ]
        seg = write_seg_nrrd(tmp_path / 'test.seg.nrrd', lm, segments)
        issues = validate_seg_preflight(seg, manifest_entry, unconstrained_ontology)
        constraint_errors = [
            e for e in _errors(issues) if 'constraint' in e.message.lower()
        ]
        assert constraint_errors == []


# ===================================================================
# LANDMARKS — HAPPY PATHS
# ===================================================================


class TestLmkPreflightHappyPath:
    def test_valid_landmarks_lps(
        self, tmp_path, manifest_entry, landmark_ontology
    ):
        """All three required points in LPS, inside volume."""
        pts = [[-4.0, -5.0, -6.0], [-3.0, -4.0, -5.0], [-2.0, -3.0, -4.0]]
        labels = ['round_window', 'oval_window', 'cochlear_apex']
        lmk = write_mrk_json(tmp_path / 'test.mrk.json', pts, labels, 'LPS')
        issues = validate_lmk_preflight(lmk, manifest_entry, landmark_ontology)
        assert _errors(issues) == []

    def test_valid_landmarks_ras(
        self, tmp_path, manifest_entry, landmark_ontology
    ):
        """Points in RAS coordinate system — should be accepted."""
        # RAS → LPS: negate x and y.  The LPS-equivalent should be
        # inside the volume bounding box.
        pts = [[4.0, 5.0, -6.0], [3.0, 4.0, -5.0], [2.0, 3.0, -4.0]]
        labels = ['round_window', 'oval_window', 'cochlear_apex']
        lmk = write_mrk_json(tmp_path / 'test.mrk.json', pts, labels, 'RAS')
        issues = validate_lmk_preflight(lmk, manifest_entry, landmark_ontology)
        assert _errors(issues) == []


# ===================================================================
# LANDMARKS — COORDINATE SYSTEM ENFORCEMENT
# ===================================================================


class TestLmkCoordinateSystem:
    def test_unknown_coordinate_system_is_error(
        self, tmp_path, manifest_entry, landmark_ontology
    ):
        pts = [[0.0, 0.0, 0.0]]
        labels = ['round_window']
        lmk = write_mrk_json(tmp_path / 'test.mrk.json', pts, labels, 'UNKNOWN')
        issues = validate_lmk_preflight(lmk, manifest_entry, landmark_ontology)
        assert any('coordinate system' in e.message.lower() for e in _errors(issues))


# ===================================================================
# LANDMARKS — LABEL UNIQUENESS
# ===================================================================


class TestLmkLabelUniqueness:
    def test_duplicate_labels_are_error(
        self, tmp_path, manifest_entry, landmark_ontology
    ):
        pts = [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]]
        labels = ['round_window', 'round_window']  # duplicate
        lmk = write_mrk_json(tmp_path / 'test.mrk.json', pts, labels, 'LPS')
        issues = validate_lmk_preflight(lmk, manifest_entry, landmark_ontology)
        assert any('duplicate' in e.message.lower() for e in _errors(issues))


# ===================================================================
# LANDMARKS — ONTOLOGY ENFORCEMENT
# ===================================================================


class TestLmkOntologyEnforcement:
    """Verify that landmark ontologies enforce their point contract."""

    def test_missing_required_point_is_error(
        self, tmp_path, manifest_entry, landmark_ontology
    ):
        """inner-ear-landmarks requires 3 points — omitting one errors."""
        pts = [[-4.0, -5.0, -6.0], [-3.0, -4.0, -5.0]]
        labels = ['round_window', 'oval_window']  # missing cochlear_apex
        lmk = write_mrk_json(tmp_path / 'test.mrk.json', pts, labels, 'LPS')
        issues = validate_lmk_preflight(lmk, manifest_entry, landmark_ontology)
        errors = _errors(issues)
        assert any('cochlear_apex' in e.message for e in errors)

    def test_extra_point_not_in_ontology_is_error(
        self, tmp_path, manifest_entry, landmark_ontology
    ):
        pts = [
            [-4.0, -5.0, -6.0],
            [-3.0, -4.0, -5.0],
            [-2.0, -3.0, -4.0],
            [-1.0, -2.0, -3.0],
        ]
        labels = [
            'round_window',
            'oval_window',
            'cochlear_apex',
            'extra_point',  # not in ontology
        ]
        lmk = write_mrk_json(tmp_path / 'test.mrk.json', pts, labels, 'LPS')
        issues = validate_lmk_preflight(lmk, manifest_entry, landmark_ontology)
        errors = _errors(issues)
        assert any('extra_point' in e.message for e in errors)

    def test_all_required_points_present_no_ontology_errors(
        self, tmp_path, manifest_entry, landmark_ontology
    ):
        pts = [[-4.0, -5.0, -6.0], [-3.0, -4.0, -5.0], [-2.0, -3.0, -4.0]]
        labels = ['round_window', 'oval_window', 'cochlear_apex']
        lmk = write_mrk_json(tmp_path / 'test.mrk.json', pts, labels, 'LPS')
        issues = validate_lmk_preflight(lmk, manifest_entry, landmark_ontology)
        ontology_errors = [
            e for e in _errors(issues)
            if 'ontology' in e.message.lower()
            or 'missing' in e.message.lower()
            or 'not defined' in e.message.lower()
        ]
        assert ontology_errors == []

    def test_segmentation_ontology_does_not_enforce_points(
        self, tmp_path, manifest_entry, inner_ear_ontology
    ):
        """A segmentation ontology passed to landmark validation should
        not apply point-name enforcement (it has no ``points`` field)."""
        pts = [[-4.0, -5.0, -6.0]]
        labels = ['arbitrary']
        lmk = write_mrk_json(tmp_path / 'test.mrk.json', pts, labels, 'LPS')
        issues = validate_lmk_preflight(lmk, manifest_entry, inner_ear_ontology)
        ontology_errors = [
            e for e in _errors(issues) if 'ontology' in e.message.lower()
        ]
        assert ontology_errors == []


# ===================================================================
# LANDMARKS — BOUNDING BOX WARNINGS
# ===================================================================


class TestLmkBoundingBox:
    def test_point_outside_volume_warns(
        self, tmp_path, manifest_entry, landmark_ontology
    ):
        """A point far from the volume should trigger a warning."""
        pts = [
            [100.0, 100.0, 100.0],  # far outside
            [-3.0, -4.0, -5.0],
            [-2.0, -3.0, -4.0],
        ]
        labels = ['round_window', 'oval_window', 'cochlear_apex']
        lmk = write_mrk_json(tmp_path / 'test.mrk.json', pts, labels, 'LPS')
        issues = validate_lmk_preflight(lmk, manifest_entry, landmark_ontology)
        assert any('outside' in w.message.lower() for w in _warnings(issues))

    def test_point_inside_volume_no_warning(
        self, tmp_path, manifest_entry, landmark_ontology
    ):
        """Points inside the bounding box should not warn."""
        pts = [[-4.0, -5.0, -6.0], [-3.0, -4.0, -5.0], [-2.0, -3.0, -4.0]]
        labels = ['round_window', 'oval_window', 'cochlear_apex']
        lmk = write_mrk_json(tmp_path / 'test.mrk.json', pts, labels, 'LPS')
        issues = validate_lmk_preflight(lmk, manifest_entry, landmark_ontology)
        bounds_warnings = [
            w for w in _warnings(issues) if 'outside' in w.message.lower()
        ]
        assert bounds_warnings == []


# ===================================================================
# LANDMARKS — FILE ERRORS
# ===================================================================


class TestLmkFileErrors:
    def test_file_not_found_is_error(
        self, tmp_path, manifest_entry, landmark_ontology
    ):
        missing = tmp_path / 'nonexistent.mrk.json'
        issues = validate_lmk_preflight(
            missing, manifest_entry, landmark_ontology
        )
        assert len(_errors(issues)) == 1
        assert 'not found' in _errors(issues)[0].message.lower()

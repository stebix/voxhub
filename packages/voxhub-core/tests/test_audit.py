"""Tests for voxhub_core.audit — cross-store coherence checking.

Plan: docs/testing/catalog-staging-audit.md §5

Each test is currently skipped.  Remove the skip marker as tests are
implemented in a downstream worktree.

Fixtures expected:
    - zarr_root_factory
    - create_zarr_store_with_annotations (helper in _core_helpers.py)
"""

import pytest

pytestmark = pytest.mark.skip(
    reason='stub — see docs/testing/catalog-staging-audit.md §5'
)


# ===========================================================================
# _probe_store_annotations
# ===========================================================================


class TestProbeStoreAnnotations:
    """Covers voxhub_core.audit._probe_store_annotations."""

    def test_store_with_segmentation_only(self, zarr_root_factory):
        """Store has segmentation annotation → returned StoreAnnotationInfo
        has has_segmentation=True, segment_map populated, has_landmarks=False."""
        del zarr_root_factory

    def test_store_with_landmarks_only(self, zarr_root_factory):
        """Store has landmarks annotation → has_landmarks=True,
        landmark_labels populated, has_segmentation=False."""
        del zarr_root_factory

    def test_store_with_both(self, zarr_root_factory):
        """Store has both annotation types → both flags true, both fields
        populated."""
        del zarr_root_factory

    def test_store_with_neither_annotation_type(self, zarr_root_factory):
        """Store has no annotations → both flags false, both fields empty."""
        del zarr_root_factory

    def test_corrupted_store_records_error(self, zarr_root_factory):
        """Store with broken annotations/ group → error populated in the
        returned StoreAnnotationInfo, function does NOT raise."""
        del zarr_root_factory


# ===========================================================================
# Segmentation coherence
# ===========================================================================


class TestSegmentationCoherence:
    """Covers _check_segmentation_coherence in voxhub_core.audit.

    Setup: build 3 stores with overlapping but not identical segment sets.
    """

    def test_coherent_stores_no_issues(self, zarr_root_factory):
        """All three stores have the identical segment set → zero issues
        reported."""
        del zarr_root_factory

    def test_missing_segment_flagged(self, zarr_root_factory):
        """Store A has {cochlea}, B and C both have {cochlea, vestibule}
        → A is flagged as missing 'vestibule' with severity='warning' and
        category='missing_segment'."""
        del zarr_root_factory

    def test_extra_segment_flagged(self, zarr_root_factory):
        """Store A has an extra 'non_standard_label' not present in the
        majority set → flagged with category='extra_segment'."""
        del zarr_root_factory

    def test_label_value_mismatch_flagged(self, zarr_root_factory):
        """Stores A and B both have 'cochlea' but with different
        label_value → flagged with category='label_mismatch'."""
        del zarr_root_factory

    def test_majority_set_used_as_reference(self, zarr_root_factory):
        """Three stores with set X, one store with set Y → X is the
        reference; the Y store is flagged for each diff."""
        del zarr_root_factory

    def test_tie_break_when_no_majority(self, zarr_root_factory):
        """Two stores with set X, two with set Y → document current tie-
        break behavior (read the implementation carefully when
        fleshing this out)."""
        del zarr_root_factory


# ===========================================================================
# Landmark coherence
# ===========================================================================


class TestLandmarkCoherence:
    """Covers _check_landmark_coherence in voxhub_core.audit."""

    def test_coherent_landmark_labels_no_issues(self, zarr_root_factory):
        """All stores have identical landmark label set → zero issues."""
        del zarr_root_factory

    def test_missing_landmark_flagged(self, zarr_root_factory):
        """Store is missing a required landmark that others have → flagged
        with category='missing_landmark' (pin the actual string)."""
        del zarr_root_factory

    def test_extra_landmark_flagged(self, zarr_root_factory):
        """Store has a landmark label not in the majority set → flagged."""
        del zarr_root_factory


# ===========================================================================
# audit() — filtering
# ===========================================================================


class TestAuditFiltering:
    """Covers the public audit() function's filter parameters."""

    def test_ontology_filter_restricts_analysis(self, zarr_root_factory):
        """audit(..., ontology_filter='inner-ear-structures') → stores
        without that ontology are excluded from comparisons entirely."""
        del zarr_root_factory

    def test_store_names_filter(self, zarr_root_factory):
        """store_names=['a', 'c'] → only those two stores compared;
        store 'b' is not even probed."""
        del zarr_root_factory

    def test_empty_root_returns_no_issues(self, tmp_path):
        """Empty zarr_root → zero issues."""
        del tmp_path

    def test_single_store_returns_no_issues(self, zarr_root_factory):
        """A single store alone cannot have coherence issues → empty list."""
        del zarr_root_factory


# ===========================================================================
# Rendering smoke tests
# ===========================================================================


class TestAuditRendering:
    """Smoke tests for audit report rendering — no visual assertions."""

    def test_audit_renders_empty_report_without_crash(self, tmp_path):
        """Empty root → audit() renders without crashing."""
        del tmp_path

    def test_audit_renders_populated_report_without_crash(
        self, zarr_root_factory
    ):
        """Populated root with issues → audit() renders without crashing."""
        del zarr_root_factory

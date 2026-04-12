"""Tests for voxhub_core.audit — cross-store coherence checking.

Plan: docs/testing/catalog-staging-audit.md §5
"""

import io
from pathlib import Path
from typing import Any

from _core_helpers import populate_store_annotation
from rich.console import Console

from voxhub_core.audit import (
    StoreAnnotationInfo,
    _check_landmark_coherence,
    _check_segmentation_coherence,
    _probe_store_annotations,
    audit,
)

# -- Canonical payloads -----------------------------------------------------


def _seg(
    names_to_values: dict[str, int],
) -> list[dict[str, Any]]:
    """Produce a segments list suitable for ``populate_store_annotation``."""
    return [
        {'id': f's{i}', 'name': n, 'label_value': v, 'color': [0.5, 0.5, 0.5]}
        for i, (n, v) in enumerate(names_to_values.items())
    ]


_FULL_SEG: dict[str, int] = {'cochlea': 1, 'vestibule': 2}
_PARTIAL_SEG: dict[str, int] = {'cochlea': 1}
_EXTRA_SEG: dict[str, int] = {
    'cochlea': 1,
    'vestibule': 2,
    'non_standard_label': 99,
}


def _silent_console() -> Console:
    return Console(file=io.StringIO(), record=False, width=160)


def _populate_seg(
    zarr_path: Path,
    *,
    segments: list[dict[str, Any]],
    annotator: str = 'alice',
    nano: str = 'aaa11111',
    ontology: str = 'inner-ear-structures',
    short_random: str = 'ab12',
) -> str:
    return populate_store_annotation(
        zarr_path,
        annotator_id=annotator,
        nano_id=nano,
        ontology=ontology,
        short_random=short_random,
        segments=segments,
    )


def _populate_lmk(
    zarr_path: Path,
    *,
    labels: list[str],
    annotator: str = 'alice',
    nano: str = 'aaa11111',
    ontology: str = 'inner-ear-landmarks',
    short_random: str = 'cd34',
) -> str:
    return populate_store_annotation(
        zarr_path,
        annotator_id=annotator,
        nano_id=nano,
        ontology=ontology,
        short_random=short_random,
        kind='landmarks',
        labels=labels,
    )


# ===========================================================================
# _probe_store_annotations
# ===========================================================================


class TestProbeStoreAnnotations:
    """Covers voxhub_core.audit._probe_store_annotations."""

    def test_store_with_segmentation_only(self, zarr_root_factory):
        root = zarr_root_factory(store_names=['foo'])
        _populate_seg(root / 'foo.zarr', segments=_seg(_FULL_SEG))

        [info] = _probe_store_annotations(root / 'foo.zarr')
        assert isinstance(info, StoreAnnotationInfo)
        assert info.has_segmentation is True
        assert info.has_landmarks is False
        assert info.segment_map == {1: 'cochlea', 2: 'vestibule'}
        assert info.ontology == 'inner-ear-structures'

    def test_store_with_landmarks_only(self, zarr_root_factory):
        root = zarr_root_factory(store_names=['foo'])
        _populate_lmk(
            root / 'foo.zarr',
            labels=['round_window', 'oval_window'],
        )

        [info] = _probe_store_annotations(root / 'foo.zarr')
        assert info.has_landmarks is True
        assert info.has_segmentation is False
        assert info.landmark_labels == ['round_window', 'oval_window']

    def test_store_with_both(self, zarr_root_factory):
        root = zarr_root_factory(store_names=['foo'])
        _populate_seg(root / 'foo.zarr', segments=_seg(_FULL_SEG))
        _populate_lmk(root / 'foo.zarr', labels=['round_window'])

        infos = _probe_store_annotations(root / 'foo.zarr')
        # One per annotation array (seg + lmk), so two infos.
        assert len(infos) == 2
        assert any(i.has_segmentation for i in infos)
        assert any(i.has_landmarks for i in infos)

    def test_store_with_no_annotations_returns_empty(self, zarr_root_factory):
        root = zarr_root_factory(store_names=['foo'])
        # No annotation populated → no info entries.
        assert _probe_store_annotations(root / 'foo.zarr') == []

    def test_corrupted_store_records_error(self, tmp_path):
        """Opening a path that isn't a zarr store yields a single info with
        ``error`` populated; the function must not raise."""
        not_a_store = tmp_path / 'not-a-store.zarr'
        not_a_store.mkdir()
        (not_a_store / 'garbage').write_text('hello')

        [info] = _probe_store_annotations(not_a_store)
        assert info.error is not None
        assert info.has_segmentation is False
        assert info.has_landmarks is False

    def test_ontology_filter_excludes_mismatched_annotations(self, zarr_root_factory):
        """An ontology filter drops annotations whose ontology differs."""
        root = zarr_root_factory(store_names=['foo'])
        _populate_seg(
            root / 'foo.zarr',
            segments=_seg(_FULL_SEG),
            ontology='inner-ear-structures',
        )
        _populate_lmk(
            root / 'foo.zarr',
            labels=['round_window'],
            ontology='inner-ear-landmarks',
        )
        infos = _probe_store_annotations(
            root / 'foo.zarr',
            ontology_filter='inner-ear-structures',
        )
        assert len(infos) == 1
        assert infos[0].ontology == 'inner-ear-structures'


# ===========================================================================
# Segmentation coherence
# ===========================================================================


class TestSegmentationCoherence:
    """Covers _check_segmentation_coherence."""

    def _probe_all(self, root: Path) -> list[StoreAnnotationInfo]:
        infos: list[StoreAnnotationInfo] = []
        for store_path in sorted(root.glob('*.zarr')):
            infos.extend(_probe_store_annotations(store_path))
        return infos

    def test_coherent_stores_no_issues(self, zarr_root_factory):
        root = zarr_root_factory(store_names=['a', 'b', 'c'])
        for name in ('a', 'b', 'c'):
            _populate_seg(root / f'{name}.zarr', segments=_seg(_FULL_SEG))

        assert _check_segmentation_coherence(self._probe_all(root)) == []

    def test_missing_segment_flagged(self, zarr_root_factory):
        """Store A has {cochlea}, B/C have {cochlea, vestibule} → A flagged
        as missing 'vestibule'."""
        root = zarr_root_factory(store_names=['a', 'b', 'c'])
        _populate_seg(root / 'a.zarr', segments=_seg(_PARTIAL_SEG))
        _populate_seg(root / 'b.zarr', segments=_seg(_FULL_SEG))
        _populate_seg(root / 'c.zarr', segments=_seg(_FULL_SEG))

        issues = _check_segmentation_coherence(self._probe_all(root))
        a_issues = [i for i in issues if i.store_name == 'a']
        assert any('vestibule' in i.message and 'Missing' in i.message for i in a_issues)
        # The current implementation categorises every segmentation issue as
        # ``category='segmentation'`` with ``severity='error'`` for missing.
        assert all(i.category == 'segmentation' for i in a_issues)
        assert any(i.severity == 'error' for i in a_issues)

    def test_extra_segment_flagged(self, zarr_root_factory):
        """Store A has an extra label not in the majority set → flagged as a
        warning."""
        root = zarr_root_factory(store_names=['a', 'b', 'c'])
        _populate_seg(root / 'a.zarr', segments=_seg(_EXTRA_SEG))
        _populate_seg(root / 'b.zarr', segments=_seg(_FULL_SEG))
        _populate_seg(root / 'c.zarr', segments=_seg(_FULL_SEG))

        issues = _check_segmentation_coherence(self._probe_all(root))
        extra = [i for i in issues if i.store_name == 'a' and 'Extra' in i.message]
        assert extra, issues
        assert 'non_standard_label' in extra[0].message
        assert extra[0].severity == 'warning'

    def test_label_value_mismatch_flagged(self, zarr_root_factory):
        """Same *name set*, but label_values swapped → flagged.

        The ``_check_segmentation_coherence`` value-level check fires only
        when a label_value that exists in the reference map is also present
        in the other store's map but points to a different name. To exercise
        that branch we keep the name sets equal (so neither 'Missing' nor
        'Extra' fires) and swap the label_value → name mapping.
        """
        root = zarr_root_factory(store_names=['a', 'b'])
        _populate_seg(
            root / 'a.zarr',
            segments=_seg({'cochlea': 1, 'vestibule': 2}),
        )
        _populate_seg(
            root / 'b.zarr',
            segments=_seg({'cochlea': 2, 'vestibule': 1}),
        )

        issues = _check_segmentation_coherence(self._probe_all(root))
        mismatch = [i for i in issues if 'expected' in i.message]
        assert mismatch, issues
        assert mismatch[0].severity == 'error'

    def test_majority_set_used_as_reference(self, zarr_root_factory):
        """Three stores with set X, one with set Y → Y is flagged, not X."""
        root = zarr_root_factory(store_names=['a', 'b', 'c', 'd'])
        for name in ('a', 'b', 'c'):
            _populate_seg(root / f'{name}.zarr', segments=_seg(_FULL_SEG))
        _populate_seg(
            root / 'd.zarr',
            segments=_seg({'only_here': 1}),
        )

        issues = _check_segmentation_coherence(self._probe_all(root))
        flagged = {i.store_name for i in issues}
        assert 'd' in flagged
        assert flagged.isdisjoint({'a', 'b', 'c'}), flagged

    def test_single_seg_store_no_issues(self, zarr_root_factory):
        """Only one store has a segmentation → no comparison, zero issues."""
        root = zarr_root_factory(store_names=['a', 'b'])
        _populate_seg(root / 'a.zarr', segments=_seg(_FULL_SEG))
        # b has no annotations.

        assert _check_segmentation_coherence(self._probe_all(root)) == []

    def test_tie_break_when_no_majority(self, zarr_root_factory):
        """Two stores with set X, two with set Y → ``Counter.most_common``
        resolves ties by insertion order of the frozenset values, so the
        first-seen set becomes the reference and the other two stores are
        flagged. This pins that deterministic behavior."""
        root = zarr_root_factory(store_names=['a', 'b', 'c', 'd'])
        _populate_seg(root / 'a.zarr', segments=_seg(_FULL_SEG))
        _populate_seg(root / 'b.zarr', segments=_seg(_FULL_SEG))
        _populate_seg(root / 'c.zarr', segments=_seg(_PARTIAL_SEG))
        _populate_seg(root / 'd.zarr', segments=_seg(_PARTIAL_SEG))

        issues = _check_segmentation_coherence(self._probe_all(root))
        flagged = {i.store_name for i in issues}
        # Under a tie, at least one of the two distinct sets is the reference
        # and the other two stores are flagged. Validate the half-and-half
        # shape rather than which specific pair wins.
        assert len(flagged) == 2, flagged
        assert flagged in ({'a', 'b'}, {'c', 'd'})


# ===========================================================================
# Landmark coherence
# ===========================================================================


class TestLandmarkCoherence:
    """Covers _check_landmark_coherence."""

    def _probe_all(self, root: Path) -> list[StoreAnnotationInfo]:
        infos: list[StoreAnnotationInfo] = []
        for store_path in sorted(root.glob('*.zarr')):
            infos.extend(_probe_store_annotations(store_path))
        return infos

    def test_coherent_landmark_labels_no_issues(self, zarr_root_factory):
        root = zarr_root_factory(store_names=['a', 'b', 'c'])
        for name in ('a', 'b', 'c'):
            _populate_lmk(
                root / f'{name}.zarr',
                labels=['round_window', 'oval_window'],
            )
        assert _check_landmark_coherence(self._probe_all(root)) == []

    def test_missing_landmark_flagged(self, zarr_root_factory):
        root = zarr_root_factory(store_names=['a', 'b', 'c'])
        _populate_lmk(root / 'a.zarr', labels=['round_window'])
        _populate_lmk(
            root / 'b.zarr',
            labels=['round_window', 'oval_window'],
        )
        _populate_lmk(
            root / 'c.zarr',
            labels=['round_window', 'oval_window'],
        )

        issues = _check_landmark_coherence(self._probe_all(root))
        a_issues = [i for i in issues if i.store_name == 'a']
        assert any('Missing' in i.message for i in a_issues)
        assert any('oval_window' in i.message for i in a_issues)
        assert all(i.category == 'landmarks' for i in a_issues)

    def test_extra_landmark_flagged(self, zarr_root_factory):
        root = zarr_root_factory(store_names=['a', 'b', 'c'])
        _populate_lmk(
            root / 'a.zarr',
            labels=['round_window', 'oval_window', 'bonus_point'],
        )
        _populate_lmk(
            root / 'b.zarr',
            labels=['round_window', 'oval_window'],
        )
        _populate_lmk(
            root / 'c.zarr',
            labels=['round_window', 'oval_window'],
        )

        issues = _check_landmark_coherence(self._probe_all(root))
        a_issues = [i for i in issues if i.store_name == 'a']
        assert any('Extra' in i.message for i in a_issues)
        assert any('bonus_point' in i.message for i in a_issues)
        assert all(i.severity == 'warning' for i in a_issues)


# ===========================================================================
# audit() — filtering
# ===========================================================================


class TestAuditFiltering:
    """Covers the public audit() function's filter parameters."""

    def test_ontology_filter_restricts_analysis(self, zarr_root_factory):
        """Stores without the target ontology are excluded from comparisons.

        Two stores get an inner-ear-landmarks annotation that disagrees on
        labels. If the audit is filtered to inner-ear-structures only, the
        landmark disagreement must not surface.
        """
        root = zarr_root_factory(store_names=['a', 'b'])
        _populate_seg(root / 'a.zarr', segments=_seg(_FULL_SEG))
        _populate_seg(root / 'b.zarr', segments=_seg(_FULL_SEG))
        _populate_lmk(root / 'a.zarr', labels=['round_window'])
        _populate_lmk(
            root / 'b.zarr',
            labels=['round_window', 'oval_window'],
        )

        issues = audit(
            root,
            ontology_filter='inner-ear-structures',
            console=_silent_console(),
        )
        assert all(i.category != 'landmarks' for i in issues), issues

    def test_store_names_filter(self, zarr_root_factory):
        """store_names filter → only named stores are included in analysis."""
        root = zarr_root_factory(store_names=['a', 'b', 'c'])
        _populate_seg(root / 'a.zarr', segments=_seg(_FULL_SEG))
        _populate_seg(root / 'b.zarr', segments=_seg(_FULL_SEG))
        # c disagrees — but is filtered out.
        _populate_seg(root / 'c.zarr', segments=_seg(_PARTIAL_SEG))

        issues = audit(
            root,
            store_names=['a', 'b'],
            console=_silent_console(),
        )
        assert issues == []

    def test_empty_root_returns_no_issues(self, tmp_path):
        assert audit(tmp_path, console=_silent_console()) == []

    def test_single_store_returns_no_issues(self, zarr_root_factory):
        """A single-store root has nothing to compare against → no issues."""
        root = zarr_root_factory(store_names=['only'])
        _populate_seg(root / 'only.zarr', segments=_seg(_FULL_SEG))
        assert audit(root, console=_silent_console()) == []


# ===========================================================================
# Rendering smoke tests
# ===========================================================================


class TestAuditRendering:
    """Smoke tests for audit report rendering — no visual assertions."""

    def test_audit_renders_empty_report_without_crash(self, tmp_path):
        """Empty root → audit() returns [] without raising."""
        assert audit(tmp_path, console=_silent_console()) == []

    def test_audit_renders_populated_report_without_crash(self, zarr_root_factory):
        """A populated, divergent root renders the issue table cleanly."""
        root = zarr_root_factory(store_names=['a', 'b', 'c'])
        _populate_seg(root / 'a.zarr', segments=_seg(_PARTIAL_SEG))
        _populate_seg(root / 'b.zarr', segments=_seg(_FULL_SEG))
        _populate_seg(root / 'c.zarr', segments=_seg(_FULL_SEG))

        console = _silent_console()
        issues = audit(root, console=console)
        assert issues, 'expected at least one issue for divergent stores'

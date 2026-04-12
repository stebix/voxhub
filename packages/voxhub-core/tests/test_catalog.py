"""Tests for voxhub_core.catalog — store discovery, probing, rendering.

Plan: docs/testing/catalog-staging-audit.md §3

Each test is currently skipped.  Remove the skip marker as tests are
implemented in a downstream worktree.

Fixtures expected:
    - zarr_root_factory
    - create_zarr_store_with_annotations (helper in _core_helpers.py)
"""

import pytest

pytestmark = pytest.mark.skip(
    reason='stub — see docs/testing/catalog-staging-audit.md §3'
)


# ===========================================================================
# discover_zarr_stores
# ===========================================================================


class TestDiscoverZarrStores:
    """Covers voxhub_core.catalog.discover_zarr_stores."""

    def test_discovers_single_store_at_root(self, zarr_root_factory):
        """One foo.zarr directly under the root → one ZarrEntry returned."""
        del zarr_root_factory

    def test_discovers_multiple_stores(self, zarr_root_factory):
        """Three stores → three entries, sorted by name deterministically."""
        del zarr_root_factory

    def test_discovers_nested_stores(self, zarr_root_factory):
        """Stores under subdirectories → pin down and document the current
        discovery behavior (flat-only vs recursive)."""
        del zarr_root_factory

    def test_ignores_non_zarr_directories(self, tmp_path):
        """Root has foo.zarr, bar/ (not a zarr), baz.txt → only foo.zarr
        discovered."""
        del tmp_path

    def test_empty_root_returns_empty_list(self, tmp_path):
        """Fresh tmp_path → empty list."""
        del tmp_path

    def test_nonexistent_root_behavior(self, tmp_path):
        """Path doesn't exist → document current behavior (raises vs empty)."""
        del tmp_path


# ===========================================================================
# ZarrEntry probing (shape, dtype, attrs, dataset_attributes)
# ===========================================================================


class TestProbeZarrEntry:
    """Covers catalog._probe_zarr and ZarrEntry population."""

    def test_probes_shape_and_dtype(self, zarr_root_factory):
        """Valid store → entry.shape and entry.dtype populated from raw/full."""
        del zarr_root_factory

    def test_probes_attributes_dict(self, zarr_root_factory):
        """Valid store → entry.attributes has ImagePositionPatient,
        ImageOrientationPatient, PixelSpacing, computed_slice_spacing_mm."""
        del zarr_root_factory

    def test_probes_source_and_series_directory(self, zarr_root_factory):
        """Store with source_directory and series_directory in attrs →
        both populated on the entry."""
        del zarr_root_factory

    def test_corrupted_store_records_error_not_raise(self, zarr_root_factory):
        """Delete raw/full → entry.error populated, other fields empty.
        The function must NOT raise."""
        del zarr_root_factory

    def test_store_without_spatial_metadata(self, zarr_root_factory):
        """raw/full exists but ImagePosition*/PixelSpacing attrs missing →
        document behavior (error populated vs empty attributes)."""
        del zarr_root_factory

    def test_dataset_attributes_populated_when_present(
        self, zarr_root_factory
    ):
        """Root has dataset_attributes root attr → entry.dataset_attributes
        dict matches."""
        del zarr_root_factory

    def test_dataset_attributes_none_when_absent(self, zarr_root_factory):
        """No dataset attrs → entry.dataset_attributes is None."""
        del zarr_root_factory


# ===========================================================================
# Annotation discovery inside ZarrEntry
# ===========================================================================


class TestDiscoverAnnotations:
    """Covers catalog._discover_annotations (walks annotations/ hierarchy)."""

    def test_no_annotations_group_returns_empty_list(self, zarr_root_factory):
        """Store without annotations/ group → entry.annotations == []."""
        del zarr_root_factory

    def test_single_annotation_discovered(self, zarr_root_factory):
        """One annotator with one instance → entry.annotations has one
        AnnotationEntry with correct path, ontology, ontology_version,
        annotator_id, integrated_at."""
        del zarr_root_factory

    def test_multiple_annotators_discovered(self, zarr_root_factory):
        """Two annotator-scoped subgroups, one instance each → both
        discovered."""
        del zarr_root_factory

    def test_multiple_instances_per_annotator(self, zarr_root_factory):
        """Same annotator has two instance directories → both discovered."""
        del zarr_root_factory

    def test_annotation_missing_ontology_attr_graceful(
        self, zarr_root_factory
    ):
        """Annotation array exists but ontology attr missing → entry has
        ontology == None or empty. Document expected behavior."""
        del zarr_root_factory

    def test_annotation_path_format_matches_convention(
        self, zarr_root_factory
    ):
        """Discovered path is exactly annotations/<annotator>-<nano>/
        <ontology>-<date>-<rand>/data."""
        del zarr_root_factory


# ===========================================================================
# Rich rendering smoke tests
# ===========================================================================


class TestCatalogRendering:
    """Smoke tests for build_tree / build_summary_table.

    These verify the rendering functions don't crash on representative
    inputs. Visual correctness isn't asserted — eyeballing is sufficient
    for display code.
    """

    def test_build_tree_on_empty_root(self, tmp_path):
        """Empty root → returns a rich.tree.Tree instance without crashing."""
        del tmp_path

    def test_build_tree_with_stores_and_annotations(
        self, zarr_root_factory
    ):
        """Populated root → returns a Tree with expected number of children."""
        del zarr_root_factory

    def test_build_summary_table_on_empty_root(self, tmp_path):
        """Empty root → returns a rich.table.Table instance without crashing."""
        del tmp_path

    def test_build_summary_table_with_stores(self, zarr_root_factory):
        """Populated root → returns a Table with N rows matching N stores."""
        del zarr_root_factory

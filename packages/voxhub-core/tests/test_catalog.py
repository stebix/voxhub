"""Tests for voxhub_core.catalog — store discovery, probing, rendering.

Plan: docs/testing/catalog-staging-audit.md §3
"""

import io
import json

import numpy as np
import zarr
from _core_helpers import (
    ORIGIN_LPS,
    SHAPE,
    SPACING_MM,
    create_zarr_store,
    populate_store_annotation,
)
from rich.console import Console
from rich.table import Table
from rich.tree import Tree

from voxhub_core.catalog import (
    AnnotationEntry,
    ZarrEntry,
    build_summary_table,
    build_tree,
    catalog,
    discover_zarr_stores,
)
from voxhub_schema import DatasetAttributes

# -- Canonical dataset_attributes payload for the `dataset_attributes` tests. ---

_DATASET_ATTRS_PAYLOAD: dict[str, object] = {
    'modality': 'CBCT',
    'resolution': {
        'voxel_size': [0.5, 0.5, 0.5],
        'unit': 'mm',
    },
    'origin': 'Acme Hospital',
    'tags': {'study': 'cohort_a'},
}


# ===========================================================================
# discover_zarr_stores
# ===========================================================================


class TestDiscoverZarrStores:
    """Covers voxhub_core.catalog.discover_zarr_stores."""

    def test_discovers_single_store_at_root(self, stores_dir_factory):
        """One foo.zarr directly under the root → one ZarrEntry returned."""
        root = stores_dir_factory(store_names=['foo'])
        entries = discover_zarr_stores(root)
        assert len(entries) == 1
        assert isinstance(entries[0], ZarrEntry)
        assert entries[0].path.name == 'foo.zarr'

    def test_discovers_multiple_stores(self, stores_dir_factory):
        """Three stores → three entries, sorted by path deterministically."""
        root = stores_dir_factory(store_names=['gamma', 'alpha', 'beta'])
        entries = discover_zarr_stores(root)
        names = [e.path.name for e in entries]
        assert names == ['alpha.zarr', 'beta.zarr', 'gamma.zarr']

    def test_discovers_nested_stores(self, stores_dir_factory):
        """`.zarr` directories under subdirectories are discovered — pins
        down the recursive behavior of ``Path.rglob('*.zarr')``."""
        root = stores_dir_factory(store_names=['flat'])
        nested_parent = root / 'sub'
        nested_parent.mkdir()
        (root / 'flat.zarr').rename(nested_parent / 'nested.zarr')
        create_zarr_store(root / 'flat.zarr')

        entries = discover_zarr_stores(root)
        names = sorted(e.path.name for e in entries)
        assert names == ['flat.zarr', 'nested.zarr']

    def test_ignores_non_zarr_directories(self, tmp_path):
        """Root has foo.zarr, bar/ (not a zarr), baz.txt → only foo.zarr."""
        root = tmp_path / 'stores'
        root.mkdir()
        create_zarr_store(root / 'foo.zarr')
        (root / 'bar').mkdir()
        (root / 'baz.txt').write_text('hello')

        entries = discover_zarr_stores(root)
        assert [e.path.name for e in entries] == ['foo.zarr']

    def test_empty_root_returns_empty_list(self, tmp_path):
        """Fresh tmp_path → empty list."""
        assert discover_zarr_stores(tmp_path) == []

    def test_nonexistent_root_returns_empty_list(self, tmp_path):
        """Path doesn't exist → ``rglob`` yields nothing → empty list."""
        # Current behavior: ``Path.rglob`` on a non-existent path yields no
        # results rather than raising, so the function returns [].
        missing = tmp_path / 'does-not-exist'
        assert discover_zarr_stores(missing) == []


# ===========================================================================
# ZarrEntry probing (shape, dtype, attrs, dataset_attributes)
# ===========================================================================


class TestProbeZarrEntry:
    """Covers catalog._probe_zarr and ZarrEntry population."""

    def test_probes_shape_and_dtype(self, stores_dir_factory):
        """Valid store → entry.shape and entry.dtype populated from raw/full."""
        root = stores_dir_factory(store_names=['foo'])
        [entry] = discover_zarr_stores(root)
        assert entry.shape == SHAPE
        assert entry.dtype == 'float32'
        assert entry.error is None

    def test_probes_attributes_dict(self, stores_dir_factory):
        """Entry.attributes carries the DICOM geometry attrs."""
        root = stores_dir_factory(store_names=['foo'])
        [entry] = discover_zarr_stores(root)
        assert list(entry.attributes['ImagePositionPatient']) == ORIGIN_LPS
        assert list(entry.attributes['ImageOrientationPatient']) == [
            1.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
        ]
        assert list(entry.attributes['PixelSpacing']) == [
            SPACING_MM[0],
            SPACING_MM[1],
        ]
        assert entry.attributes['computed_slice_spacing_mm'] == SPACING_MM[2]

    def test_probes_source_and_series_directory(self, stores_dir_factory):
        """When source_directory/series_directory attrs are present they are
        surfaced on the entry."""
        root = stores_dir_factory(store_names=['foo'])
        arr = zarr.open_array(root / 'foo.zarr' / 'raw' / 'full', mode='r+')
        arr.update_attributes(
            {
                'source_directory': '/data/dicom/patient_42',
                'series_directory': 'series_01',
            }
        )
        [entry] = discover_zarr_stores(root)
        assert entry.source_directory == '/data/dicom/patient_42'
        assert entry.series_directory == 'series_01'

    def test_corrupted_store_records_error_not_raise(self, stores_dir_factory):
        """Delete raw/full → entry.error populated, other fields None."""
        root = stores_dir_factory(store_names=['broken'], corrupt=['broken'])
        [entry] = discover_zarr_stores(root)
        assert entry.error is not None
        assert entry.shape is None
        assert entry.dtype is None
        assert entry.attributes == {}
        assert entry.annotations == []

    def test_store_without_spatial_metadata_still_probes(self, stores_dir_factory):
        """raw/full present but spatial attrs missing → probe succeeds (attrs
        just come back empty). The catalog layer does not validate schema —
        only the server layer flags ``Missing spatial metadata``."""
        root = stores_dir_factory(store_names=['bare'])
        arr_path = root / 'bare.zarr' / 'raw' / 'full'
        meta = json.loads((arr_path / 'zarr.json').read_text())
        meta['attributes'] = {}
        (arr_path / 'zarr.json').write_text(json.dumps(meta))

        [entry] = discover_zarr_stores(root)
        assert entry.error is None
        assert entry.shape == SHAPE
        assert entry.attributes == {}

    def test_dataset_attributes_populated_when_present(self, stores_dir_factory):
        """Root has dataset_attributes root attr → entry.dataset_attributes
        matches the payload."""
        root = stores_dir_factory(
            store_names=['foo'],
            dataset_attributes={'foo': _DATASET_ATTRS_PAYLOAD},
        )
        [entry] = discover_zarr_stores(root)
        assert isinstance(entry.dataset_attributes, DatasetAttributes)
        assert entry.dataset_attributes.origin == 'Acme Hospital'
        assert entry.dataset_attributes.tags == {'study': 'cohort_a'}

    def test_dataset_attributes_none_when_absent(self, stores_dir_factory):
        """No dataset attrs → entry.dataset_attributes is None."""
        root = stores_dir_factory(store_names=['foo'])
        [entry] = discover_zarr_stores(root)
        assert entry.dataset_attributes is None


# ===========================================================================
# Annotation discovery inside ZarrEntry
# ===========================================================================


class TestDiscoverAnnotations:
    """Covers catalog._discover_annotations (walks annotations/ hierarchy)."""

    def test_no_annotations_group_returns_empty_list(self, stores_dir_factory):
        """Store without annotations/ group → entry.annotations == []."""
        root = stores_dir_factory(store_names=['foo'])
        [entry] = discover_zarr_stores(root)
        assert entry.annotations == []

    def test_single_annotation_discovered(self, stores_dir_factory):
        """One annotator with one instance → exactly one AnnotationEntry
        with the expected attrs."""
        root = stores_dir_factory(
            store_names=['foo'],
            with_annotations=True,
        )
        [entry] = discover_zarr_stores(root)
        assert len(entry.annotations) == 1
        ann = entry.annotations[0]
        assert isinstance(ann, AnnotationEntry)
        assert ann.annotator_id == 'alice'
        assert ann.ontology == 'inner-ear-structures'
        assert ann.ontology_version == 1
        assert ann.integrated_at == '2026-01-01T00:00:00+00:00'

    def test_multiple_annotators_discovered(self, stores_dir_factory):
        """Two annotator-scoped subgroups → both discovered."""
        root = stores_dir_factory(store_names=['foo'])
        populate_store_annotation(
            root / 'foo.zarr',
            annotator_id='alice',
            nano_id='aaa11111',
        )
        populate_store_annotation(
            root / 'foo.zarr',
            annotator_id='bob',
            nano_id='bbb22222',
            ontology='inner-ear-landmarks',
        )
        [entry] = discover_zarr_stores(root)
        ids = sorted(a.annotator_id for a in entry.annotations)
        assert ids == ['alice', 'bob']

    def test_multiple_instances_per_annotator(self, stores_dir_factory):
        """Same annotator has two instance directories → both discovered."""
        root = stores_dir_factory(store_names=['foo'])
        populate_store_annotation(
            root / 'foo.zarr',
            annotator_id='alice',
            nano_id='aaa11111',
            short_random='ab12',
        )
        populate_store_annotation(
            root / 'foo.zarr',
            annotator_id='alice',
            nano_id='aaa11111',
            short_random='cd34',
        )
        [entry] = discover_zarr_stores(root)
        assert len(entry.annotations) == 2
        paths = sorted(a.path for a in entry.annotations)
        assert paths[0] != paths[1]
        assert all('alice-aaa11111' in p for p in paths)

    def test_annotation_missing_ontology_attr_graceful(self, stores_dir_factory):
        """Annotation array exists but ontology attr missing → entry has
        empty-string ontology and 0 ontology_version (current defaults in
        ``_discover_annotations``)."""
        root = stores_dir_factory(store_names=['foo'])
        populate_store_annotation(
            root / 'foo.zarr',
            omit_ontology_attr=True,
        )
        [entry] = discover_zarr_stores(root)
        assert len(entry.annotations) == 1
        ann = entry.annotations[0]
        assert ann.ontology == ''
        assert ann.ontology_version == 0
        # Other metadata is still recovered.
        assert ann.annotator_id == 'alice'

    def test_annotation_path_format_matches_convention(self, stores_dir_factory):
        """Discovered path is ``annotations/<annotator>-<nano>/<instance>``.

        Note: ``_discover_annotations`` records the *group* path (one level
        above the `data` array), so the convention ends at the instance
        directory, not the data array.
        """
        root = stores_dir_factory(
            store_names=['foo'],
            with_annotations=True,
        )
        [entry] = discover_zarr_stores(root)
        [ann] = entry.annotations
        # populate_store_annotation writes to
        # annotations/alice-xyz45678/inner-ear-structures-20260101-ab12/data.
        assert ann.path == (
            'annotations/alice-xyz45678/inner-ear-structures-20260101-ab12'
        )

    def test_annotator_id_derived_from_slug_not_attrs(self, stores_dir_factory):
        """annotator_id comes from the slug's prefix, not from zarr attrs.

        Guards the catalog.py fix: real integrate code does not write
        annotator_id into zarr array attrs.  Discovery must still report
        the correct annotator_id by parsing the annotator-slug directory.
        """
        root = stores_dir_factory(store_names=['foo'])
        zarr_path = root / 'foo.zarr'
        populate_store_annotation(
            zarr_path,
            annotator_id='dr-smith',
            nano_id='abcd1234',
        )
        # Remove the annotator_id attr to simulate real integrate output.
        store = zarr.open_group(zarr_path, mode='r+')
        arr_path = 'annotations/dr-smith-abcd1234/inner-ear-structures-20260101-ab12/data'
        arr = store[arr_path]
        existing = dict(arr.attrs)
        existing.pop('annotator_id', None)
        arr.attrs.clear()
        arr.update_attributes(existing)

        [entry] = discover_zarr_stores(root)
        [ann] = entry.annotations
        assert ann.annotator_id == 'dr-smith'

    def test_malformed_annotator_slug_skipped(self, stores_dir_factory):
        """A malformed annotator-slug subgroup is skipped (not crashed on).

        Well-formed siblings still appear in the result.
        """
        root = stores_dir_factory(store_names=['foo'])
        zarr_path = root / 'foo.zarr'
        populate_store_annotation(
            zarr_path,
            annotator_id='alice',
            nano_id='abcd1234',
        )
        # Create a malformed annotator directory (missing nano_id separator).
        store = zarr.open_group(zarr_path, mode='r+')
        store.create_array(
            'annotations/not-a-valid-slug-ABCD/inst-20260101-zz99/data',
            data=np.zeros((2, 2, 2), dtype=np.int16),
            overwrite=True,
        )

        [entry] = discover_zarr_stores(root)
        ids = sorted(a.annotator_id for a in entry.annotations)
        assert ids == ['alice']


# ===========================================================================
# Rich rendering smoke tests
# ===========================================================================


class TestCatalogRendering:
    """Smoke tests for build_tree / build_summary_table.

    Rendering correctness is validated by eyeball; the tests here just
    ensure the functions don't crash on representative inputs and return
    the expected rich types.
    """

    def test_build_tree_on_empty_root(self, tmp_path):
        """Empty root → returns a Tree without crashing."""
        tree = build_tree(tmp_path, [])
        assert isinstance(tree, Tree)
        assert tree.children == []

    def test_build_tree_with_stores_and_annotations(self, stores_dir_factory):
        """Populated root → Tree has one child per discovered store."""
        root = stores_dir_factory(
            store_names=['a', 'b', 'c'],
            with_annotations=True,
        )
        entries = discover_zarr_stores(root)
        tree = build_tree(root, entries)
        assert isinstance(tree, Tree)
        assert len(tree.children) == len(entries) == 3

    def test_build_summary_table_on_empty_root(self, tmp_path):
        """Empty root → returns a Table instance."""
        table = build_summary_table([])
        assert isinstance(table, Table)
        assert table.row_count == 0
        del tmp_path

    def test_build_summary_table_with_stores(self, stores_dir_factory):
        """Populated root → Table has one row per store."""
        root = stores_dir_factory(store_names=['a', 'b'])
        entries = discover_zarr_stores(root)
        table = build_summary_table(entries)
        assert isinstance(table, Table)
        assert table.row_count == len(entries) == 2


# ===========================================================================
# catalog() — top-level convenience
# ===========================================================================


class TestCatalogEntrypoint:
    """Smoke tests for the public ``catalog()`` function."""

    def _silent_console(self) -> Console:
        return Console(file=io.StringIO(), record=False, width=160)

    def test_catalog_on_missing_root_returns_empty(self, tmp_path):
        """``catalog()`` on a non-directory path returns [] and prints a
        warning rather than raising."""
        missing = tmp_path / 'nope'
        result = catalog(missing, console=self._silent_console())
        assert result == []

    def test_catalog_on_empty_root_returns_empty(self, tmp_path):
        """``catalog()`` on an empty directory returns []."""
        result = catalog(tmp_path, console=self._silent_console())
        assert result == []

    def test_catalog_returns_entries_on_populated_root(self, stores_dir_factory):
        """``catalog()`` forwards the probed entries to the caller."""
        root = stores_dir_factory(store_names=['a', 'b'])
        result = catalog(root, show_table=True, console=self._silent_console())
        assert [e.path.name for e in result] == ['a.zarr', 'b.zarr']

"""Tests for ontology loading, versioning, and properties."""

from pathlib import Path

import pytest

from voxhub_schema.ontology import (
    UNCONSTRAINED_SEGMENTATION,
    _parse_ontology_file,
    list_ontologies,
    load_ontology,
)

# ===================================================================
# LOADING
# ===================================================================


class TestLoadOntology:
    def test_load_inner_ear_structures(self):
        ont = load_ontology('inner-ear-structures')
        assert ont.name == 'inner-ear-structures'
        assert ont.version == 1
        assert ont.type == 'segmentation'
        assert ont.channel == 'multi'
        assert ont.labels is not None
        assert len(ont.labels) == 4

    def test_load_inner_ear_landmarks(self):
        ont = load_ontology('inner-ear-landmarks')
        assert ont.name == 'inner-ear-landmarks'
        assert ont.version == 1
        assert ont.type == 'landmarks'
        assert ont.points is not None
        assert set(ont.points) == {'round_window', 'oval_window', 'cochlear_apex'}
        assert ont.coordinate_system == 'LPS'

    def test_load_unconstrained(self):
        ont = load_ontology('unconstrained')
        assert ont.name == 'unconstrained'
        assert ont.type == 'segmentation'
        assert ont.labels is None
        assert ont.constraints is not None
        assert 'non_negative_integers' in ont.constraints
        assert 'sequential_from_zero' in ont.constraints
        assert 'background_at_zero' in ont.constraints

    def test_load_fluid_space(self):
        ont = load_ontology('inner-ear-total-fluid-space')
        assert ont.name == 'inner-ear-total-fluid-space'
        assert ont.type == 'segmentation'
        assert ont.channel == 'single'
        assert ont.labels is not None
        assert len(ont.labels) == 2

    def test_pinned_version_loads_exact(self):
        ont = load_ontology('inner-ear-structures', version=1)
        assert ont.version == 1

    def test_nonexistent_name_raises(self):
        with pytest.raises(FileNotFoundError, match='nonexistent'):
            load_ontology('nonexistent')

    def test_nonexistent_version_raises(self):
        with pytest.raises(FileNotFoundError, match='version 99'):
            load_ontology('inner-ear-structures', version=99)

    def test_invalid_type_raises(self, tmp_path: Path):
        """An out-of-vocabulary ``type`` is rejected at parse time."""
        path = tmp_path / 'bad-ontology-v1.yaml'
        path.write_text('name: bad-ontology\nversion: 1\ntype: bogus\n')
        with pytest.raises(ValueError, match='invalid ontology type'):
            _parse_ontology_file(path)


# ===================================================================
# LISTING
# ===================================================================


class TestListOntologies:
    def test_returns_all_shipped_ontologies(self):
        all_onts = list_ontologies()
        names = {o.name for o in all_onts}
        assert 'inner-ear-structures' in names
        assert 'inner-ear-landmarks' in names
        assert 'unconstrained' in names
        assert 'inner-ear-total-fluid-space' in names

    def test_sorted_by_name_and_version(self):
        all_onts = list_ontologies()
        keys = [(o.name, o.version) for o in all_onts]
        assert keys == sorted(keys)


# ===================================================================
# ONTOLOGY PROPERTIES
# ===================================================================


class TestOntologyProperties:
    def test_constrained_is_not_unconstrained(self):
        ont = load_ontology('inner-ear-structures')
        assert ont.is_unconstrained is False

    def test_unconstrained_is_unconstrained(self):
        ont = load_ontology('unconstrained')
        assert ont.is_unconstrained is True

    def test_constrained_label_map(self):
        ont = load_ontology('inner-ear-structures')
        lm = ont.label_map
        assert lm is not None
        assert lm == {
            0: 'background',
            1: 'cochlea',
            2: 'vestibule',
            3: 'semicircular_canals',
        }

    def test_unconstrained_label_map_is_none(self):
        ont = load_ontology('unconstrained')
        assert ont.label_map is None

    def test_landmark_ontology_has_no_labels(self):
        ont = load_ontology('inner-ear-landmarks')
        assert ont.labels is None
        assert ont.label_map is None

    def test_module_level_unconstrained_constant(self):
        """``UNCONSTRAINED_SEGMENTATION`` is loaded at import time."""
        assert UNCONSTRAINED_SEGMENTATION.name == 'unconstrained'
        assert UNCONSTRAINED_SEGMENTATION.is_unconstrained is True

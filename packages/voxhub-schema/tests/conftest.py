"""Shared fixtures for voxhub-schema tests."""

import pytest
from _schema_helpers import ORIGIN_LPS, SHAPE, SPACE_DIRECTIONS, SPACING_MM

from voxhub_schema.ontology import load_ontology

# -- Ontology fixtures -------------------------------------------------------


@pytest.fixture
def inner_ear_ontology():
    """The inner-ear-structures segmentation ontology."""
    return load_ontology('inner-ear-structures')


@pytest.fixture
def landmark_ontology():
    """The inner-ear-landmarks landmark ontology."""
    return load_ontology('inner-ear-landmarks')


@pytest.fixture
def unconstrained_ontology():
    """The unconstrained segmentation ontology."""
    return load_ontology('unconstrained')


@pytest.fixture
def fluid_space_ontology():
    """The inner-ear-total-fluid-space single-channel ontology."""
    return load_ontology('inner-ear-total-fluid-space')


# -- Manifest fixtures -------------------------------------------------------


@pytest.fixture
def manifest_entry():
    """Plain spatial metadata (``VolumeMetadata``) for the canonical volume.

    The unified validators take the volume's spatial metadata as a plain
    mapping so the schema package stays free of zarr / manifest coupling.
    """
    return {
        'shape': list(SHAPE),
        'spacing_mm': SPACING_MM,
        'origin_lps': ORIGIN_LPS,
        'space_directions': SPACE_DIRECTIONS,
    }

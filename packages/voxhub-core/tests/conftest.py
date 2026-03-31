"""Shared fixtures for voxhub-core tests."""

import pytest

from voxhub_schema.ontology import load_ontology

# -- Ontology fixtures -------------------------------------------------------


@pytest.fixture
def inner_ear_ontology():
    return load_ontology('inner-ear-structures')


@pytest.fixture
def landmark_ontology():
    return load_ontology('inner-ear-landmarks')


@pytest.fixture
def unconstrained_ontology():
    return load_ontology('unconstrained')

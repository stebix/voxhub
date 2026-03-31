"""Ontology definitions and loading from versioned YAML files.

Ontologies are YAML data files shipped with this package in the
``ontologies/`` directory.  Filenames encode the version:
``<name>-v<N>.yaml``.
"""

from __future__ import annotations

import re
from importlib import resources
from pathlib import Path
from typing import Literal

import attrs
import yaml

type OntologyType = Literal['segmentation', 'landmarks']
type ChannelMode = Literal['single', 'multi']

_FILENAME_RE = re.compile(r'^(.+)-v(\d+)\.yaml$')


@attrs.define
class OntologyLabel:
    """A single label in a segmentation ontology."""

    value: int
    name: str


@attrs.define
class Ontology:
    """A versioned ontology definition.

    Parameters
    ----------
    name : str
        Human-readable ontology name (e.g. ``'inner-ear-structures'``).
    version : int
        Monotonically increasing version number.
    type : OntologyType
        ``'segmentation'`` or ``'landmarks'``.
    channel : ChannelMode | None
        ``'single'`` or ``'multi'`` for segmentations, ``None`` for
        landmarks.
    labels : list[OntologyLabel] | None
        Label definitions for segmentation ontologies.  ``None`` for
        unconstrained ontologies or landmarks.
    points : list[str] | None
        Named points for landmark ontologies.
    coordinate_system : str | None
        Expected coordinate system for landmarks (e.g. ``'LPS'``).
    constraints : list[str] | None
        Structural constraints for unconstrained ontologies.
    """

    name: str
    version: int
    type: OntologyType
    channel: ChannelMode | None = None
    labels: list[OntologyLabel] | None = None
    points: list[str] | None = None
    coordinate_system: str | None = None
    constraints: list[str] | None = None

    @property
    def is_unconstrained(self) -> bool:
        """Whether this ontology has unconstrained labels."""
        return self.type == 'segmentation' and self.labels is None

    @property
    def label_map(self) -> dict[int, str] | None:
        """Map from label value to name, or ``None`` if unconstrained."""
        if self.labels is None:
            return None
        return {label.value: label.name for label in self.labels}


def _ontology_dir() -> Path:
    """Return the path to the shipped ontology YAML directory."""
    return Path(str(resources.files('voxhub_schema') / 'ontologies'))


def _parse_ontology_file(path: Path) -> Ontology:
    """Parse a single ontology YAML file into an ``Ontology`` object."""
    with open(path) as f:
        data = yaml.safe_load(f)

    labels: list[OntologyLabel] | None = None
    if data.get('labels') is not None:
        labels = [
            OntologyLabel(value=int(k), name=str(v)) for k, v in data['labels'].items()
        ]

    return Ontology(
        name=data['name'],
        version=int(data['version']),
        type=data['type'],
        channel=data.get('channel'),
        labels=labels,
        points=data.get('points'),
        coordinate_system=data.get('coordinate_system'),
        constraints=data.get('constraints'),
    )


def load_ontology(name: str, version: int | None = None) -> Ontology:
    """Load an ontology by name, optionally pinned to a version.

    Parameters
    ----------
    name : str
        Ontology name (e.g. ``'inner-ear-structures'``).
    version : int | None
        Specific version to load.  If ``None``, returns the latest.

    Returns
    -------
    Ontology

    Raises
    ------
    FileNotFoundError
        If no matching ontology file exists.
    """
    ont_dir = _ontology_dir()

    if version is not None:
        path = ont_dir / f'{name}-v{version}.yaml'
        if not path.exists():
            msg = f'Ontology {name!r} version {version} not found (expected {path.name})'
            raise FileNotFoundError(msg)
        return _parse_ontology_file(path)

    # Find the latest version.
    candidates: list[tuple[int, Path]] = []
    for path in ont_dir.iterdir():
        match = _FILENAME_RE.match(path.name)
        if match and match.group(1) == name:
            candidates.append((int(match.group(2)), path))

    if not candidates:
        msg = f'No ontology files found for {name!r}'
        raise FileNotFoundError(msg)

    candidates.sort(key=lambda t: t[0])
    return _parse_ontology_file(candidates[-1][1])


def list_ontologies() -> list[Ontology]:
    """Load and return all shipped ontologies.

    Returns
    -------
    list[Ontology]
        All ontologies, sorted by ``(name, version)``.
    """
    ont_dir = _ontology_dir()
    ontologies: list[Ontology] = []

    for path in ont_dir.iterdir():
        if _FILENAME_RE.match(path.name):
            ontologies.append(_parse_ontology_file(path))

    ontologies.sort(key=lambda o: (o.name, o.version))
    return ontologies


UNCONSTRAINED_SEGMENTATION: Ontology = load_ontology('unconstrained')
"""Generic fallback ontology for unconstrained segmentations."""

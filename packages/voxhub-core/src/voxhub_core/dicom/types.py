# ruff: noqa: F722, N812
"""Type definitions for the DICOM pipeline."""

from pathlib import Path
from typing import Any

import attrs
from jaxtyping import Float
from numpy import ndarray as Array

type DicomTree = dict[str, list[Path] | DicomTree]
type ActualizedDicomTree = dict[str, 'DicomVolume | ActualizedDicomTree']


@attrs.define
class DicomVolume:
    """A 3D DICOM volume with associated metadata."""

    volume: Float[Array, 'z y x']
    metadata: dict[str, Any]

"""DICOM directory tree parsing.

Recursively discovers DICOM leaf directories, validates slice
contiguity, and produces a :data:`DicomTree` structure.
"""

import itertools
import warnings
from pathlib import Path

from .types import DicomTree


def _are_consecutive(numbers: list[int]) -> bool:
    """Return True if *numbers* form a contiguous sequence."""
    if len(numbers) < 2:
        return True
    sorted_numbers = sorted(numbers)
    return all(b - a == 1 for a, b in itertools.pairwise(sorted_numbers))


def parse_dicom_directory(
    path: Path,
    *,
    verbose: bool = False,
) -> list[Path]:
    """Parse a leaf directory of DICOM files.

    Validates that filenames are integer slice numbers forming a
    contiguous sequence.

    Parameters
    ----------
    path : Path
        Directory containing ``.dcm`` files.
    verbose : bool
        Print progress information.

    Returns
    -------
    list[Path]
        Sorted list of ``.dcm`` paths.

    Raises
    ------
    FileNotFoundError
        If *path* does not exist or is not a directory.
    ValueError
        If the directory has no ``.dcm`` files, non-integer stems,
        or non-contiguous slices.
    """
    if not path.is_dir():
        msg = f"'{path}' is not an existing directory"
        raise FileNotFoundError(msg)

    dcm_files = sorted(
        (
            item
            for item in path.iterdir()
            if item.is_file() and item.suffix.lower() == '.dcm'
        ),
        key=lambda p: int(p.stem),
    )

    if not dcm_files:
        msg = f"No .dcm files found in '{path}'"
        raise ValueError(msg)

    try:
        slice_numbers = [int(p.stem) for p in dcm_files]
    except ValueError as exc:
        msg = f"Non-integer DICOM filename stem in '{path}'"
        raise ValueError(msg) from exc

    if not _are_consecutive(slice_numbers):
        missing = set(range(min(slice_numbers), max(slice_numbers) + 1)) - set(
            slice_numbers
        )
        msg = (
            f"DICOM slice numbers in '{path}' are not contiguous. "
            f'Missing slices: {sorted(missing)}'
        )
        raise ValueError(msg)

    if verbose:
        print(f"Found {len(dcm_files)} contiguous DICOM slices in '{path}'")

    return dcm_files


def parse_dicom_tree(
    path: Path,
    *,
    verbose: bool = False,
) -> DicomTree:
    """Recursively walk a directory tree and parse DICOM leaf directories.

    Each subdirectory of *path* is classified as:

    - **leaf** (contains only ``.dcm`` files) -- parsed via
      :func:`parse_dicom_directory`.
    - **intermediate** (contains only subdirectories) -- recursed into.
    - **mixed** (``.dcm`` files *and* subdirectories) -- raises
      :class:`ValueError`.
    - **empty / irrelevant** -- silently skipped.

    Invalid DICOM leaves (e.g. non-contiguous slices) are skipped with a
    warning instead of raising.

    Parameters
    ----------
    path : Path
        Root directory to scan.
    verbose : bool
        Print progress information.

    Returns
    -------
    DicomTree
        Nested dict whose leaf values are ``list[Path]``.

    Raises
    ------
    FileNotFoundError
        If *path* is not an existing directory.
    ValueError
        If a subdirectory mixes ``.dcm`` files and subdirectories.
    """
    if not path.is_dir():
        msg = f"'{path}' is not an existing directory"
        raise FileNotFoundError(msg)

    result: DicomTree = {}

    for subdir in sorted(path.iterdir()):
        if not subdir.is_dir():
            continue

        dcm_files = [
            item
            for item in subdir.iterdir()
            if item.is_file() and item.suffix.lower() == '.dcm'
        ]
        subdirs = [item for item in subdir.iterdir() if item.is_dir()]

        has_dcm = len(dcm_files) > 0
        has_subdirs = len(subdirs) > 0

        if has_dcm and has_subdirs:
            msg = (
                f"Mixed directory '{subdir}' contains both .dcm files and subdirectories"
            )
            raise ValueError(msg)

        if has_dcm:
            try:
                result[subdir.name] = parse_dicom_directory(subdir, verbose=verbose)
            except ValueError as exc:
                warnings.warn(f"Skipping '{subdir.name}': {exc}", stacklevel=2)
        elif has_subdirs:
            result[subdir.name] = parse_dicom_tree(subdir, verbose=verbose)

    return result

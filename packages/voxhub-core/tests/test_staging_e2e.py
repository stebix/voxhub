"""End-to-end tests for voxhub_core.staging.stage and extract_spatial_metadata.

The low-level NRRD writer (_write_nrrd_raw) is already covered by
test_staging.py; this file covers the public stage() entrypoint and the
extract_spatial_metadata helper that feeds it.

Plan: docs/testing/catalog-staging-audit.md §4
"""

import hashlib
import io
import json
import re
from pathlib import Path

import nrrd
import numpy as np
import pytest
import zarr
from _core_helpers import (
    ORIGIN_LPS,
    SHAPE,
    SPACING_MM,
    create_zarr_store,
)
from rich.console import Console

from voxhub_core.staging import extract_spatial_metadata, stage

_SHA256_HEX = re.compile(r'^sha256:[0-9a-f]{64}$')


def _silent_console() -> Console:
    return Console(file=io.StringIO(), record=False, width=160)


def _stage(zarr_root: Path, staging_dir: Path, **kwargs) -> dict:
    """Invoke ``stage()`` with a silent console."""
    return stage(zarr_root, staging_dir, console=_silent_console(), **kwargs)


def _strip_spatial_attrs(zarr_path: Path, keys: tuple[str, ...]) -> None:
    """Remove spatial attrs from ``raw/full`` by rewriting zarr.json."""
    arr_path = zarr_path / 'raw' / 'full'
    meta = json.loads((arr_path / 'zarr.json').read_text())
    for key in keys:
        meta.get('attributes', {}).pop(key, None)
    (arr_path / 'zarr.json').write_text(json.dumps(meta))


# ===========================================================================
# extract_spatial_metadata
# ===========================================================================


class TestExtractSpatialMetadata:
    """Covers voxhub_core.staging.extract_spatial_metadata."""

    def test_canonical_axis_aligned_metadata(self, zarr_root_factory):
        """Identity-like orientation → origin/directions/spacing match the
        canonical values written by ``create_zarr_store``."""
        root = zarr_root_factory(store_names=['foo'])
        arr = zarr.open_array(root / 'foo.zarr' / 'raw' / 'full', mode='r')
        origin, directions, spacing = extract_spatial_metadata(dict(arr.attrs))

        np.testing.assert_allclose(origin, ORIGIN_LPS)
        # With row=[1,0,0] col=[0,1,0], slice_normal=[0,0,1], the directions
        # matrix rows are [slice_normal*zs, col*ys, row*xs].
        expected = np.array(
            [
                [0.0, 0.0, SPACING_MM[2]],
                [0.0, SPACING_MM[1], 0.0],
                [SPACING_MM[0], 0.0, 0.0],
            ]
        )
        np.testing.assert_allclose(directions, expected)
        assert spacing == [SPACING_MM[2], SPACING_MM[0], SPACING_MM[1]]

    def test_oblique_orientation(self, zarr_root_factory):
        """Non-identity ImageOrientationPatient → direction rows reflect the
        rotation."""
        root = zarr_root_factory(store_names=['foo'])
        theta = np.pi / 6  # 30 deg rotation around slice axis
        iop = [np.cos(theta), np.sin(theta), 0.0, -np.sin(theta), np.cos(theta), 0.0]
        arr = zarr.open_array(root / 'foo.zarr' / 'raw' / 'full', mode='r+')
        arr.update_attributes({'ImageOrientationPatient': iop})

        origin, directions, _ = extract_spatial_metadata(dict(arr.attrs))
        del origin
        # slice_normal should still be ±[0,0,1].
        np.testing.assert_allclose(
            directions[0] / np.linalg.norm(directions[0]),
            [0.0, 0.0, 1.0],
            atol=1e-12,
        )
        # row/col direction rows should encode the rotation.
        np.testing.assert_allclose(
            directions[2] / SPACING_MM[0],
            iop[:3],
        )
        np.testing.assert_allclose(
            directions[1] / SPACING_MM[1],
            iop[3:6],
        )

    def test_anisotropic_spacing(self, zarr_root_factory):
        """PixelSpacing ≠ computed_slice_spacing_mm → returned spacing_mm
        mixes row/col/slice correctly."""
        root = zarr_root_factory(store_names=['foo'])
        arr = zarr.open_array(root / 'foo.zarr' / 'raw' / 'full', mode='r+')
        arr.update_attributes(
            {
                'PixelSpacing': [0.3, 0.7],
                'computed_slice_spacing_mm': 1.2,
            }
        )

        _origin, _dirs, spacing = extract_spatial_metadata(dict(arr.attrs))
        # spacing_mm = [slice_spacing, row_spacing, col_spacing]
        assert spacing == [1.2, 0.3, 0.7]

    def test_missing_image_position_patient_raises_keyerror(self, zarr_root_factory):
        root = zarr_root_factory(store_names=['foo'])
        _strip_spatial_attrs(root / 'foo.zarr', ('ImagePositionPatient',))
        arr = zarr.open_array(root / 'foo.zarr' / 'raw' / 'full', mode='r')
        with pytest.raises(KeyError):
            extract_spatial_metadata(dict(arr.attrs))

    def test_missing_pixel_spacing_raises_keyerror(self, zarr_root_factory):
        root = zarr_root_factory(store_names=['foo'])
        _strip_spatial_attrs(root / 'foo.zarr', ('PixelSpacing',))
        arr = zarr.open_array(root / 'foo.zarr' / 'raw' / 'full', mode='r')
        with pytest.raises(KeyError):
            extract_spatial_metadata(dict(arr.attrs))

    def test_missing_computed_slice_spacing_raises_keyerror(self, zarr_root_factory):
        root = zarr_root_factory(store_names=['foo'])
        _strip_spatial_attrs(root / 'foo.zarr', ('computed_slice_spacing_mm',))
        arr = zarr.open_array(root / 'foo.zarr' / 'raw' / 'full', mode='r')
        with pytest.raises(KeyError):
            extract_spatial_metadata(dict(arr.attrs))

    def test_returns_numpy_arrays_for_origin_and_directions(self, zarr_root_factory):
        """The function's contract is np.ndarray for origin and directions."""
        root = zarr_root_factory(store_names=['foo'])
        arr = zarr.open_array(root / 'foo.zarr' / 'raw' / 'full', mode='r')
        origin, directions, spacing = extract_spatial_metadata(dict(arr.attrs))
        assert isinstance(origin, np.ndarray)
        assert isinstance(directions, np.ndarray)
        assert isinstance(spacing, list)


# ===========================================================================
# stage() — single store happy path
# ===========================================================================


class TestStageSingleStore:
    """Covers voxhub_core.staging.stage — single-store happy path."""

    def test_creates_staging_dir_with_store_subdirectory(
        self, zarr_root_factory, tmp_path
    ):
        root = zarr_root_factory(store_names=['foo'])
        staging = tmp_path / 'staging'
        _stage(root, staging, store_names=['foo'])
        assert (staging / 'foo').is_dir()

    def test_writes_raw_nrrd(self, zarr_root_factory, tmp_path):
        root = zarr_root_factory(store_names=['foo'])
        staging = tmp_path / 'staging'
        _stage(root, staging, store_names=['foo'])
        nrrd_path = staging / 'foo' / 'raw.nrrd'
        assert nrrd_path.is_file()
        data, header = nrrd.read(str(nrrd_path))
        assert data.size > 0
        assert header['space'] == 'left-posterior-superior'

    def test_nrrd_data_matches_zarr_data(self, zarr_root_factory, tmp_path):
        """nrrd.read on _write_nrrd_raw output returns the transpose of the
        source ZYX array (see axis-order note in test_staging.py)."""
        root = zarr_root_factory(store_names=['foo'])
        staging = tmp_path / 'staging'
        _stage(root, staging, store_names=['foo'])

        src_arr = zarr.open_array(root / 'foo.zarr' / 'raw' / 'full', mode='r')
        src = src_arr[:]
        read_back, _header = nrrd.read(str(staging / 'foo' / 'raw.nrrd'))
        np.testing.assert_array_equal(read_back, np.asarray(src).T)

    def test_manifest_structure(self, zarr_root_factory, tmp_path):
        root = zarr_root_factory(store_names=['foo'])
        staging = tmp_path / 'staging'
        manifest = _stage(root, staging, store_names=['foo'])

        assert list(manifest) == ['foo']
        entry = manifest['foo']
        required = {
            'zarr_path',
            'raw_checksum',
            'shape',
            'origin_lps',
            'space_directions',
            'spacing_mm',
        }
        assert required.issubset(entry)
        assert entry['shape'] == list(SHAPE)
        assert entry['origin_lps'] == list(ORIGIN_LPS)

    def test_raw_checksum_is_sha256_hex(self, zarr_root_factory, tmp_path):
        root = zarr_root_factory(store_names=['foo'])
        staging = tmp_path / 'staging'
        manifest = _stage(root, staging, store_names=['foo'])
        checksum = manifest['foo']['raw_checksum']
        assert _SHA256_HEX.match(checksum), checksum

    def test_checksum_matches_actual_file_contents(self, zarr_root_factory, tmp_path):
        root = zarr_root_factory(store_names=['foo'])
        staging = tmp_path / 'staging'
        manifest = _stage(root, staging, store_names=['foo'])

        actual = hashlib.sha256((staging / 'foo' / 'raw.nrrd').read_bytes()).hexdigest()
        assert manifest['foo']['raw_checksum'] == f'sha256:{actual}'


# ===========================================================================
# stage() — multi-store
# ===========================================================================


class TestStageMultipleStores:
    """Multi-store filtering and selection."""

    def test_stages_all_stores_when_names_none(self, zarr_root_factory, tmp_path):
        root = zarr_root_factory(store_names=['a', 'b', 'c'])
        staging = tmp_path / 'staging'
        manifest = _stage(root, staging, store_names=None)
        assert sorted(manifest) == ['a', 'b', 'c']
        for name in ('a', 'b', 'c'):
            assert (staging / name / 'raw.nrrd').is_file()

    def test_stages_subset_when_names_provided(self, zarr_root_factory, tmp_path):
        root = zarr_root_factory(store_names=['a', 'b', 'c'])
        staging = tmp_path / 'staging'
        manifest = _stage(root, staging, store_names=['a', 'c'])
        assert sorted(manifest) == ['a', 'c']
        assert (staging / 'a').is_dir()
        assert (staging / 'c').is_dir()
        assert not (staging / 'b').exists()

    def test_nonexistent_store_name_raises(self, zarr_root_factory, tmp_path):
        """All requested names missing → FileNotFoundError.

        Pinned behavior per the current implementation: when name-filtering
        leaves zero stores, ``stage()`` raises, not silently-skips.
        """
        root = zarr_root_factory(store_names=['a'])
        staging = tmp_path / 'staging'
        with pytest.raises(FileNotFoundError, match='No matching stores'):
            _stage(root, staging, store_names=['missing'])

    def test_partial_name_match_stages_intersection(self, zarr_root_factory, tmp_path):
        """Mix of valid and missing names → only valid ones are staged."""
        root = zarr_root_factory(store_names=['a', 'b'])
        staging = tmp_path / 'staging'
        manifest = _stage(root, staging, store_names=['a', 'missing'])
        assert list(manifest) == ['a']
        assert not (staging / 'missing').exists()


# ===========================================================================
# stage() — compression flag
# ===========================================================================


class TestStageCompressionFlag:
    """Covers the --compress flag in stage()."""

    def test_compressed_nrrd_raw_block_is_gzipped(self, zarr_root_factory, tmp_path):
        """compress=True → the raw data block starts with gzip magic.

        NRRD files begin with an ASCII header; compression applies to the
        payload following the blank-line header terminator.
        """
        root = zarr_root_factory(store_names=['foo'])
        staging = tmp_path / 'staging'
        _stage(root, staging, store_names=['foo'], compress=True)

        payload = (staging / 'foo' / 'raw.nrrd').read_bytes()
        blank_line = payload.index(b'\n\n')
        raw_block = payload[blank_line + 2 :]
        assert raw_block[:2] == b'\x1f\x8b', raw_block[:4]

    def test_compressed_and_uncompressed_data_equivalent(
        self, zarr_root_factory, tmp_path
    ):
        """Staging the same store compressed vs raw → identical data once
        decoded by nrrd.read."""
        root = zarr_root_factory(store_names=['foo'])
        staging_raw = tmp_path / 'staging_raw'
        staging_gz = tmp_path / 'staging_gz'
        _stage(root, staging_raw, store_names=['foo'], compress=False)
        _stage(root, staging_gz, store_names=['foo'], compress=True)

        a, _ = nrrd.read(str(staging_raw / 'foo' / 'raw.nrrd'))
        b, _ = nrrd.read(str(staging_gz / 'foo' / 'raw.nrrd'))
        np.testing.assert_array_equal(a, b)


# ===========================================================================
# stage() — force flag and error paths
# ===========================================================================


class TestStageForceAndErrors:
    """Covers force flag and error propagation."""

    def test_refuses_to_overwrite_without_force(self, zarr_root_factory, tmp_path):
        root = zarr_root_factory(store_names=['foo'])
        staging = tmp_path / 'staging'
        staging.mkdir()
        (staging / 'preexisting.txt').write_text('hi')

        with pytest.raises(FileExistsError, match='already exists'):
            _stage(root, staging, store_names=['foo'])

    def test_force_overwrites_existing_staging_contents(
        self, zarr_root_factory, tmp_path
    ):
        """force=True → stage succeeds even when staging_dir already exists.

        ``stage()`` does not currently *erase* old files on force — it just
        bypasses the existence check. This test pins that exact semantics.
        """
        root = zarr_root_factory(store_names=['foo'])
        staging = tmp_path / 'staging'
        staging.mkdir()
        preexisting = staging / 'preexisting.txt'
        preexisting.write_text('hi')

        manifest = _stage(root, staging, store_names=['foo'], force=True)
        assert 'foo' in manifest
        assert (staging / 'foo' / 'raw.nrrd').is_file()
        assert preexisting.exists()

    def test_missing_zarr_root_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match='Zarr root directory not found'):
            _stage(tmp_path / 'missing', tmp_path / 'staging')

    def test_empty_zarr_root_raises(self, tmp_path):
        """Directory exists but contains no .zarr stores → FileNotFoundError."""
        empty = tmp_path / 'empty'
        empty.mkdir()
        with pytest.raises(FileNotFoundError, match=r'No \.zarr stores found'):
            _stage(empty, tmp_path / 'staging')

    def test_store_without_raw_full_is_skipped_not_raised(self, tmp_path):
        """A corrupted store (raw/full missing) is reported as an error by
        the catalog probe. ``stage()`` logs and skips rather than raising,
        so the manifest simply omits the broken store.
        """
        root = tmp_path / 'stores'
        root.mkdir()
        good = create_zarr_store(root / 'good.zarr')
        broken = create_zarr_store(root / 'broken.zarr')
        import shutil

        shutil.rmtree(broken / 'raw')
        del good

        manifest = _stage(root, tmp_path / 'staging')
        assert list(manifest) == ['good']

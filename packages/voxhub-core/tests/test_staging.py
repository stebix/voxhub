"""Tests for the fast NRRD writer in extraction.py.

Covers correctness (equivalence with pynrrd) and performance (benchmarks).

Axis-order note
---------------
pynrrd >= 1.0 writes data in F-order per the NRRD spec (first listed size
varies fastest in the file) and on read returns a C-order numpy array with
shape equal to the header sizes.  For a ZYX numpy array of shape (Z, Y, X):

- ``nrrd.write`` records sizes = [Z, Y, X]; ``nrrd.read`` returns shape (Z, Y, X)
- ``write_nrrd_raw`` records sizes = [X, Y, Z] (reversed), so
  ``nrrd.read`` returns shape (X, Y, Z) = data.T

The invariant that proves both writers encode the same volume:

    data_fast == data_ref.T       (equivalently, data_fast == data.T)
"""

import timeit

import nrrd
import numpy as np
import pytest

from voxhub_core.extraction import build_raw_volume_header, write_nrrd_raw

# -- Shared geometry ---------------------------------------------------------

_ORIGIN = [-5.0, -6.0, -7.0]
_SPACE_DIRECTIONS = [
    [0.0, 0.0, 0.5],
    [0.0, 0.5, 0.0],
    [0.5, 0.0, 0.0],
]

# ~4 M voxels — large enough to surface pynrrd's Python-level serialisation overhead.
_BENCH_SHAPE = (64, 256, 256)


def _make_header() -> dict:
    return {
        'space': 'left-posterior-superior',
        'space origin': _ORIGIN,
        'space directions': _SPACE_DIRECTIONS,
    }


# -- Module-scoped benchmark fixtures ----------------------------------------


@pytest.fixture(scope='module')
def bench_data():
    rng = np.random.default_rng(42)
    return rng.standard_normal(_BENCH_SHAPE).astype('float32')


@pytest.fixture(scope='module')
def bench_header():
    return build_raw_volume_header(
        _BENCH_SHAPE,
        np.array(_ORIGIN),
        np.array(_SPACE_DIRECTIONS),
    )


# ===================================================================
# EQUIVALENCE: hand-rolled vs pynrrd
# ===================================================================


class TestNrrdWriterEquivalence:
    """write_nrrd_raw and nrrd.write encode the same volume data.

    pynrrd reads NRRD files using the spec's F-order convention (first listed
    size varies fastest in the file), so reading a write_nrrd_raw file (which
    has reversed sizes) yields the transpose of the original array:

        data_fast == data_ref.T
    """

    @pytest.mark.parametrize(
        'dtype',
        ['float32', 'float64', 'int16', 'uint8', 'int32', 'uint16'],
    )
    def test_data_is_transpose_of_pynrrd_output(self, tmp_path, dtype):
        """Reading fast-writer output gives the transpose of pynrrd output."""
        rng = np.random.default_rng(0)
        data = rng.integers(0, 10, size=(8, 10, 12)).astype(dtype)
        header = _make_header()

        p_fast = tmp_path / 'fast.nrrd'
        p_ref = tmp_path / 'ref.nrrd'
        write_nrrd_raw(p_fast, data, header)
        nrrd.write(str(p_ref), data, header)

        data_fast, _ = nrrd.read(str(p_fast))
        data_ref, _ = nrrd.read(str(p_ref))

        np.testing.assert_array_equal(data_fast, data_ref.T)

    def test_shape_is_reversed_in_header(self, tmp_path):
        """write_nrrd_raw lists sizes in reversed (fastest-axis-first) order."""
        data = np.zeros((8, 10, 12), dtype='float32')
        header = _make_header()

        p_fast = tmp_path / 'fast.nrrd'
        p_ref = tmp_path / 'ref.nrrd'
        write_nrrd_raw(p_fast, data, header)
        nrrd.write(str(p_ref), data, header)

        data_fast, _ = nrrd.read(str(p_fast))
        data_ref, _ = nrrd.read(str(p_ref))

        assert data_fast.shape == data_ref.shape[::-1]

    def test_space_origin_identical(self, tmp_path):
        """Space origin (not axis-dependent) is written identically by both writers."""
        data = np.zeros((8, 10, 12), dtype='float32')
        header = _make_header()

        p_fast = tmp_path / 'fast.nrrd'
        p_ref = tmp_path / 'ref.nrrd'
        write_nrrd_raw(p_fast, data, header)
        nrrd.write(str(p_ref), data, header)

        _, h_fast = nrrd.read(str(p_fast))
        _, h_ref = nrrd.read(str(p_ref))

        assert h_fast['space'] == h_ref['space']
        np.testing.assert_allclose(h_fast['space origin'], h_ref['space origin'])

    def test_space_directions_reversed(self, tmp_path):
        """Space direction rows are reversed to match the reversed sizes."""
        data = np.zeros((8, 10, 12), dtype='float32')
        header = _make_header()

        p_fast = tmp_path / 'fast.nrrd'
        p_ref = tmp_path / 'ref.nrrd'
        write_nrrd_raw(p_fast, data, header)
        nrrd.write(str(p_ref), data, header)

        _, h_fast = nrrd.read(str(p_fast))
        _, h_ref = nrrd.read(str(p_ref))

        np.testing.assert_allclose(
            h_fast['space directions'],
            h_ref['space directions'][::-1],
        )

    def test_compressed_data_is_transpose_of_pynrrd_output(self, tmp_path):
        """Gzip output produces the same transposed equivalence as uncompressed."""
        rng = np.random.default_rng(1)
        data = rng.integers(0, 5, size=(8, 10, 12)).astype('int16')
        header = _make_header()

        p_fast = tmp_path / 'fast.nrrd'
        p_ref = tmp_path / 'ref.nrrd'
        write_nrrd_raw(p_fast, data, header, compress=True)
        nrrd.write(str(p_ref), data, {**header, 'encoding': 'gzip'})

        data_fast, _ = nrrd.read(str(p_fast))
        data_ref, _ = nrrd.read(str(p_ref))

        np.testing.assert_array_equal(data_fast, data_ref.T)

    def test_custom_key_passthrough(self, tmp_path):
        """Non-standard header keys are stored as custom fields and survive read."""
        data = np.zeros((4, 4, 4), dtype='float32')
        header = {**_make_header(), 'MyCustomField': 'hello'}

        p_fast = tmp_path / 'fast.nrrd'
        write_nrrd_raw(p_fast, data, header)
        _, h_fast = nrrd.read(str(p_fast))

        assert h_fast.get('MyCustomField') == 'hello'

    def test_unsupported_dtype_raises(self, tmp_path):
        """complex64 voxels raise ValueError immediately."""
        data = np.zeros((4, 4, 4), dtype='complex64')
        with pytest.raises(ValueError, match='Unsupported dtype'):
            write_nrrd_raw(tmp_path / 'bad.nrrd', data, _make_header())


# ===================================================================
# BENCHMARKS
# ===================================================================


def test_benchmark_handrolled_write(benchmark, tmp_path, bench_data, bench_header):
    """Benchmark write_nrrd_raw on a ~4 M-voxel float32 volume."""
    out = tmp_path / 'fast.nrrd'
    benchmark(lambda: write_nrrd_raw(out, bench_data, bench_header))


def test_benchmark_pynrrd_write(benchmark, tmp_path, bench_data, bench_header):
    """Benchmark nrrd.write on the same volume for comparison."""
    out = tmp_path / 'ref.nrrd'
    benchmark(lambda: nrrd.write(str(out), bench_data, bench_header))


def test_handrolled_faster_than_pynrrd(tmp_path, bench_data, bench_header):
    """Hand-rolled writer is at least 10x faster than pynrrd for large volumes.

    The docstring in extraction.py claims ~80x; 10x is used here as a
    conservative bound so the assertion holds on slow CI runners.
    """
    fast_path = tmp_path / 'fast.nrrd'
    ref_path = tmp_path / 'ref.nrrd'

    n = 3
    t_fast = timeit.timeit(
        lambda: write_nrrd_raw(fast_path, bench_data, bench_header),
        number=n,
    )
    t_ref = timeit.timeit(
        lambda: nrrd.write(str(ref_path), bench_data, bench_header),
        number=n,
    )
    speedup = t_ref / t_fast
    assert speedup >= 10, f'Expected >=10x speedup, got {speedup:.1f}x'

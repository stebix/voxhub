"""End-to-end tests for voxhub_core.staging.stage and extract_spatial_metadata.

The low-level NRRD writer (_write_nrrd_raw) is already covered by
test_staging.py; this file covers the public stage() entrypoint and the
extract_spatial_metadata helper that feeds it.

Plan: docs/testing/catalog-staging-audit.md §4

Each test is currently skipped.  Remove the skip marker as tests are
implemented in a downstream worktree.

Fixtures expected:
    - zarr_root_factory
"""

import pytest

pytestmark = pytest.mark.skip(
    reason='stub — see docs/testing/catalog-staging-audit.md §4'
)


# ===========================================================================
# extract_spatial_metadata
# ===========================================================================


class TestExtractSpatialMetadata:
    """Covers voxhub_core.staging.extract_spatial_metadata."""

    def test_canonical_axis_aligned_metadata(self, zarr_root_factory):
        """Store with identity-like orientation → returned origin/directions/
        spacing match what _core_helpers.create_zarr_store writes."""
        del zarr_root_factory

    def test_oblique_orientation(self, zarr_root_factory):
        """Non-identity ImageOrientationPatient → space_directions matrix
        reflects the rotation correctly."""
        del zarr_root_factory

    def test_anisotropic_spacing(self, zarr_root_factory):
        """PixelSpacing differs from computed_slice_spacing_mm → returned
        spacing_mm triple reflects the mix correctly."""
        del zarr_root_factory

    def test_missing_image_position_patient_raises_keyerror(self, tmp_path):
        """No ImagePositionPatient attr → KeyError."""
        del tmp_path

    def test_missing_pixel_spacing_raises_keyerror(self, tmp_path):
        """No PixelSpacing attr → KeyError."""
        del tmp_path

    def test_missing_computed_slice_spacing_raises_keyerror(self, tmp_path):
        """No computed_slice_spacing_mm attr → KeyError."""
        del tmp_path

    def test_returns_numpy_arrays_for_origin_and_directions(
        self, zarr_root_factory
    ):
        """Returned origin and space_directions are np.ndarray, not Python
        lists (function contract)."""
        del zarr_root_factory


# ===========================================================================
# stage() — single store happy path
# ===========================================================================


class TestStageSingleStore:
    """Covers voxhub_core.staging.stage — single-store happy path."""

    def test_creates_wip_dir_with_store_subdirectory(
        self, zarr_root_factory, tmp_path
    ):
        """stage(root, wip_dir, store_names=['foo']) → wip_dir/foo/ exists."""
        del zarr_root_factory, tmp_path

    def test_writes_raw_nrrd(self, zarr_root_factory, tmp_path):
        """wip_dir/foo/raw.nrrd exists and can be read via nrrd.read()."""
        del zarr_root_factory, tmp_path

    def test_nrrd_data_matches_zarr_data(
        self, zarr_root_factory, tmp_path
    ):
        """Read back both; they match modulo the axis reversal documented
        in test_staging.py."""
        del zarr_root_factory, tmp_path

    def test_manifest_structure(self, zarr_root_factory, tmp_path):
        """Returned manifest is dict[store_name, dict] with zarr_path,
        raw_checksum, shape, origin_lps, space_directions, spacing_mm keys."""
        del zarr_root_factory, tmp_path

    def test_raw_checksum_is_sha256_hex(self, zarr_root_factory, tmp_path):
        """checksum string matches sha256:[0-9a-f]{64}."""
        del zarr_root_factory, tmp_path

    def test_checksum_matches_actual_file_contents(
        self, zarr_root_factory, tmp_path
    ):
        """Compute sha256 of wip_dir/foo/raw.nrrd directly; compare to the
        manifest value — they must match byte-for-byte."""
        del zarr_root_factory, tmp_path


# ===========================================================================
# stage() — multi-store
# ===========================================================================


class TestStageMultipleStores:
    """Multi-store filtering and selection."""

    def test_stages_all_stores_when_names_none(
        self, zarr_root_factory, tmp_path
    ):
        """store_names=None → all stores in zarr_root are staged."""
        del zarr_root_factory, tmp_path

    def test_stages_subset_when_names_provided(
        self, zarr_root_factory, tmp_path
    ):
        """store_names=['a', 'c'] → only those two appear in manifest and
        on disk."""
        del zarr_root_factory, tmp_path

    def test_nonexistent_store_name_behavior(
        self, zarr_root_factory, tmp_path
    ):
        """store_names=['missing'] → document current behavior (warning or
        silent skip). Pin it down."""
        del zarr_root_factory, tmp_path


# ===========================================================================
# stage() — compression flag
# ===========================================================================


class TestStageCompressionFlag:
    """Covers the --compress flag in stage()."""

    def test_compressed_nrrd_is_gzipped(
        self, zarr_root_factory, tmp_path
    ):
        """compress=True → output file starts with \\x1f\\x8b (gzip magic)."""
        del zarr_root_factory, tmp_path

    def test_compressed_and_uncompressed_data_equivalent(
        self, zarr_root_factory, tmp_path
    ):
        """Stage the same store twice (compress=True and =False), read both
        via nrrd.read, compare arrays (should be identical modulo header
        key set)."""
        del zarr_root_factory, tmp_path


# ===========================================================================
# stage() — force flag and error paths
# ===========================================================================


class TestStageForceAndErrors:
    """Covers force flag and error propagation."""

    def test_refuses_to_overwrite_without_force(
        self, zarr_root_factory, tmp_path
    ):
        """Pre-populate wip_dir with conflicting file → FileExistsError."""
        del zarr_root_factory, tmp_path

    def test_force_overwrites_existing_wip_contents(
        self, zarr_root_factory, tmp_path
    ):
        """Same setup but force=True → succeeds, old contents gone."""
        del zarr_root_factory, tmp_path

    def test_missing_zarr_root_raises(self, tmp_path):
        """zarr_root doesn't exist → FileNotFoundError."""
        del tmp_path

    def test_zarr_store_without_raw_full_raises_or_skips(
        self, zarr_root_factory, tmp_path
    ):
        """Store lacks raw/full → document behavior and pin it down."""
        del zarr_root_factory, tmp_path

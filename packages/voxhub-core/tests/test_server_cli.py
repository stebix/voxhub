"""Function-level tests for voxhub_core.server.cli handlers.

Each test calls ``_run_X(args)`` directly with a hand-built
``argparse.Namespace``, captures stdout via ``capsys``, and asserts on the
parsed JSON envelope.  Subprocess-level smoke tests live in
``test_server_cli_subprocess.py``.

Plan: docs/testing/server-cli.md
"""

import json
import shutil
from pathlib import Path

import numpy as np
import pytest
import zarr
from _core_helpers import (
    ORIGIN_LPS,
    SHAPE,
    SPACING_MM,
    default_seg_label_map,
    write_seg_nrrd,
)

from voxhub_core.extraction import extract_spatial_metadata
from voxhub_core.server import cli as server_cli
from voxhub_schema import (
    PROTOCOL_VERSION,
    UNCONSTRAINED_SEGMENTATION,
    validate_seg_preflight,
)

# ===========================================================================
# _run_list_stores
# ===========================================================================


class TestListStores:
    """Covers voxhub_core.server.cli._run_list_stores."""

    def test_lists_single_empty_store(
        self, stores_dir_factory, server_argv, parsed_stdout
    ):
        root = stores_dir_factory(('alpha',))
        server_cli._run_list_stores(server_argv(stores_dir=root))

        payload = parsed_stdout()
        assert payload['protocol_version'] == PROTOCOL_VERSION
        assert len(payload['stores']) == 1
        entry = payload['stores'][0]
        assert entry['name'] == 'alpha'
        assert entry['shape'] == list(SHAPE)
        assert entry['dtype'] == 'float32'
        assert entry['origin_lps'] == ORIGIN_LPS
        assert entry['spacing_mm'] == SPACING_MM
        assert entry['annotations'] == []
        assert entry['error'] is None
        assert entry['dataset_attributes'] is None

    def test_lists_multiple_stores_sorted(
        self, stores_dir_factory, server_argv, parsed_stdout
    ):
        root = stores_dir_factory(('charlie', 'alpha', 'bravo'))
        server_cli._run_list_stores(server_argv(stores_dir=root))

        payload = parsed_stdout()
        names = [s['name'] for s in payload['stores']]
        # discover_zarr_stores sorts by filesystem path; '.zarr' suffix
        # preserves alphabetical order of the stem.
        assert names == sorted(names)
        assert set(names) == {'alpha', 'bravo', 'charlie'}

    def test_lists_store_with_annotations(
        self, stores_dir_factory, server_argv, parsed_stdout
    ):
        root = stores_dir_factory(('alpha',), with_annotations=True)
        server_cli._run_list_stores(server_argv(stores_dir=root))

        payload = parsed_stdout()
        entry = payload['stores'][0]
        assert len(entry['annotations']) == 1
        ann = entry['annotations'][0]
        assert ann['annotator_id'] == 'alice'
        assert ann['ontology'] == 'inner-ear-structures'
        assert ann['ontology_version'] == 1
        assert ann['integrated_at'] == '2026-01-01T00:00:00+00:00'
        assert ann['path'].startswith('annotations/alice-')

    def test_includes_dataset_attributes_when_present(
        self, stores_dir_factory, server_argv, parsed_stdout
    ):
        da = {
            'modality': 'MRI',
            'resolution': {'voxel_size': [0.5, 0.5, 0.5], 'unit': 'mm'},
            'origin': 'synthetic',
            'tags': {'note': 'test'},
        }
        root = stores_dir_factory(('alpha',), dataset_attributes={'alpha': da})
        server_cli._run_list_stores(server_argv(stores_dir=root))

        payload = parsed_stdout()
        entry = payload['stores'][0]
        assert entry['dataset_attributes'] is not None
        assert entry['dataset_attributes']['modality'] == 'MRI'
        assert entry['dataset_attributes']['origin'] == 'synthetic'

    def test_protocol_version_present(
        self, stores_dir_factory, server_argv, parsed_stdout
    ):
        root = stores_dir_factory(('alpha',))
        server_cli._run_list_stores(server_argv(stores_dir=root))
        assert parsed_stdout()['protocol_version'] == PROTOCOL_VERSION

    def test_store_with_probe_error(self, stores_dir_factory, server_argv, parsed_stdout):
        root = stores_dir_factory(('good', 'broken'), corrupt=('broken',))
        server_cli._run_list_stores(server_argv(stores_dir=root))

        payload = parsed_stdout()
        by_name = {s['name']: s for s in payload['stores']}
        assert by_name['broken']['error'] is not None
        assert by_name['broken']['shape'] == []
        assert by_name['broken']['annotations'] == []
        # Unaffected sibling still succeeds.
        assert by_name['good']['error'] is None
        assert by_name['good']['shape'] == list(SHAPE)

    def test_store_missing_spatial_metadata(
        self, stores_dir_factory, server_argv, parsed_stdout
    ):
        root = stores_dir_factory(('alpha',), with_annotations=True)
        # Remove spatial attrs from raw/full by rewriting zarr.json.
        arr_path = root / 'alpha.zarr' / 'raw' / 'full'
        meta = json.loads((arr_path / 'zarr.json').read_text())
        for k in (
            'ImagePositionPatient',
            'ImageOrientationPatient',
            'PixelSpacing',
            'computed_slice_spacing_mm',
        ):
            meta.get('attributes', {}).pop(k, None)
        (arr_path / 'zarr.json').write_text(json.dumps(meta))

        server_cli._run_list_stores(server_argv(stores_dir=root))
        entry = parsed_stdout()['stores'][0]
        assert entry['error'] == 'Missing spatial metadata'
        # Annotations still reported — discovery is independent.
        assert len(entry['annotations']) == 1

    def test_nonexistent_stores_dir(self, tmp_path, server_argv, parsed_stdout):
        server_cli._run_list_stores(server_argv(stores_dir=tmp_path / 'does-not-exist'))
        payload = parsed_stdout()
        assert payload['stores'] == []
        assert payload['protocol_version'] == PROTOCOL_VERSION

    def test_logs_duration_on_completion(self, stores_dir_factory, server_argv, caplog):
        import logging

        caplog.set_level(logging.INFO, logger='voxhub_core.server.cli')
        root = stores_dir_factory(('alpha',))
        server_cli._run_list_stores(server_argv(stores_dir=root))

        # structlog renders the event dict as the LogRecord's msg.
        completed = [
            r
            for r in caplog.records
            if isinstance(r.msg, dict) and r.msg.get('event') == 'list_stores_completed'
        ]
        assert completed
        assert 'duration_s' in completed[0].msg

    # -- Catalog-cache integration (PR 2) ----------------------------------

    def test_catalog_version_emitted_on_first_call(
        self, stores_dir_factory, server_argv, parsed_stdout
    ):
        root = stores_dir_factory(('alpha',))
        server_cli._run_list_stores(server_argv(stores_dir=root))
        payload = parsed_stdout()
        assert payload['catalog_version'] == 1

    def test_cache_file_created_at_meta_catalog_json(
        self, stores_dir_factory, server_argv, parsed_stdout
    ):
        root = stores_dir_factory(('alpha',))
        server_cli._run_list_stores(server_argv(stores_dir=root))
        parsed_stdout()  # drain stdout

        cache_path = root / '.meta' / 'catalog.json'
        assert cache_path.is_file()
        data = json.loads(cache_path.read_text())
        assert data['catalog_version'] == 1
        assert set(data['stores'].keys()) == {'alpha'}

    def test_repeated_calls_within_ttl_reuse_cache(
        self,
        stores_dir_factory,
        server_argv,
        parsed_stdout,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from voxhub_core.server import catalog_cache as cc

        root = stores_dir_factory(('alpha', 'bravo'))
        server_cli._run_list_stores(server_argv(stores_dir=root))
        first = parsed_stdout()

        # Poison the rebuild path: a warm hit must not touch it.
        def _boom(*_a: object, **_kw: object) -> None:
            raise AssertionError('warm hit must not rebuild')

        monkeypatch.setattr(cc, '_build_snapshot', _boom)

        server_cli._run_list_stores(server_argv(stores_dir=root))
        second = parsed_stdout()

        assert second['catalog_version'] == first['catalog_version']
        assert [s['name'] for s in second['stores']] == [
            s['name'] for s in first['stores']
        ]

    def test_corrupt_cache_file_triggers_healthy_rebuild(
        self, stores_dir_factory, server_argv, parsed_stdout
    ):
        root = stores_dir_factory(('alpha',))
        server_cli._run_list_stores(server_argv(stores_dir=root))
        parsed_stdout()  # drain

        cache_path = root / '.meta' / 'catalog.json'
        cache_path.write_text('{not json at all')

        server_cli._run_list_stores(server_argv(stores_dir=root))
        payload = parsed_stdout()

        assert len(payload['stores']) == 1
        assert payload['stores'][0]['name'] == 'alpha'
        assert payload['stores'][0]['error'] is None
        # Corrupt parse → cold rebuild → catalog_version resets to 1.
        assert payload['catalog_version'] == 1

    def test_out_of_band_add_after_ttl_bumps_catalog_version(
        self, stores_dir_factory, server_argv, parsed_stdout
    ):
        from _core_helpers import create_zarr_store

        root = stores_dir_factory(('alpha',))
        server_cli._run_list_stores(server_argv(stores_dir=root))
        first = parsed_stdout()
        assert first['catalog_version'] == 1
        assert {s['name'] for s in first['stores']} == {'alpha'}

        # Age the on-disk catalog past the default TTL. Rewriting ``built_at``
        # is cheaper than faking a monotonic clock through the CLI.
        cache_path = root / '.meta' / 'catalog.json'
        data = json.loads(cache_path.read_text())
        data['built_at'] = '2000-01-01T00:00:00+00:00'
        cache_path.write_text(json.dumps(data))

        # Out-of-band filesystem mutation the fingerprint will notice.
        create_zarr_store(root / 'bravo.zarr')

        server_cli._run_list_stores(server_argv(stores_dir=root))
        second = parsed_stdout()

        assert second['catalog_version'] == first['catalog_version'] + 1
        assert {s['name'] for s in second['stores']} == {'alpha', 'bravo'}

    # -- --if-version client short-circuit (PR 5) --------------------------

    def test_if_version_matching_returns_unchanged_envelope(
        self, stores_dir_factory, server_argv, parsed_stdout
    ):
        root = stores_dir_factory(('alpha', 'bravo'))
        # Prime the cache so we know the current catalog_version.
        server_cli._run_list_stores(server_argv(stores_dir=root))
        first = parsed_stdout()
        version = first['catalog_version']

        server_cli._run_list_stores(server_argv(stores_dir=root, if_version=version))
        payload = parsed_stdout()

        assert payload['protocol_version'] == PROTOCOL_VERSION
        assert payload['catalog_version'] == version
        assert payload['unchanged'] is True
        assert 'stores' not in payload

    def test_if_version_mismatching_returns_full_payload(
        self, stores_dir_factory, server_argv, parsed_stdout
    ):
        root = stores_dir_factory(('alpha',))
        server_cli._run_list_stores(server_argv(stores_dir=root))
        first = parsed_stdout()
        stale_version = first['catalog_version'] - 1

        server_cli._run_list_stores(
            server_argv(stores_dir=root, if_version=stale_version)
        )
        payload = parsed_stdout()

        assert 'unchanged' not in payload
        assert payload['catalog_version'] == first['catalog_version']
        assert {s['name'] for s in payload['stores']} == {'alpha'}

    def test_if_version_zero_against_first_call_does_not_short_circuit(
        self, stores_dir_factory, server_argv, parsed_stdout
    ):
        """First-ever call has no on-disk cache. The cold rebuild assigns
        catalog_version=1, so a client probing with --if-version 0 must
        receive the full payload, not an unchanged envelope."""
        root = stores_dir_factory(('alpha',))
        server_cli._run_list_stores(server_argv(stores_dir=root, if_version=0))
        payload = parsed_stdout()

        assert payload['catalog_version'] == 1
        assert 'unchanged' not in payload
        assert payload['stores'][0]['name'] == 'alpha'


# ===========================================================================
# _run_prepare_pull
# ===========================================================================


class TestPreparePull:
    """Covers voxhub_core.server.cli._run_prepare_pull (single-store)."""

    def test_stages_single_store_to_tempdir(
        self, stores_dir_factory, server_argv, parsed_stdout
    ):
        root = stores_dir_factory(('alpha',))
        server_cli._run_prepare_pull(server_argv(stores_dir=root, store='alpha'))

        payload = parsed_stdout()
        staging_dir = Path(payload['staging_dir'])
        try:
            assert staging_dir.is_dir()
            assert staging_dir.name.startswith(server_cli.STAGING_DIR_PREFIX)
            assert payload['store_name'] == 'alpha'
            assert payload['raw_name'] == 'raw.nrrd'
            assert payload['raw_checksum'].startswith('sha256:')
            assert payload['shape'] == list(SHAPE)
            assert payload['spacing_mm'] == SPACING_MM
            assert payload['origin_lps'] == ORIGIN_LPS
            assert payload['skipped_annotations'] == []
            # Layout: raw.nrrd lives at the session root, no <store_name>/ subdir.
            assert (staging_dir / 'raw.nrrd').is_file()
            assert not (staging_dir / 'alpha').exists()
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)

    def test_writes_pull_manifest(self, stores_dir_factory, server_argv, parsed_stdout):
        from voxhub_schema import PullManifest

        root = stores_dir_factory(('alpha',))
        server_cli._run_prepare_pull(server_argv(stores_dir=root, store='alpha'))

        payload = parsed_stdout()
        staging_dir = Path(payload['staging_dir'])
        try:
            assert (staging_dir / '.voxhub_pull.json').is_file()
            manifest = PullManifest.read(staging_dir)
            assert manifest.store_name == 'alpha'
            assert manifest.raw_name == 'raw.nrrd'
            assert manifest.raw_checksum == payload['raw_checksum']
            assert manifest.server_stores_dir == str(root)
            assert manifest.shape == list(SHAPE)
            assert manifest.annotations == []
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)

    def test_stages_under_configured_staging_root(
        self, stores_dir_factory, tmp_path, server_argv, parsed_stdout
    ):
        """Server-authoritative contract: ``prepare-pull`` creates its
        session dir under ``args.staging_root`` (populated from
        ``settings.storage.staging_dir`` in production)."""
        root = stores_dir_factory(('alpha',))
        staging_root = tmp_path / 'configured_staging_root'
        staging_root.mkdir()

        server_cli._run_prepare_pull(
            server_argv(
                stores_dir=root,
                store='alpha',
                staging_root=str(staging_root),
            )
        )

        payload = parsed_stdout()
        staging_dir = Path(payload['staging_dir'])
        try:
            assert staging_dir.parent == staging_root
            assert staging_dir.name.startswith(server_cli.STAGING_DIR_PREFIX)
            assert (staging_dir / 'raw.nrrd').is_file()
            assert (staging_dir / '.voxhub_pull.json').is_file()
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)

    def test_exports_reference_annotation(
        self, stores_dir_factory, server_argv, parsed_stdout
    ):
        """Requested annotation is exported to reference/ and recorded in manifest."""
        from voxhub_schema import PullManifest

        root = stores_dir_factory(('alpha',), with_annotations=True)
        ann_rel = 'annotations/alice-xyz45678/inner-ear-structures-20260101-ab12'

        server_cli._run_prepare_pull(
            server_argv(
                stores_dir=root,
                store='alpha',
                include_existing_annotations=[ann_rel],
            )
        )

        payload = parsed_stdout()
        staging_dir = Path(payload['staging_dir'])
        try:
            ref_dir = staging_dir / 'reference'
            assert ref_dir.is_dir()
            files = list(ref_dir.iterdir())
            # Populated annotation has no segments/labels, so it may classify
            # as landmarks and fail export; allow skipped, but manifest must
            # reflect reality.
            manifest = PullManifest.read(staging_dir)
            assert len(manifest.annotations) + len(payload['skipped_annotations']) == 1
            if manifest.annotations:
                entry = manifest.annotations[0]
                assert entry.annotator_id == 'alice'
                assert entry.zarr_source_path == ann_rel
                assert entry.reference_checksum.startswith('sha256:')
                assert entry.reference_filename in (f.name for f in files)
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)

    def test_compression_flag_propagates_to_extract_volume(
        self, stores_dir_factory, server_argv, parsed_stdout, monkeypatch
    ):
        root = stores_dir_factory(('alpha',))
        captured: dict[str, object] = {}

        original_extract_volume = server_cli.extract_volume

        def spy_extract_volume(*args, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            return original_extract_volume(*args, **kwargs)

        monkeypatch.setattr(server_cli, 'extract_volume', spy_extract_volume)

        server_cli._run_prepare_pull(
            server_argv(stores_dir=root, store='alpha', compress=True)
        )
        payload = parsed_stdout()
        try:
            assert captured['compress'] is True
        finally:
            shutil.rmtree(payload['staging_dir'], ignore_errors=True)

    def test_extract_volume_failure_writes_error_envelope_and_exits(
        self, stores_dir_factory, server_argv, parsed_stdout, monkeypatch
    ):
        root = stores_dir_factory(('alpha',))

        def boom(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError('extraction blew up')

        monkeypatch.setattr(server_cli, 'extract_volume', boom)

        with pytest.raises(SystemExit) as excinfo:
            server_cli._run_prepare_pull(server_argv(stores_dir=root, store='alpha'))
        assert excinfo.value.code == 1

        envelope = parsed_stdout()
        assert envelope['error'] is True
        assert envelope['code'] == 'prepare_pull_failed'
        assert 'extraction blew up' in envelope['message']
        assert envelope['protocol_version'] == PROTOCOL_VERSION

    def test_store_not_found_fails_before_staging(
        self, stores_dir_factory, tmp_path, server_argv, parsed_stdout, monkeypatch
    ):
        """Missing --store errors early with no staging dir created."""
        root = stores_dir_factory(('alpha',))

        # Sentinel to assert extract_volume is never called.
        calls: list[int] = []

        def sentinel(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            calls.append(1)
            raise AssertionError('extract_volume must not be called when store is absent')

        monkeypatch.setattr(server_cli, 'extract_volume', sentinel)

        with pytest.raises(SystemExit) as excinfo:
            server_cli._run_prepare_pull(
                server_argv(stores_dir=root, store='does-not-exist')
            )
        assert excinfo.value.code == 1

        envelope = parsed_stdout()
        assert envelope['error'] is True
        assert envelope['code'] == 'store_not_found'
        assert 'does-not-exist' in envelope['message']
        assert calls == []

    def test_skipped_annotation_missing_array(
        self, stores_dir_factory, server_argv, parsed_stdout
    ):
        """Requesting an annotation path without a data array surfaces
        it in skipped_annotations; manifest has no entry for it."""
        from voxhub_schema import PullManifest

        root = stores_dir_factory(('alpha',))
        missing_ann = 'annotations/alice-abcd1234/nothing-here-20260101-zz99'

        server_cli._run_prepare_pull(
            server_argv(
                stores_dir=root,
                store='alpha',
                include_existing_annotations=[missing_ann],
            )
        )

        payload = parsed_stdout()
        staging_dir = Path(payload['staging_dir'])
        try:
            assert len(payload['skipped_annotations']) == 1
            skip = payload['skipped_annotations'][0]
            assert skip['path'] == missing_ann
            assert 'not found' in skip['reason']
            manifest = PullManifest.read(staging_dir)
            assert manifest.annotations == []
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)

    def test_protocol_version_present(
        self, stores_dir_factory, server_argv, parsed_stdout
    ):
        root = stores_dir_factory(('alpha',))
        server_cli._run_prepare_pull(server_argv(stores_dir=root, store='alpha'))
        payload = parsed_stdout()
        try:
            assert payload['protocol_version'] == PROTOCOL_VERSION
        finally:
            shutil.rmtree(payload['staging_dir'], ignore_errors=True)

    def test_staging_dir_is_string_not_path_object(
        self, stores_dir_factory, server_argv, parsed_stdout
    ):
        root = stores_dir_factory(('alpha',))
        server_cli._run_prepare_pull(server_argv(stores_dir=root, store='alpha'))
        payload = parsed_stdout()
        try:
            assert isinstance(payload['staging_dir'], str)
            assert not payload['staging_dir'].startswith('PosixPath(')
        finally:
            shutil.rmtree(payload['staging_dir'], ignore_errors=True)

    def test_partial_extraction_cleans_up_orphan_file(
        self, stores_dir_factory, server_argv, parsed_stdout, monkeypatch
    ):
        """An ExtractionError partway through writing a reference file
        must not leave a truncated file on disk — the annotation appears
        in skipped_annotations and reference/ contains nothing for it."""
        from voxhub_core.extraction import ExtractionError
        from voxhub_schema import PullManifest

        root = stores_dir_factory(('alpha',), with_annotations=True)
        ann_rel = 'annotations/alice-xyz45678/inner-ear-structures-20260101-ab12'

        # Patch both extractors: we don't assert kind here, we just need
        # whichever one runs to partial-write then fail.
        def partial_then_fail(_zarr_path, _array_zarr_path, dest):  # type: ignore[no-untyped-def]
            dest.write_bytes(b'partial bytes before failure')
            raise ExtractionError('injected mid-write failure')

        monkeypatch.setattr(server_cli, 'extract_segmentation', partial_then_fail)
        monkeypatch.setattr(server_cli, 'extract_landmarks', partial_then_fail)

        server_cli._run_prepare_pull(
            server_argv(
                stores_dir=root,
                store='alpha',
                include_existing_annotations=[ann_rel],
            )
        )

        payload = parsed_stdout()
        staging_dir = Path(payload['staging_dir'])
        try:
            skipped = payload['skipped_annotations']
            assert len(skipped) == 1
            assert skipped[0]['path'] == ann_rel
            assert 'injected mid-write failure' in skipped[0]['reason']

            # Manifest has no entry for the skipped annotation.
            manifest = PullManifest.read(staging_dir)
            assert manifest.annotations == []

            # No orphan file left behind in reference/.
            ref_dir = staging_dir / 'reference'
            if ref_dir.exists():
                assert list(ref_dir.iterdir()) == []
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)

    def test_include_annotation_path_traversal_rejected(
        self, stores_dir_factory, server_argv, parsed_stdout, monkeypatch
    ):
        """A ``--include-existing-annotations`` path that escapes the store
        shape (``../../etc``) is rejected up front with an
        ``invalid_annotation_path`` envelope; nothing is extracted (task
        2.8)."""
        root = stores_dir_factory(('alpha',))

        def sentinel(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError('extract_volume must not run on an invalid path')

        monkeypatch.setattr(server_cli, 'extract_volume', sentinel)

        with pytest.raises(SystemExit) as excinfo:
            server_cli._run_prepare_pull(
                server_argv(
                    stores_dir=root,
                    store='alpha',
                    include_existing_annotations=['../../etc'],
                )
            )
        assert excinfo.value.code == 1

        envelope = parsed_stdout()
        assert envelope['error'] is True
        assert envelope['code'] == 'invalid_annotation_path'

    def test_failed_prepare_pull_leaves_no_staging_dir(
        self, stores_dir_factory, tmp_path, server_argv, parsed_stdout, monkeypatch
    ):
        """An extraction failure after mkdtemp cleans up the staging dir
        instead of leaving it for gc (task 2.8)."""
        root = stores_dir_factory(('alpha',))
        staging_root = tmp_path / 'staging'
        staging_root.mkdir()

        def boom(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError('extraction blew up')

        monkeypatch.setattr(server_cli, 'extract_volume', boom)

        with pytest.raises(SystemExit):
            server_cli._run_prepare_pull(
                server_argv(
                    stores_dir=root, store='alpha', staging_root=str(staging_root)
                )
            )
        parsed_stdout()

        leftovers = [
            p
            for p in staging_root.iterdir()
            if p.name.startswith(server_cli.STAGING_DIR_PREFIX)
        ]
        assert leftovers == []


# ===========================================================================
# _run_integrate_annotations — helpers
# ===========================================================================


def _integrate_argv(
    server_argv,
    *,
    stores_dir: Path,
    staging_dir: Path,
    annotator_id: str = 'alice',
    nano_id: str = 'deadbeef',
    machine_id: str = 'machine-xyz',
    force: bool = False,
    checksums: list[str] | None = None,
    expected_ontology: list[str] | None = None,
    unconstrained: bool = False,
):
    """Build an integrate-annotations Namespace with an ontology policy.

    Mirrors the real CLI contract: exactly one of ``expected_ontology``
    / ``unconstrained`` must be non-empty-non-False.  Default is
    ``['inner-ear-structures']`` so the bulk of happy-path tests don't
    have to spell it out; pass ``unconstrained=True`` to explicitly
    test the opt-out path, or override ``expected_ontology=[...]`` for
    a different ontology set.
    """
    if expected_ontology is None and not unconstrained:
        expected_ontology = ['inner-ear-structures']
    return server_argv(
        stores_dir=stores_dir,
        staging_dir=str(staging_dir),
        annotator_id=annotator_id,
        nano_id=nano_id,
        machine_id=machine_id,
        force=force,
        checksums=checksums,
        expected_ontology=expected_ontology or [],
        unconstrained=unconstrained,
    )


def _written_annotations(store_zarr: Path) -> list[Path]:
    """Return the integrated annotation instance directories under a store."""
    ann_root = store_zarr / 'annotations'
    if not ann_root.is_dir():
        return []
    out: list[Path] = []
    for annotator in ann_root.iterdir():
        if annotator.is_dir():
            out.extend(p for p in annotator.iterdir() if p.is_dir())
    return out


# ===========================================================================
# _run_prepare_pull memory-budget surface
# ===========================================================================


class TestPreparePullMemoryWarnings:
    """Covers the memory-budget integration in _run_prepare_pull."""

    def test_default_payload_carries_empty_warnings_list(
        self, stores_dir_factory, server_argv, parsed_stdout
    ):
        root = stores_dir_factory(('alpha',))
        server_cli._run_prepare_pull(server_argv(stores_dir=root, store='alpha'))
        payload = parsed_stdout()
        staging_dir = Path(payload['staging_dir'])
        try:
            assert payload['memory_warnings'] == []
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)

    def test_large_volume_warning_surfaces_in_payload(
        self, stores_dir_factory, server_argv, parsed_stdout, monkeypatch
    ):
        from voxhub_core.server import settings as settings_mod

        root = stores_dir_factory(('alpha',))
        # Tiny threshold so the test SHAPE (10*12*14*4 = 6720 bytes) trips it.
        memory = settings_mod.MemorySettings(
            max_safe_volume_mb=0,
            refuse_when_low_memory=False,
            safety_factor=2.0,
        )
        # Pretend memory is generous so the low-memory path can't fire.
        from voxhub_core import memory_budget as mb

        monkeypatch.setattr(mb, 'read_available_bytes', lambda: 16 * 1024**3)

        server_cli._run_prepare_pull(
            server_argv(stores_dir=root, store='alpha', memory_settings=memory)
        )
        payload = parsed_stdout()
        staging_dir = Path(payload['staging_dir'])
        try:
            warnings = payload['memory_warnings']
            assert len(warnings) == 1
            assert warnings[0]['code'] == 'large_volume'
            assert warnings[0]['volume_bytes'] == 10 * 12 * 14 * 4
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)

    def test_refuses_when_low_memory(
        self, stores_dir_factory, server_argv, capsys, monkeypatch
    ):
        from voxhub_core.server import settings as settings_mod

        root = stores_dir_factory(('alpha',))
        memory = settings_mod.MemorySettings(
            max_safe_volume_mb=512,
            refuse_when_low_memory=True,
            safety_factor=2.0,
        )
        from voxhub_core import memory_budget as mb

        # 1 byte available — far below 2 * 6720.
        monkeypatch.setattr(mb, 'read_available_bytes', lambda: 1)

        with pytest.raises(SystemExit) as exc_info:
            server_cli._run_prepare_pull(
                server_argv(stores_dir=root, store='alpha', memory_settings=memory)
            )
        assert exc_info.value.code == 1

        out = capsys.readouterr().out
        envelope = json.loads(out.strip().splitlines()[-1])
        assert envelope['error'] is True
        assert envelope['code'] == 'insufficient_memory'

    def test_prepare_pull_refuses_when_disk_full(
        self, stores_dir_factory, tmp_path, server_argv, parsed_stdout, monkeypatch
    ):
        """A staging filesystem >=90% full → ``disk_full`` envelope, exit 1,
        and nothing staged (task 2.6)."""
        import collections

        root = stores_dir_factory(('alpha',))
        staging_root = tmp_path / 'staging'
        staging_root.mkdir()
        usage = collections.namedtuple('usage', ['total', 'used', 'free'])
        monkeypatch.setattr(
            server_cli.shutil, 'disk_usage', lambda _p: usage(1000, 950, 50)
        )

        with pytest.raises(SystemExit) as excinfo:
            server_cli._run_prepare_pull(
                server_argv(
                    stores_dir=root, store='alpha', staging_root=str(staging_root)
                )
            )
        assert excinfo.value.code == 1

        envelope = parsed_stdout()
        assert envelope['error'] is True
        assert envelope['code'] == 'disk_full'
        # Nothing staged.
        assert list(staging_root.iterdir()) == []


# ===========================================================================
# _run_integrate_annotations (memory + disk preconditions — task 2.6)
# ===========================================================================


class TestIntegrateMemoryAndDiskPreconditions:
    """Covers the integrate-side RAM budget and disk-full guard (task 2.6)."""

    def test_integrate_refuses_segmentation_on_low_memory(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
        monkeypatch,
    ):
        from voxhub_core import memory_budget as mb
        from voxhub_core.server import settings as settings_mod

        stores_dir = stores_dir_factory(('alpha',))
        staging = staging_dir_with_annotations(store_names=['alpha'])
        memory = settings_mod.MemorySettings(
            max_safe_volume_mb=512,
            refuse_when_low_memory=True,
            safety_factor=2.0,
        )
        # 1 byte available — far below the seg.nrrd file size * safety.
        monkeypatch.setattr(mb, 'read_available_bytes', lambda: 1)

        argv = _integrate_argv(server_argv, stores_dir=stores_dir, staging_dir=staging)
        argv.memory_settings = memory
        # A failed store makes the whole batch exit non-zero (task 2.8).
        with pytest.raises(SystemExit) as excinfo:
            server_cli._run_integrate_annotations(argv)
        assert excinfo.value.code == 1

        result = parsed_stdout()['stores']['alpha']
        assert result['status'] == 'failed'
        assert any('memory' in i['message'].lower() for i in result['issues'])
        assert _written_annotations(stores_dir / 'alpha.zarr') == []

    def test_integrate_refuses_when_disk_full(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
        monkeypatch,
    ):
        import collections

        stores_dir = stores_dir_factory(('alpha',))
        staging = staging_dir_with_annotations(store_names=['alpha'])
        usage = collections.namedtuple('usage', ['total', 'used', 'free'])
        monkeypatch.setattr(
            server_cli.shutil, 'disk_usage', lambda _p: usage(1000, 999, 1)
        )

        with pytest.raises(SystemExit) as excinfo:
            server_cli._run_integrate_annotations(
                _integrate_argv(server_argv, stores_dir=stores_dir, staging_dir=staging)
            )
        assert excinfo.value.code == 1

        envelope = parsed_stdout()
        assert envelope['error'] is True
        assert envelope['code'] == 'disk_full'
        assert _written_annotations(stores_dir / 'alpha.zarr') == []


# ===========================================================================
# _run_integrate_annotations (happy paths)
# ===========================================================================


class TestIntegrateAnnotationsHappy:
    """Covers _run_integrate_annotations success paths."""

    def test_integrates_segmentation_writes_to_annotator_scoped_path(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
    ):
        stores_dir = stores_dir_factory(('alpha',))
        staging = staging_dir_with_annotations(store_names=['alpha'])

        server_cli._run_integrate_annotations(
            _integrate_argv(server_argv, stores_dir=stores_dir, staging_dir=staging)
        )

        payload = parsed_stdout()
        store_result = payload['stores']['alpha']
        assert store_result['status'] == 'integrated'
        assert len(store_result['annotations']) == 1
        ann = store_result['annotations'][0]
        assert ann['path'].startswith('annotations/alice-deadbeef/')
        assert ann['ontology'] == 'inner-ear-structures'

        written = _written_annotations(stores_dir / 'alpha.zarr')
        assert len(written) == 1
        assert written[0].parent.name == 'alice-deadbeef'

    def test_integrates_landmarks_writes_to_annotator_scoped_path(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
    ):
        stores_dir = stores_dir_factory(('alpha',))
        staging = staging_dir_with_annotations(
            store_names=['alpha'],
            include_seg=False,
            include_lmk=True,
        )

        server_cli._run_integrate_annotations(
            _integrate_argv(
                server_argv,
                stores_dir=stores_dir,
                staging_dir=staging,
                expected_ontology=['inner-ear-landmarks'],
            )
        )

        payload = parsed_stdout()
        store_result = payload['stores']['alpha']
        assert store_result['status'] == 'integrated'
        ann = store_result['annotations'][0]
        assert ann['ontology'] == 'inner-ear-landmarks'
        assert ann['path'].startswith('annotations/alice-deadbeef/')

    def test_integrates_both_seg_and_landmarks_in_single_call(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
    ):
        stores_dir = stores_dir_factory(('alpha',))
        staging = staging_dir_with_annotations(
            store_names=['alpha'],
            include_seg=True,
            include_lmk=True,
        )

        server_cli._run_integrate_annotations(
            _integrate_argv(
                server_argv,
                stores_dir=stores_dir,
                staging_dir=staging,
                expected_ontology=['inner-ear-structures', 'inner-ear-landmarks'],
            )
        )

        store_result = parsed_stdout()['stores']['alpha']
        assert store_result['status'] == 'integrated'
        ontologies = {a['ontology'] for a in store_result['annotations']}
        assert ontologies == {'inner-ear-structures', 'inner-ear-landmarks'}
        assert len(_written_annotations(stores_dir / 'alpha.zarr')) == 2

    def test_provenance_recorded_on_success(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
    ):
        stores_dir = stores_dir_factory(('alpha',))
        staging = staging_dir_with_annotations(store_names=['alpha'])

        server_cli._run_integrate_annotations(
            _integrate_argv(server_argv, stores_dir=stores_dir, staging_dir=staging)
        )
        parsed_stdout()

        jsonl_path = stores_dir / '.meta' / 'provenance.jsonl'
        assert jsonl_path.is_file()
        lines = [
            json.loads(line)
            for line in jsonl_path.read_text().splitlines()
            if line.strip()
        ]
        assert len(lines) == 1
        record = lines[0]
        assert record['event'] == 'push'
        assert record['store'] == 'alpha'
        assert record['annotator_id'] == 'alice'
        assert record['ontology'] == 'inner-ear-structures'

        written = _written_annotations(stores_dir / 'alpha.zarr')[0]
        arr = zarr.open_array(written / 'data', mode='r')
        arr_attrs = dict(arr.attrs)
        assert arr_attrs['annotator_id'] == 'alice'
        assert arr_attrs['machine_id'] == 'machine-xyz'
        assert arr_attrs['ontology'] == 'inner-ear-structures'
        assert arr_attrs['integrated_at']

    def test_declared_ontology_from_cli_flows_into_zarr_attrs(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
    ):
        """Ontology declared via --expected-ontology on the CLI shows up
        in the integrated annotation's zarr attrs and the instance-dir
        prefix — the CLI is the authoritative source under the new
        explicit-intent policy (prior RemoteManifest read has been
        dropped)."""
        stores_dir = stores_dir_factory(('alpha',))
        staging = staging_dir_with_annotations(store_names=['alpha'])

        server_cli._run_integrate_annotations(
            _integrate_argv(
                server_argv,
                stores_dir=stores_dir,
                staging_dir=staging,
                expected_ontology=['inner-ear-structures'],
            )
        )
        parsed_stdout()

        written = _written_annotations(stores_dir / 'alpha.zarr')[0]
        arr = zarr.open_array(written / 'data', mode='r')
        assert dict(arr.attrs)['ontology'] == 'inner-ear-structures'
        # Instance dir prefix matches the ontology declared on the CLI.
        assert written.name.startswith('inner-ear-structures-')

    def test_integrate_does_not_require_voxhub_manifest_json(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
    ):
        """Regression guard: after the RemoteManifest decoupling, the
        server's integrate-annotations path must not read (or require)
        ``.voxhub_manifest.json`` in the staging dir.  Prior to this
        change, the server wrote ``.voxhub_pull.json`` during
        prepare-pull but tried to read ``.voxhub_manifest.json`` during
        integrate — a latent break that a test helper papered over in
        CI.  Ontology declaration now comes from CLI args, so the two
        files' schemas are decoupled from integrate entirely.

        This test asserts the staging dir contains *no*
        ``.voxhub_manifest.json``, then runs integrate successfully.
        If someone re-adds a ``RemoteManifest.read(staging_dir)`` call
        to the server path, this test fails with the old
        ``manifest_missing`` error envelope."""
        stores_dir = stores_dir_factory(('alpha',))
        staging = staging_dir_with_annotations(store_names=['alpha'])

        # Precondition: the fixture produces a bare staging dir — no
        # manifest file of either schema should be present.
        assert not (staging / '.voxhub_manifest.json').exists()
        assert not (staging / '.voxhub_pull.json').exists()

        server_cli._run_integrate_annotations(
            _integrate_argv(server_argv, stores_dir=stores_dir, staging_dir=staging)
        )

        payload = parsed_stdout()
        # No structured error (no top-level ``error`` key on success).
        assert 'error' not in payload or payload.get('error') is not True
        assert payload['stores']['alpha']['status'] == 'integrated'
        assert len(payload['stores']['alpha']['annotations']) == 1

    def test_checksum_matches_accepted(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
    ):
        stores_dir = stores_dir_factory(('alpha',))
        staging = staging_dir_with_annotations(store_names=['alpha'])
        seg_file = staging / 'alpha' / 'segmentation.seg.nrrd'
        correct_checksum = server_cli.compute_sha256(seg_file)

        server_cli._shim_integrate_annotations(
            _integrate_argv(
                server_argv,
                stores_dir=stores_dir,
                staging_dir=staging,
                checksums=[f'{seg_file.name}:{correct_checksum}'],
            )
        )

        store_result = parsed_stdout()['stores']['alpha']
        assert store_result['status'] == 'integrated'


# ===========================================================================
# _run_integrate_annotations (error paths)
# ===========================================================================


class TestIntegrateAnnotationsErrors:
    """Covers error/rejection paths in _run_integrate_annotations."""

    def test_checksum_mismatch_fails_store_and_exits_nonzero(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
    ):
        """A checksum mismatch fails the store (task 2.8): the full per-store
        JSON is emitted and the batch exits non-zero — no mid-loop bare
        ``checksum_mismatch`` envelope."""
        stores_dir = stores_dir_factory(('alpha',))
        staging = staging_dir_with_annotations(store_names=['alpha'])

        bogus = f'sha256:{"0" * 64}'
        with pytest.raises(SystemExit) as excinfo:
            server_cli._shim_integrate_annotations(
                _integrate_argv(
                    server_argv,
                    stores_dir=stores_dir,
                    staging_dir=staging,
                    checksums=[f'segmentation.seg.nrrd:{bogus}'],
                )
            )
        assert excinfo.value.code == 1

        result = parsed_stdout()['stores']['alpha']
        assert result['status'] == 'failed'
        assert any('mismatch' in i['message'].lower() for i in result['issues'])
        # No annotation was written.
        assert _written_annotations(stores_dir / 'alpha.zarr') == []

    def test_unknown_ontology_warns_then_errors_on_type_mismatch(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
    ):
        """Declared ontology that can't be loaded produces a ``warning``
        issue (resolution failure) AND, because no other declared
        ontology remains to match the annotation's type, the per-
        annotation integration errors.  Under the explicit-intent
        policy we never silently downgrade to unconstrained — the
        client has to opt in via ``--unconstrained``."""
        stores_dir = stores_dir_factory(('alpha',))
        staging = staging_dir_with_annotations(store_names=['alpha'])

        # A failed store makes the batch exit non-zero (task 2.8).
        with pytest.raises(SystemExit) as excinfo:
            server_cli._run_integrate_annotations(
                _integrate_argv(
                    server_argv,
                    stores_dir=stores_dir,
                    staging_dir=staging,
                    expected_ontology=['does-not-exist'],
                )
            )
        assert excinfo.value.code == 1

        store_result = parsed_stdout()['stores']['alpha']

        warnings = [i for i in store_result['issues'] if i['severity'] == 'warning']
        assert any('does-not-exist' in w['message'] for w in warnings)

        errors = [i for i in store_result['issues'] if i['severity'] == 'error']
        assert any('No declared ontology matches' in e['message'] for e in errors)

        assert store_result['status'] == 'failed'
        assert _written_annotations(stores_dir / 'alpha.zarr') == []

    def test_segmentation_validation_error_without_force_blocks_write(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
    ):
        stores_dir = stores_dir_factory(('alpha',))
        # Shape mismatch: seg is (5,5,5), manifest declares SHAPE=(10,12,14).
        staging = staging_dir_with_annotations(
            store_names=['alpha'],
            seg_label_map=np.zeros((5, 5, 5), dtype=np.int16),
            seg_segments=[{'id': 's0', 'name': 'cochlea', 'label_value': 1}],
        )

        # A failed store makes the batch exit non-zero (task 2.8).
        with pytest.raises(SystemExit) as excinfo:
            server_cli._run_integrate_annotations(
                _integrate_argv(server_argv, stores_dir=stores_dir, staging_dir=staging)
            )
        assert excinfo.value.code == 1

        store_result = parsed_stdout()['stores']['alpha']
        assert store_result['status'] == 'failed'
        assert _written_annotations(stores_dir / 'alpha.zarr') == []
        errors = [i for i in store_result['issues'] if i['severity'] == 'error']
        assert errors

    def test_force_cannot_bypass_validation_errors(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
    ):
        """Error-severity issues always fail the store — ``--force`` may
        only accept warnings (arch plan A.2, decision 3).  No annotation
        group lands in zarr and no provenance line is written."""
        stores_dir = stores_dir_factory(('alpha',))
        staging = staging_dir_with_annotations(
            store_names=['alpha'],
            seg_label_map=np.zeros((5, 5, 5), dtype=np.int16),
            seg_segments=[{'id': 's0', 'name': 'cochlea', 'label_value': 1}],
        )

        with pytest.raises(SystemExit) as excinfo:
            server_cli._run_integrate_annotations(
                _integrate_argv(
                    server_argv, stores_dir=stores_dir, staging_dir=staging, force=True
                )
            )
        assert excinfo.value.code == 1

        store_result = parsed_stdout()['stores']['alpha']
        assert store_result['status'] == 'failed'
        assert store_result['code'] == 'validation_failed'
        assert _written_annotations(stores_dir / 'alpha.zarr') == []
        assert not (stores_dir / '.meta' / 'provenance.jsonl').exists()

    def test_warnings_only_with_force_integrates_and_stamps_forced(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
    ):
        """Warnings-only + force → integrated; the accepted warnings land
        in the provenance record together with ``forced: true``, and the
        annotation's zarr attrs carry ``forced: true``."""
        stores_dir = stores_dir_factory(('alpha',))
        # Label 1 present in the volume but not declared in the header:
        # warning-severity only under the unconstrained ontology.
        lm = np.zeros(SHAPE, dtype=np.int16)
        lm[0, 0, 0] = 1
        staging = staging_dir_with_annotations(
            store_names=['alpha'],
            seg_label_map=lm,
            seg_segments=[],
        )

        server_cli._run_integrate_annotations(
            _integrate_argv(
                server_argv,
                stores_dir=stores_dir,
                staging_dir=staging,
                unconstrained=True,
                force=True,
            )
        )

        store_result = parsed_stdout()['stores']['alpha']
        assert store_result['status'] == 'integrated'
        warnings = [i for i in store_result['issues'] if i['severity'] == 'warning']
        assert warnings

        jsonl_path = stores_dir / '.meta' / 'provenance.jsonl'
        lines = [
            json.loads(line)
            for line in jsonl_path.read_text().splitlines()
            if line.strip()
        ]
        assert len(lines) == 1
        record = lines[0]
        assert record['forced'] is True
        assert [i['severity'] for i in record['issues']] == ['warning']

        written = _written_annotations(stores_dir / 'alpha.zarr')
        assert len(written) == 1
        arr = zarr.open_array(written[0] / 'data', mode='r')
        assert dict(arr.attrs)['forced'] is True

    def test_warnings_only_without_force_integrates_without_forced_stamp(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
    ):
        """Warnings never block integration; without force there is no
        ``forced`` stamp anywhere — pre-A.2 records stay byte-identical."""
        stores_dir = stores_dir_factory(('alpha',))
        lm = np.zeros(SHAPE, dtype=np.int16)
        lm[0, 0, 0] = 1
        staging = staging_dir_with_annotations(
            store_names=['alpha'],
            seg_label_map=lm,
            seg_segments=[],
        )

        server_cli._run_integrate_annotations(
            _integrate_argv(
                server_argv,
                stores_dir=stores_dir,
                staging_dir=staging,
                unconstrained=True,
            )
        )

        store_result = parsed_stdout()['stores']['alpha']
        assert store_result['status'] == 'integrated'

        jsonl_path = stores_dir / '.meta' / 'provenance.jsonl'
        record = json.loads(jsonl_path.read_text().splitlines()[0])
        assert 'forced' not in record

        written = _written_annotations(stores_dir / 'alpha.zarr')
        arr = zarr.open_array(written[0] / 'data', mode='r')
        assert 'forced' not in dict(arr.attrs)

    def test_parse_error_recorded_in_issues(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
    ):
        stores_dir = stores_dir_factory(('alpha', 'bravo'))
        staging = staging_dir_with_annotations(store_names=['alpha', 'bravo'])
        # Corrupt alpha's seg.nrrd.
        (staging / 'alpha' / 'segmentation.seg.nrrd').write_bytes(b'NOT AN NRRD')

        # alpha fails while bravo integrates, so the batch exits non-zero
        # but still reports both stores (task 2.8).
        with pytest.raises(SystemExit) as excinfo:
            server_cli._run_integrate_annotations(
                _integrate_argv(server_argv, stores_dir=stores_dir, staging_dir=staging)
            )
        assert excinfo.value.code == 1

        payload = parsed_stdout()
        assert payload['stores']['alpha']['status'] == 'failed'
        errors = [
            i for i in payload['stores']['alpha']['issues'] if i['severity'] == 'error'
        ]
        assert errors
        # bravo unaffected.
        assert payload['stores']['bravo']['status'] == 'integrated'


# ===========================================================================
# _run_integrate_annotations — unconstrained constraint enforcement (A.1)
# ===========================================================================


def _non_sequential_label_map() -> np.ndarray:
    """A label map with labels [0, 1, 3] (gap at 2) -- violates
    ``sequential_from_zero`` under the unconstrained ontology."""
    lm = np.zeros(SHAPE, dtype=np.int16)
    lm[0, 0, 0] = 1
    lm[1, 1, 1] = 3
    return lm


class TestUnconstrainedConstraintEnforcement:
    """--unconstrained resolves the shipped ``unconstrained`` ontology so its
    structural constraints are enforced on the live server path (they were
    silently skipped when a bare ``None`` was passed).
    """

    def test_negative_label_is_error(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
    ):
        """Negative voxel value under --unconstrained fails the store with a
        constraint violation (fails before A.1: the constraint check was
        skipped; only the generic non-negativity message appeared). A failed
        store makes the batch exit non-zero (task 2.8)."""
        stores_dir = stores_dir_factory(('alpha',))
        lm = default_seg_label_map()
        lm[0, 0, 0] = -1
        staging = staging_dir_with_annotations(store_names=['alpha'], seg_label_map=lm)

        with pytest.raises(SystemExit) as excinfo:
            server_cli._run_integrate_annotations(
                _integrate_argv(
                    server_argv,
                    stores_dir=stores_dir,
                    staging_dir=staging,
                    unconstrained=True,
                )
            )
        assert excinfo.value.code == 1

        store_result = parsed_stdout()['stores']['alpha']
        assert store_result['status'] == 'failed'
        errors = [i for i in store_result['issues'] if i['severity'] == 'error']
        assert any(
            'constraint' in e['message'].lower() and 'negative' in e['message'].lower()
            for e in errors
        )
        assert _written_annotations(stores_dir / 'alpha.zarr') == []

    def test_non_sequential_labels_is_error(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
    ):
        """Non-sequential labels under --unconstrained fail the store (fails
        before A.1: with ontology=None the store integrated with only a gap
        warning). A failed store makes the batch exit non-zero (task 2.8)."""
        stores_dir = stores_dir_factory(('alpha',))
        lm = np.zeros(SHAPE, dtype=np.int16)
        lm[0, 0, 0] = 1
        lm[1, 1, 1] = 3  # skips 2 -> not sequential from zero
        staging = staging_dir_with_annotations(
            store_names=['alpha'],
            seg_label_map=lm,
            seg_segments=[
                {'id': 's0', 'name': 'a', 'label_value': 1},
                {'id': 's1', 'name': 'c', 'label_value': 3},
            ],
        )

        with pytest.raises(SystemExit) as excinfo:
            server_cli._run_integrate_annotations(
                _integrate_argv(
                    server_argv,
                    stores_dir=stores_dir,
                    staging_dir=staging,
                    unconstrained=True,
                )
            )
        assert excinfo.value.code == 1

        store_result = parsed_stdout()['stores']['alpha']
        assert store_result['status'] == 'failed'
        errors = [i for i in store_result['issues'] if i['severity'] == 'error']
        assert any('sequential' in e['message'].lower() for e in errors)
        assert _written_annotations(stores_dir / 'alpha.zarr') == []

    @pytest.mark.parametrize(
        'seg_label_map',
        [
            None,  # valid default seg -> no issues
            _non_sequential_label_map(),  # constraint-violating -> issues
        ],
        ids=['valid', 'non_sequential'],
    )
    def test_server_and_direct_validate_agree(
        self,
        seg_label_map,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
    ):
        """The server integrate path and a direct ``validate_seg_preflight``
        call produce identical issue lists for the same input. A failed
        (constraint-violating) store makes the batch exit non-zero (task
        2.8); the valid store integrates and exits zero."""
        stores_dir = stores_dir_factory(('alpha',))
        seg_segments = (
            None
            if seg_label_map is None
            else [
                {'id': 's0', 'name': 'a', 'label_value': 1},
                {'id': 's1', 'name': 'c', 'label_value': 3},
            ]
        )
        staging = staging_dir_with_annotations(
            store_names=['alpha'],
            seg_label_map=seg_label_map,
            seg_segments=seg_segments,
        )

        argv = _integrate_argv(
            server_argv,
            stores_dir=stores_dir,
            staging_dir=staging,
            unconstrained=True,
        )
        if seg_label_map is None:
            server_cli._run_integrate_annotations(argv)
        else:
            with pytest.raises(SystemExit) as excinfo:
                server_cli._run_integrate_annotations(argv)
            assert excinfo.value.code == 1
        server_issues = parsed_stdout()['stores']['alpha']['issues']

        # Reconstruct the exact spatial metadata the server passed in.
        root = zarr.open_group(stores_dir / 'alpha.zarr', mode='r')
        arr = root['raw']['full']
        origin, space_directions, spacing_mm = extract_spatial_metadata(dict(arr.attrs))
        manifest_entry = {
            'shape': list(arr.shape),
            'origin_lps': origin.tolist(),
            'space_directions': space_directions.tolist(),
            'spacing_mm': spacing_mm,
        }
        seg_file = staging / 'alpha' / 'segmentation.seg.nrrd'
        direct = validate_seg_preflight(
            seg_file, manifest_entry, UNCONSTRAINED_SEGMENTATION
        )
        direct_issues = [{'severity': i.severity, 'message': i.message} for i in direct]

        assert server_issues == direct_issues


# ===========================================================================
# _run_integrate_annotations (checksum fail-closed — task 2.5)
# ===========================================================================


class TestIntegrateChecksumFailClosed:
    """Covers fail-closed checksum verification (launch task 2.5).

    When the client supplies ``--checksums`` it asserts integrity of every
    uploaded annotation file.  A file missing from the set, or a malformed
    token, silently disables the check otherwise — so the owning store is
    failed closed instead of integrated unverified.
    """

    def test_missing_checksum_entry_fails_store_closed(
        self, stores_dir_factory, staging_dir_with_annotations, server_argv, parsed_stdout
    ):
        stores_dir = stores_dir_factory(('alpha',))
        staging = staging_dir_with_annotations(
            store_names=['alpha'], include_seg=True, include_lmk=True
        )
        seg_file = staging / 'alpha' / 'segmentation.seg.nrrd'
        seg_ck = server_cli.compute_sha256(seg_file)
        # Checksum supplied for the segmentation but NOT the landmarks file.
        # A failed store makes the batch exit non-zero (task 2.8).
        with pytest.raises(SystemExit) as excinfo:
            server_cli._shim_integrate_annotations(
                _integrate_argv(
                    server_argv,
                    stores_dir=stores_dir,
                    staging_dir=staging,
                    expected_ontology=['inner-ear-structures', 'inner-ear-landmarks'],
                    checksums=[f'{seg_file.name}:{seg_ck}'],
                )
            )
        assert excinfo.value.code == 1

        result = parsed_stdout()['stores']['alpha']
        assert result['status'] == 'failed'
        errors = [i for i in result['issues'] if i['severity'] == 'error']
        assert any(
            'landmarks.mrk.json' in e['message'] and 'checksum' in e['message'].lower()
            for e in errors
        )
        assert _written_annotations(stores_dir / 'alpha.zarr') == []

    def test_malformed_checksum_token_fails_store_closed(
        self, stores_dir_factory, staging_dir_with_annotations, server_argv, parsed_stdout
    ):
        stores_dir = stores_dir_factory(('alpha',))
        staging = staging_dir_with_annotations(store_names=['alpha'])
        # ``segmentation.seg.nrrd:garbage`` splits into two colon-parts, so
        # it is not a well-formed ``<name>:sha256:<hex>`` token.  A failed
        # store makes the batch exit non-zero (task 2.8).
        with pytest.raises(SystemExit) as excinfo:
            server_cli._shim_integrate_annotations(
                _integrate_argv(
                    server_argv,
                    stores_dir=stores_dir,
                    staging_dir=staging,
                    checksums=['segmentation.seg.nrrd:garbage'],
                )
            )
        assert excinfo.value.code == 1

        result = parsed_stdout()['stores']['alpha']
        assert result['status'] == 'failed'
        errors = [i for i in result['issues'] if i['severity'] == 'error']
        assert any('malformed' in e['message'].lower() for e in errors)
        assert _written_annotations(stores_dir / 'alpha.zarr') == []


# ===========================================================================
# _run_integrate_annotations (multi-store)
# ===========================================================================


class TestIntegrateAnnotationsMultiStore:
    """Covers multi-store behavior in _run_integrate_annotations."""

    def test_partial_failure_per_store_isolated(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
    ):
        stores_dir = stores_dir_factory(('alpha', 'bravo'))
        staging = staging_dir_with_annotations(store_names=['alpha', 'bravo'])
        # Bravo's seg has a shape mismatch.
        write_seg_nrrd(
            staging / 'bravo' / 'segmentation.seg.nrrd',
            np.zeros((4, 4, 4), dtype=np.int16),
            [{'id': 's0', 'name': 'cochlea', 'label_value': 1}],
        )

        # alpha integrates, bravo fails; the batch exits non-zero but the
        # response still reports both stores (task 2.8).
        with pytest.raises(SystemExit) as excinfo:
            server_cli._run_integrate_annotations(
                _integrate_argv(server_argv, stores_dir=stores_dir, staging_dir=staging)
            )
        assert excinfo.value.code == 1

        payload = parsed_stdout()
        assert payload['stores']['alpha']['status'] == 'integrated'
        assert payload['stores']['bravo']['status'] == 'failed'
        assert len(_written_annotations(stores_dir / 'alpha.zarr')) == 1
        assert _written_annotations(stores_dir / 'bravo.zarr') == []

    def test_iteration_order_deterministic(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
    ):
        stores_dir = stores_dir_factory(('alpha', 'bravo', 'charlie'))
        staging = staging_dir_with_annotations(store_names=['charlie', 'alpha', 'bravo'])

        server_cli._run_integrate_annotations(
            _integrate_argv(server_argv, stores_dir=stores_dir, staging_dir=staging)
        )

        payload = parsed_stdout()
        assert list(payload['stores']) == ['alpha', 'bravo', 'charlie']

    def test_skips_hidden_directories(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
    ):
        stores_dir = stores_dir_factory(('alpha',))
        staging = staging_dir_with_annotations(store_names=['alpha'])
        hidden = staging / '.hidden'
        hidden.mkdir()
        (hidden / 'segmentation.seg.nrrd').write_bytes(b'junk')

        server_cli._run_integrate_annotations(
            _integrate_argv(server_argv, stores_dir=stores_dir, staging_dir=staging)
        )

        payload = parsed_stdout()
        assert list(payload['stores']) == ['alpha']

    def test_skips_directories_without_matching_zarr_store(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
    ):
        stores_dir = stores_dir_factory(('alpha',))
        staging = staging_dir_with_annotations(store_names=['alpha', 'orphan'])

        server_cli._run_integrate_annotations(
            _integrate_argv(server_argv, stores_dir=stores_dir, staging_dir=staging)
        )

        payload = parsed_stdout()
        # Only alpha appears; orphan has no zarr store to integrate into.
        assert list(payload['stores']) == ['alpha']


# ===========================================================================
# _run_integrate_annotations (loop robustness — task 2.8)
# ===========================================================================


class TestIntegrateLoopRobustness:
    """Covers non-atomic multi-store push robustness (launch task 2.8)."""

    def test_bad_checksum_in_second_store_isolates_failure_and_exits_1(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
    ):
        """A checksum mismatch in a later store no longer aborts the loop
        mid-way: the earlier store still integrates, the response reports
        both statuses, and the batch exits non-zero."""
        stores_dir = stores_dir_factory(('alpha', 'bravo'))
        staging = staging_dir_with_annotations(store_names=['alpha', 'bravo'])
        # alpha keeps the default seg; give bravo a different (still valid)
        # seg so it mismatches the shared basename checksum.
        alpha_seg = staging / 'alpha' / 'segmentation.seg.nrrd'
        correct_ck = server_cli.compute_sha256(alpha_seg)
        write_seg_nrrd(
            staging / 'bravo' / 'segmentation.seg.nrrd',
            np.ones(SHAPE, dtype=np.int16),
            [{'id': 's0', 'name': 'cochlea', 'label_value': 1}],
        )

        with pytest.raises(SystemExit) as excinfo:
            server_cli._shim_integrate_annotations(
                _integrate_argv(
                    server_argv,
                    stores_dir=stores_dir,
                    staging_dir=staging,
                    checksums=[f'segmentation.seg.nrrd:{correct_ck}'],
                )
            )
        assert excinfo.value.code == 1

        payload = parsed_stdout()
        assert payload['stores']['alpha']['status'] == 'integrated'
        assert payload['stores']['bravo']['status'] == 'failed'
        assert any(
            'mismatch' in i['message'].lower()
            for i in payload['stores']['bravo']['issues']
        )
        assert len(_written_annotations(stores_dir / 'alpha.zarr')) == 1
        assert _written_annotations(stores_dir / 'bravo.zarr') == []

    def test_reference_subdir_is_never_integrated(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
    ):
        """A ``reference/`` staging subdir (pulled reference annotations) is
        skipped in store discovery even when a same-named zarr store
        exists."""
        stores_dir = stores_dir_factory(('alpha', 'reference'))
        staging = staging_dir_with_annotations(store_names=['alpha', 'reference'])

        server_cli._run_integrate_annotations(
            _integrate_argv(server_argv, stores_dir=stores_dir, staging_dir=staging)
        )

        payload = parsed_stdout()
        assert payload['stores']['alpha']['status'] == 'integrated'
        assert 'reference' not in payload['stores']


# ===========================================================================
# _run_integrate_annotations (ontology resolution)
# ===========================================================================


class TestIntegrateAnnotationsOntology:
    """Covers _resolve_ontologies + ontology selection logic."""

    def test_segmentation_ontology_resolution_filters_by_type(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
    ):
        stores_dir = stores_dir_factory(('alpha',))
        staging = staging_dir_with_annotations(
            store_names=['alpha'],
            include_seg=True,
            include_lmk=True,
        )

        server_cli._run_integrate_annotations(
            _integrate_argv(
                server_argv,
                stores_dir=stores_dir,
                staging_dir=staging,
                expected_ontology=['inner-ear-structures', 'inner-ear-landmarks'],
            )
        )

        annotations = parsed_stdout()['stores']['alpha']['annotations']
        by_ontology = {a['ontology']: a for a in annotations}
        assert 'inner-ear-structures' in by_ontology
        assert 'inner-ear-landmarks' in by_ontology

    def test_first_matching_ontology_used(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
    ):
        stores_dir = stores_dir_factory(('alpha',))
        # Pin the documented behavior: given two ontologies that both
        # match the annotation's type, the first one declared wins.
        staging = staging_dir_with_annotations(store_names=['alpha'])

        server_cli._run_integrate_annotations(
            _integrate_argv(
                server_argv,
                stores_dir=stores_dir,
                staging_dir=staging,
                expected_ontology=[
                    'inner-ear-structures',
                    'inner-ear-total-fluid-space',
                ],
            )
        )

        store_result = parsed_stdout()['stores']['alpha']
        assert store_result['annotations'][0]['ontology'] == ('inner-ear-structures')

    def test_declared_ontology_type_mismatch_errors_no_silent_fallback(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
    ):
        """Declaring only a landmarks ontology while the session ships a
        segmentation must error — silent unconstrained-fallback would
        corrupt the ground-truth provenance record.  The error message
        points the client at ``--unconstrained`` as the explicit escape
        hatch."""
        stores_dir = stores_dir_factory(('alpha',))
        staging = staging_dir_with_annotations(
            store_names=['alpha'],
            include_seg=True,
            include_lmk=False,
        )

        # A failed store makes the batch exit non-zero (task 2.8).
        with pytest.raises(SystemExit) as excinfo:
            server_cli._run_integrate_annotations(
                _integrate_argv(
                    server_argv,
                    stores_dir=stores_dir,
                    staging_dir=staging,
                    expected_ontology=['inner-ear-landmarks'],
                )
            )
        assert excinfo.value.code == 1

        store_result = parsed_stdout()['stores']['alpha']
        assert store_result['status'] == 'failed'
        errors = [i for i in store_result['issues'] if i['severity'] == 'error']
        assert any(
            'No declared ontology matches annotation type segmentation' in e['message']
            for e in errors
        )
        assert any('--unconstrained' in e['message'] for e in errors)
        assert _written_annotations(stores_dir / 'alpha.zarr') == []

    def test_unconstrained_flag_integrates_without_ontology(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
    ):
        """Explicit --unconstrained opts the session out of ontology
        enforcement.  Integration succeeds and provenance records the
        ontology as ``'unconstrained'`` (not a silent default)."""
        stores_dir = stores_dir_factory(('alpha',))
        staging = staging_dir_with_annotations(store_names=['alpha'])

        server_cli._run_integrate_annotations(
            _integrate_argv(
                server_argv,
                stores_dir=stores_dir,
                staging_dir=staging,
                unconstrained=True,
            )
        )

        store_result = parsed_stdout()['stores']['alpha']
        assert store_result['status'] == 'integrated'
        ann = store_result['annotations'][0]
        assert ann['ontology'] == 'unconstrained'

        written = _written_annotations(stores_dir / 'alpha.zarr')[0]
        arr = zarr.open_array(written / 'data', mode='r')
        assert dict(arr.attrs)['ontology'] == 'unconstrained'

    def test_neither_ontology_flag_nor_unconstrained_errors(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
    ):
        """Omitting both --expected-ontology and --unconstrained is a
        client bug (no declared intent) — integrate exits with a
        structured ``ontology_not_declared`` envelope rather than
        silently defaulting."""
        stores_dir = stores_dir_factory(('alpha',))
        staging = staging_dir_with_annotations(store_names=['alpha'])

        with pytest.raises(SystemExit) as excinfo:
            server_cli._run_integrate_annotations(
                _integrate_argv(
                    server_argv,
                    stores_dir=stores_dir,
                    staging_dir=staging,
                    expected_ontology=[],
                    unconstrained=False,
                )
            )
        assert excinfo.value.code == 1

        envelope = parsed_stdout()
        assert envelope['error'] is True
        assert envelope['code'] == 'ontology_not_declared'
        assert _written_annotations(stores_dir / 'alpha.zarr') == []

    def test_both_ontology_flag_and_unconstrained_errors(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
    ):
        """--expected-ontology and --unconstrained are mutually
        exclusive; passing both is ambiguous and rejected."""
        stores_dir = stores_dir_factory(('alpha',))
        staging = staging_dir_with_annotations(store_names=['alpha'])

        with pytest.raises(SystemExit) as excinfo:
            server_cli._run_integrate_annotations(
                _integrate_argv(
                    server_argv,
                    stores_dir=stores_dir,
                    staging_dir=staging,
                    expected_ontology=['inner-ear-structures'],
                    unconstrained=True,
                )
            )
        assert excinfo.value.code == 1

        envelope = parsed_stdout()
        assert envelope['error'] is True
        assert envelope['code'] == 'ambiguous_ontology_spec'
        assert _written_annotations(stores_dir / 'alpha.zarr') == []


# ===========================================================================
# _run_integrate_annotations (catalog cache invalidation — PR 3)
# ===========================================================================


class TestIntegrateAnnotationsCacheInvalidation:
    """Covers write-through invalidation of the catalog cache.

    After a successful integrate the next ``list-stores`` call must observe
    the newly-written annotation immediately -- no TTL wait -- and the
    ``catalog_version`` must increase monotonically per touched store.
    """

    def test_successful_integrate_bumps_catalog_version_and_shows_annotation(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
    ):
        stores_dir = stores_dir_factory(('alpha',))
        staging = staging_dir_with_annotations(store_names=['alpha'])

        server_cli._run_list_stores(server_argv(stores_dir=stores_dir))
        first = parsed_stdout()
        assert first['stores'][0]['annotations'] == []
        initial_version = first['catalog_version']

        server_cli._run_integrate_annotations(
            _integrate_argv(server_argv, stores_dir=stores_dir, staging_dir=staging)
        )
        parsed_stdout()  # drain integrate output

        server_cli._run_list_stores(server_argv(stores_dir=stores_dir))
        second = parsed_stdout()

        assert second['catalog_version'] == initial_version + 1
        alpha = next(s for s in second['stores'] if s['name'] == 'alpha')
        assert len(alpha['annotations']) == 1
        assert alpha['annotations'][0]['annotator_id'] == 'alice'
        assert alpha['annotations'][0]['ontology'] == 'inner-ear-structures'

    def test_integrate_writing_nothing_leaves_catalog_version_untouched(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
    ):
        stores_dir = stores_dir_factory(('alpha',))
        # Shape mismatch + force=False => validation errors block all writes.
        staging = staging_dir_with_annotations(
            store_names=['alpha'],
            seg_label_map=np.zeros((5, 5, 5), dtype=np.int16),
            seg_segments=[{'id': 's0', 'name': 'cochlea', 'label_value': 1}],
        )

        server_cli._run_list_stores(server_argv(stores_dir=stores_dir))
        first = parsed_stdout()
        initial_version = first['catalog_version']

        # The store fails, so the batch exits non-zero (task 2.8).
        with pytest.raises(SystemExit) as excinfo:
            server_cli._run_integrate_annotations(
                _integrate_argv(server_argv, stores_dir=stores_dir, staging_dir=staging)
            )
        assert excinfo.value.code == 1
        store_result = parsed_stdout()['stores']['alpha']
        assert store_result['status'] == 'failed'

        server_cli._run_list_stores(server_argv(stores_dir=stores_dir))
        second = parsed_stdout()

        assert second['catalog_version'] == initial_version

    def test_multi_store_integrate_bumps_version_once_per_store(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
    ):
        stores_dir = stores_dir_factory(('alpha', 'bravo', 'charlie'))
        staging = staging_dir_with_annotations(store_names=['alpha', 'bravo', 'charlie'])

        server_cli._run_list_stores(server_argv(stores_dir=stores_dir))
        initial_version = parsed_stdout()['catalog_version']

        server_cli._run_integrate_annotations(
            _integrate_argv(server_argv, stores_dir=stores_dir, staging_dir=staging)
        )
        integrate_payload = parsed_stdout()
        touched = [
            name
            for name, result in integrate_payload['stores'].items()
            if result['status'] == 'integrated'
        ]
        assert set(touched) == {'alpha', 'bravo', 'charlie'}

        server_cli._run_list_stores(server_argv(stores_dir=stores_dir))
        final = parsed_stdout()

        assert final['catalog_version'] == initial_version + len(touched)
        for store in final['stores']:
            assert len(store['annotations']) == 1
            assert store['annotations'][0]['annotator_id'] == 'alice'

    def test_invalidate_failure_is_non_fatal(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from voxhub_core.server import catalog_cache

        stores_dir = stores_dir_factory(('alpha',))
        staging = staging_dir_with_annotations(store_names=['alpha'])

        def boom(*_args: object, **_kwargs: object) -> None:
            raise catalog_cache.CacheLockError('simulated lock timeout')

        monkeypatch.setattr(catalog_cache, 'invalidate_store', boom)

        with caplog.at_level('WARNING'):
            server_cli._run_integrate_annotations(
                _integrate_argv(server_argv, stores_dir=stores_dir, staging_dir=staging)
            )

        store_result = parsed_stdout()['stores']['alpha']
        assert store_result['status'] == 'integrated'
        assert len(_written_annotations(stores_dir / 'alpha.zarr')) == 1

        events = [
            r.msg
            for r in caplog.records
            if isinstance(r.msg, dict)
            and r.msg.get('event') == 'catalog_invalidate_failed'
        ]
        assert events, 'expected a catalog_invalidate_failed warning'
        assert events[0]['store'] == 'alpha'
        assert 'simulated lock timeout' in events[0]['error']


# ===========================================================================
# _run_integrate_annotations / _run_prepare_pull — key-bound identity (plan §B)
# ===========================================================================


def _jsonl_records(stores_dir: Path) -> list[dict]:
    """Return parsed provenance JSONL lines, or [] when the file is absent."""
    jsonl = stores_dir / '.meta' / 'provenance.jsonl'
    if not jsonl.is_file():
        return []
    return [json.loads(line) for line in jsonl.read_text().splitlines() if line.strip()]


class TestIntegrateAnnotationsIdentity:
    """The connecting SSH key's VOXHUB_ANNOTATOR binds annotator identity.

    Over SSH the env var (injected by sshd from the key's environment=
    option) is authoritative and overrides the client-sent --annotator-id;
    a disagreement fails the request; without the env var (local/dev) the
    flag is used exactly as before.
    """

    def test_env_var_alone_provides_identity_with_ssh_key_source(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
        monkeypatch,
    ):
        # "env set + no flag": the key binding alone provides the identity.
        monkeypatch.setenv('VOXHUB_ANNOTATOR', 'carol')
        stores_dir = stores_dir_factory(('alpha',))
        staging = staging_dir_with_annotations(store_names=['alpha'])

        server_cli._run_integrate_annotations(
            _integrate_argv(
                server_argv,
                stores_dir=stores_dir,
                staging_dir=staging,
                annotator_id=None,
            )
        )

        payload = parsed_stdout()
        ann = payload['stores']['alpha']['annotations'][0]
        # Annotator-scoped path uses the key-bound identity, not the flag.
        assert ann['path'].startswith('annotations/carol-deadbeef/')

        records = _jsonl_records(stores_dir)
        assert len(records) == 1
        assert records[0]['annotator_id'] == 'carol'
        assert records[0]['identity_source'] == 'ssh_key'

        written = _written_annotations(stores_dir / 'alpha.zarr')[0]
        arr_attrs = dict(zarr.open_array(written / 'data', mode='r').attrs)
        assert arr_attrs['annotator_id'] == 'carol'
        assert arr_attrs['identity_source'] == 'ssh_key'

    def test_env_var_matching_flag_stamps_ssh_key_source(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
        monkeypatch,
    ):
        # Realistic client: sends --annotator-id equal to the key binding.
        monkeypatch.setenv('VOXHUB_ANNOTATOR', 'alice')
        stores_dir = stores_dir_factory(('alpha',))
        staging = staging_dir_with_annotations(store_names=['alpha'])

        server_cli._run_integrate_annotations(
            _integrate_argv(
                server_argv,
                stores_dir=stores_dir,
                staging_dir=staging,
                annotator_id='alice',
            )
        )
        parsed_stdout()

        records = _jsonl_records(stores_dir)
        assert records[0]['annotator_id'] == 'alice'
        assert records[0]['identity_source'] == 'ssh_key'

    def test_env_var_disagreeing_with_flag_fails_and_writes_nothing(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
        monkeypatch,
    ):
        monkeypatch.setenv('VOXHUB_ANNOTATOR', 'alice')
        stores_dir = stores_dir_factory(('alpha',))
        staging = staging_dir_with_annotations(store_names=['alpha'])

        with pytest.raises(SystemExit) as exc_info:
            server_cli._run_integrate_annotations(
                _integrate_argv(
                    server_argv,
                    stores_dir=stores_dir,
                    staging_dir=staging,
                    annotator_id='mallory',
                )
            )
        assert exc_info.value.code == 1

        envelope = parsed_stdout()
        assert envelope['error'] is True
        assert envelope['code'] == 'identity_mismatch'

        # Nothing landed: no annotations group, no provenance line.
        assert _written_annotations(stores_dir / 'alpha.zarr') == []
        assert _jsonl_records(stores_dir) == []

    def test_env_var_unset_uses_flag_with_flag_source(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
        monkeypatch,
    ):
        monkeypatch.delenv('VOXHUB_ANNOTATOR', raising=False)
        stores_dir = stores_dir_factory(('alpha',))
        staging = staging_dir_with_annotations(store_names=['alpha'])

        server_cli._run_integrate_annotations(
            _integrate_argv(
                server_argv,
                stores_dir=stores_dir,
                staging_dir=staging,
                annotator_id='alice',
            )
        )
        payload = parsed_stdout()
        ann = payload['stores']['alpha']['annotations'][0]
        assert ann['path'].startswith('annotations/alice-deadbeef/')

        records = _jsonl_records(stores_dir)
        assert records[0]['annotator_id'] == 'alice'
        assert records[0]['identity_source'] == 'flag'

        written = _written_annotations(stores_dir / 'alpha.zarr')[0]
        arr_attrs = dict(zarr.open_array(written / 'data', mode='r').attrs)
        assert arr_attrs['identity_source'] == 'flag'


class TestPreparePullIdentity:
    """prepare-pull resolves the same key-bound identity so the pull session
    (whose staging-dir name becomes the pull_session_id in later integrate
    provenance) is attributable to the connecting key."""

    def test_env_var_disagreeing_with_flag_fails(
        self,
        stores_dir_factory,
        server_argv,
        parsed_stdout,
        monkeypatch,
    ):
        monkeypatch.setenv('VOXHUB_ANNOTATOR', 'alice')
        root = stores_dir_factory(('alpha',))

        with pytest.raises(SystemExit) as exc_info:
            server_cli._run_prepare_pull(
                server_argv(stores_dir=root, store='alpha', annotator_id='mallory')
            )
        assert exc_info.value.code == 1

        envelope = parsed_stdout()
        assert envelope['error'] is True
        assert envelope['code'] == 'identity_mismatch'

    def test_env_var_logs_ssh_key_source(
        self,
        stores_dir_factory,
        server_argv,
        parsed_stdout,
        caplog,
        monkeypatch,
    ):
        import logging

        caplog.set_level(logging.INFO, logger='voxhub_core.server.cli')
        monkeypatch.setenv('VOXHUB_ANNOTATOR', 'carol')
        root = stores_dir_factory(('alpha',))

        server_cli._run_prepare_pull(
            server_argv(stores_dir=root, store='alpha', annotator_id=None)
        )
        payload = parsed_stdout()
        shutil.rmtree(Path(payload['staging_dir']), ignore_errors=True)

        started = [
            r.msg
            for r in caplog.records
            if isinstance(r.msg, dict) and r.msg.get('event') == 'prepare_pull_started'
        ]
        assert started, 'expected a prepare_pull_started log event'
        assert started[0]['annotator_id'] == 'carol'
        assert started[0]['identity_source'] == 'ssh_key'


# ===========================================================================
# _run_cleanup
# ===========================================================================


class TestCleanup:
    """Covers voxhub_core.server.cli._run_cleanup."""

    def test_removes_existing_staging_dir(self, tmp_path, server_argv, parsed_stdout):
        staging = tmp_path / 'vxhb-staging-abc'
        staging.mkdir()
        (staging / 'payload').write_text('data')

        server_cli._run_cleanup(server_argv(staging_dir=str(staging)))

        payload = parsed_stdout()
        assert payload['status'] == 'ok'
        assert not staging.exists()

    def test_emits_staging_dir_reaped_event_on_ack(
        self, tmp_path, server_argv, parsed_stdout, caplog
    ):
        """Successful cleanup emits ``staging_dir_reaped`` with
        ``reason='client_ack'`` for observability."""
        from voxhub_schema import PROTOCOL_VERSION, PullManifest

        staging = tmp_path / 'vxhb-staging-abc'
        staging.mkdir()
        PullManifest(
            protocol_version=PROTOCOL_VERSION,
            prepared_at='2026-04-14T12:00:00+00:00',
            server_host='h',
            server_stores_dir='/s',
            store_name='patient-007',
            raw_name='raw.nrrd',
            raw_checksum='sha256:x',
            shape=[1, 1, 1],
            spacing_mm=[1.0, 1.0, 1.0],
            origin_lps=[0.0, 0.0, 0.0],
            space_directions=[[1, 0, 0], [0, 1, 0], [0, 0, 1]],
        ).write(staging)

        with caplog.at_level('INFO'):
            server_cli._run_cleanup(server_argv(staging_dir=str(staging)))
        parsed_stdout()

        reaped_events = [
            r for r in caplog.records if "'event': 'staging_dir_reaped'" in r.message
        ]
        assert len(reaped_events) == 1
        msg = reaped_events[0].message
        assert "'reason': 'client_ack'" in msg
        assert "'store_name': 'patient-007'" in msg
        assert "'had_manifest': True" in msg

    def test_noop_when_staging_dir_missing(self, tmp_path, server_argv, parsed_stdout):
        """A prefix-valid, inside-root path that no longer exists on disk
        is a noop (client retry after crash-mid-cleanup)."""
        missing = tmp_path / 'vxhb-staging-does-not-exist'

        server_cli._run_cleanup(server_argv(staging_dir=str(missing)))

        payload = parsed_stdout()
        assert payload['status'] == 'ok'

    def test_refuses_path_without_staging_prefix(
        self, tmp_path, server_argv, parsed_stdout
    ):
        """A dir inside the staging root but whose basename lacks the
        ``vxhb-staging-`` prefix is rejected — this is what stops a
        buggy/malicious client from cleaning up arbitrary tempdir
        siblings (pytest's own dirs, other daemons' state, etc.)."""
        suspicious = tmp_path / 'user-data'
        suspicious.mkdir()

        with pytest.raises(SystemExit) as excinfo:
            server_cli._run_cleanup(server_argv(staging_dir=str(suspicious)))
        assert excinfo.value.code == 1

        envelope = parsed_stdout()
        assert envelope['error'] is True
        assert envelope['code'] == 'invalid_staging_dir'
        # The directory must still exist — no rmtree happened.
        assert suspicious.exists()

    def test_refuses_path_outside_staging_root(
        self, tmp_path, server_argv, parsed_stdout
    ):
        """A prefix-valid path that resolves outside the configured
        staging root is rejected — closes the ``cleanup ~/.ssh`` hole
        that motivated the contract change."""
        # Force the staging_root to a narrow sub-dir of tmp_path, then
        # point at a sibling that carries the prefix but is outside.
        narrow_root = tmp_path / 'narrow_root'
        narrow_root.mkdir()
        outside = tmp_path / 'vxhb-staging-outside'
        outside.mkdir()

        with pytest.raises(SystemExit) as excinfo:
            server_cli._run_cleanup(
                server_argv(
                    staging_dir=str(outside),
                    staging_root=str(narrow_root),
                )
            )
        assert excinfo.value.code == 1

        envelope = parsed_stdout()
        assert envelope['error'] is True
        assert envelope['code'] == 'invalid_staging_dir'
        assert outside.exists()


# ===========================================================================
# _run_gc
# ===========================================================================


class TestGc:
    """Covers voxhub_core.server.cli._run_gc.

    Isolation: ``_run_gc`` scans ``args.staging_root`` (populated from
    ``settings.storage.staging_dir`` in production).  Every test points
    that at a per-test ``tmp_path`` sub-directory so the real staging
    root is never touched.
    """

    @staticmethod
    def _staging_root(fake_tmp: Path) -> Path:
        fake_tmp.mkdir(exist_ok=True)
        return fake_tmp

    def test_removes_dirs_older_than_ttl(self, tmp_path, server_argv, parsed_stdout):
        fake_tmp = self._staging_root(tmp_path / 'fake_tmp')
        old = fake_tmp / 'vxhb-staging-old'
        old.mkdir()
        # 48 hours in the past.
        import os as _os

        old_ts = old.stat().st_mtime - 48 * 3600
        _os.utime(old, (old_ts, old_ts))

        server_cli._run_gc(server_argv(ttl_hours=24.0, staging_root=str(fake_tmp)))

        payload = parsed_stdout()
        assert str(old) in payload['removed']
        assert payload['count'] == 1
        assert not old.exists()

    def test_emits_staging_dir_reaped_event_on_gc(
        self, tmp_path, server_argv, parsed_stdout, caplog
    ):
        """GC reap emits ``staging_dir_reaped`` with ``reason='gc_unacked'``."""
        from voxhub_schema import PROTOCOL_VERSION, PullManifest

        fake_tmp = self._staging_root(tmp_path / 'fake_tmp')
        old = fake_tmp / 'vxhb-staging-old'
        old.mkdir()
        PullManifest(
            protocol_version=PROTOCOL_VERSION,
            prepared_at='2026-04-14T12:00:00+00:00',
            server_host='h',
            server_stores_dir='/s',
            store_name='patient-013',
            raw_name='raw.nrrd',
            raw_checksum='sha256:x',
            shape=[1, 1, 1],
            spacing_mm=[1.0, 1.0, 1.0],
            origin_lps=[0.0, 0.0, 0.0],
            space_directions=[[1, 0, 0], [0, 1, 0], [0, 0, 1]],
        ).write(old)

        import os as _os

        old_ts = old.stat().st_mtime - 48 * 3600
        _os.utime(old, (old_ts, old_ts))

        with caplog.at_level('INFO'):
            server_cli._run_gc(server_argv(ttl_hours=24.0, staging_root=str(fake_tmp)))
        parsed_stdout()

        reaped_events = [
            r for r in caplog.records if "'event': 'staging_dir_reaped'" in r.message
        ]
        assert len(reaped_events) == 1
        msg = reaped_events[0].message
        assert "'reason': 'gc_unacked'" in msg
        assert "'store_name': 'patient-013'" in msg
        assert "'had_manifest': True" in msg

    def test_keeps_dirs_newer_than_ttl(self, tmp_path, server_argv, parsed_stdout):
        fake_tmp = self._staging_root(tmp_path / 'fake_tmp')
        recent = fake_tmp / 'vxhb-staging-recent'
        recent.mkdir()

        server_cli._run_gc(server_argv(ttl_hours=24.0, staging_root=str(fake_tmp)))

        payload = parsed_stdout()
        assert payload['count'] == 0
        assert recent.exists()

    def test_ignores_non_staging_prefix(self, tmp_path, server_argv, parsed_stdout):
        fake_tmp = self._staging_root(tmp_path / 'fake_tmp')
        other = fake_tmp / 'foo-bar'
        other.mkdir()
        import os as _os

        old_ts = other.stat().st_mtime - 48 * 3600
        _os.utime(other, (old_ts, old_ts))

        server_cli._run_gc(server_argv(ttl_hours=24.0, staging_root=str(fake_tmp)))

        payload = parsed_stdout()
        assert payload['count'] == 0
        assert other.exists()

    def test_ignores_files_only_dirs(self, tmp_path, server_argv, parsed_stdout):
        fake_tmp = self._staging_root(tmp_path / 'fake_tmp')
        staging_file = fake_tmp / 'vxhb-staging-file'
        staging_file.write_text('data')
        import os as _os

        old_ts = staging_file.stat().st_mtime - 48 * 3600
        _os.utime(staging_file, (old_ts, old_ts))

        server_cli._run_gc(server_argv(ttl_hours=24.0, staging_root=str(fake_tmp)))

        payload = parsed_stdout()
        assert payload['count'] == 0
        assert staging_file.exists()

    def test_count_matches_removed_length(self, tmp_path, server_argv, parsed_stdout):
        fake_tmp = self._staging_root(tmp_path / 'fake_tmp')
        import os as _os

        for name in ('vxhb-staging-a', 'vxhb-staging-b', 'vxhb-staging-c'):
            p = fake_tmp / name
            p.mkdir()
            _os.utime(p, (p.stat().st_mtime - 48 * 3600,) * 2)

        server_cli._run_gc(server_argv(ttl_hours=24.0, staging_root=str(fake_tmp)))

        payload = parsed_stdout()
        assert payload['count'] == len(payload['removed'])
        assert payload['count'] == 3

    def test_default_ttl_24_hours(self, tmp_path, server_argv, parsed_stdout):
        """Exercising argparse isn't possible at function layer; instead,
        assert that server_argv's default matches the documented default."""
        fake_tmp = self._staging_root(tmp_path / 'fake_tmp')
        ns = server_argv(staging_root=str(fake_tmp))  # no ttl override
        assert ns.ttl_hours == 24.0
        server_cli._run_gc(ns)
        # Empty fake_tmp → no removals, just verify the handler exits cleanly.
        assert parsed_stdout()['count'] == 0


# ===========================================================================
# _run_validate_attributes
# ===========================================================================


class TestValidateAttributes:
    """Covers voxhub_core.server.cli._run_validate_attributes."""

    def test_store_without_dataset_attributes_reports_missing(
        self, stores_dir_factory, server_argv, parsed_stdout
    ):
        root = stores_dir_factory(('alpha',))
        server_cli._run_validate_attributes(server_argv(stores_dir=root))

        result = parsed_stdout()['results']['alpha']
        assert result['status'] == 'missing'
        assert result['issues'] == []

    def test_store_with_valid_attributes_reports_ok(
        self, stores_dir_factory, server_argv, parsed_stdout
    ):
        # Declared voxel size matches the canonical SPACING_MM.
        da = {
            'modality': 'MRI',
            'resolution': {'voxel_size': list(SPACING_MM), 'unit': 'mm'},
            'origin': 'synthetic',
            'tags': {},
        }
        root = stores_dir_factory(('alpha',), dataset_attributes={'alpha': da})
        server_cli._run_validate_attributes(server_argv(stores_dir=root))

        result = parsed_stdout()['results']['alpha']
        assert result['status'] == 'ok'
        assert result['issues'] == []

    def test_store_with_mismatched_voxel_size_reports_warning(
        self, stores_dir_factory, server_argv, parsed_stdout
    ):
        # Declare voxel size that disagrees with raw/full metadata.
        da = {
            'modality': 'MRI',
            'resolution': {'voxel_size': [0.1, 0.1, 0.1], 'unit': 'mm'},
            'origin': 'synthetic',
            'tags': {},
        }
        root = stores_dir_factory(('alpha',), dataset_attributes={'alpha': da})
        server_cli._run_validate_attributes(server_argv(stores_dir=root))

        result = parsed_stdout()['results']['alpha']
        assert result['status'] == 'warning'
        assert result['issues']
        issue = result['issues'][0]
        assert {'field', 'declared', 'actual', 'message'} <= issue.keys()

    def test_filters_stores_by_name(self, stores_dir_factory, server_argv, parsed_stdout):
        root = stores_dir_factory(('alpha', 'bravo'))
        server_cli._run_validate_attributes(
            server_argv(stores_dir=root, stores=['alpha'])
        )

        payload = parsed_stdout()
        assert list(payload['results']) == ['alpha']

    def test_protocol_version_present(
        self, stores_dir_factory, server_argv, parsed_stdout
    ):
        root = stores_dir_factory(('alpha',))
        server_cli._run_validate_attributes(server_argv(stores_dir=root))
        assert parsed_stdout()['protocol_version'] == PROTOCOL_VERSION


# ===========================================================================
# _run_healthcheck
# ===========================================================================


class TestHealthcheck:
    """Covers voxhub_core.server.cli._run_healthcheck and _check_* helpers."""

    def test_healthy_all_green(self, stores_dir_factory, server_argv, parsed_stdout):
        root = stores_dir_factory(('alpha',))
        server_cli._run_healthcheck(server_argv(stores_dir=root))

        payload = parsed_stdout()
        assert payload['status'] == 'healthy'
        check_names = [c['name'] for c in payload['checks']]
        assert {'python_version', 'packages', 'stores_dir', 'stores'} <= set(check_names)
        for check in payload['checks']:
            assert check['status'] == 'ok', check

    def test_degraded_when_stores_dir_unwritable(
        self, tmp_path, server_argv, parsed_stdout
    ):
        # Point at a path that doesn't exist — stores_dir check fails.
        missing = tmp_path / 'nonexistent'

        with pytest.raises(SystemExit) as excinfo:
            server_cli._run_healthcheck(server_argv(stores_dir=missing))
        assert excinfo.value.code == 1

        payload = parsed_stdout()
        assert payload['status'] == 'degraded'
        check = next(c for c in payload['checks'] if c['name'] == 'stores_dir')
        assert check['status'] == 'fail'

    def test_degraded_when_store_corrupted(
        self, stores_dir_factory, server_argv, parsed_stdout
    ):
        root = stores_dir_factory(('good', 'broken'), corrupt=('broken',))
        with pytest.raises(SystemExit):
            server_cli._run_healthcheck(server_argv(stores_dir=root))

        payload = parsed_stdout()
        assert payload['status'] == 'degraded'
        stores_check = next(c for c in payload['checks'] if c['name'] == 'stores')
        assert stores_check['status'] == 'fail'
        assert 'broken' in stores_check['detail']

    def test_provenance_check_ok_when_file_missing(
        self, stores_dir_factory, server_argv, parsed_stdout
    ):
        root = stores_dir_factory(('alpha',))
        server_cli._run_healthcheck(server_argv(stores_dir=root))

        prov_check = next(
            c for c in parsed_stdout()['checks'] if c['name'] == 'provenance'
        )
        assert prov_check['status'] == 'ok'
        assert 'no provenance file' in prov_check['detail']

    def test_provenance_check_fails_on_malformed_jsonl(
        self, stores_dir_factory, server_argv, parsed_stdout
    ):
        root = stores_dir_factory(('alpha',))
        meta_dir = root / '.meta'
        meta_dir.mkdir()
        (meta_dir / 'provenance.jsonl').write_text('{"valid":true}\nNOT JSON\n')

        with pytest.raises(SystemExit):
            server_cli._run_healthcheck(server_argv(stores_dir=root))

        prov_check = next(
            c for c in parsed_stdout()['checks'] if c['name'] == 'provenance'
        )
        assert prov_check['status'] == 'fail'

    def test_store_and_provenance_checks_skipped_when_stores_dir_fails(
        self, tmp_path, server_argv, parsed_stdout
    ):
        # If stores_dir fails, store/provenance checks are not even run —
        # they would crash on a nonexistent directory.
        missing = tmp_path / 'nonexistent'
        with pytest.raises(SystemExit):
            server_cli._run_healthcheck(server_argv(stores_dir=missing))

        check_names = {c['name'] for c in parsed_stdout()['checks']}
        assert 'stores' not in check_names
        assert 'provenance' not in check_names

    def test_python_version_check(self):
        check = server_cli._check_python_version()
        assert check['name'] == 'python_version'
        # The test suite itself requires 3.12+ per pyproject; must pass.
        assert check['status'] == 'ok'

    def test_rsync_check_when_rsync_present(self, monkeypatch):
        monkeypatch.setattr(shutil, 'which', lambda _name: '/usr/bin/rsync')
        check = server_cli._check_rsync()
        assert check == {
            'name': 'rsync',
            'status': 'ok',
            'detail': '/usr/bin/rsync',
        }

    def test_rsync_check_when_absent(self, monkeypatch):
        monkeypatch.setattr(shutil, 'which', lambda _name: None)
        check = server_cli._check_rsync()
        assert check['status'] == 'fail'

    def test_packages_check_all_importable(self):
        check = server_cli._check_packages()
        assert check['status'] == 'ok'


# ===========================================================================
# main() entry point
# ===========================================================================


class TestMainEntry:
    """Covers voxhub_core.server.cli.main — argparse wiring and error handling."""

    def test_no_command_prints_help_and_exits_zero(self, capsys, monkeypatch):
        monkeypatch.setattr('sys.argv', ['voxhub-server'])
        with pytest.raises(SystemExit) as excinfo:
            server_cli.main()
        assert excinfo.value.code == 0
        captured = capsys.readouterr()
        assert 'usage' in captured.out.lower()

    def test_unknown_command_argparse_error(self, capsys, monkeypatch):
        monkeypatch.setattr('sys.argv', ['voxhub-server', 'nonsense'])
        with pytest.raises(SystemExit) as excinfo:
            server_cli.main()
        assert excinfo.value.code == 2

    def test_unhandled_exception_returns_server_error_envelope(self, capsys, monkeypatch):
        # The autouse ``_default_server_config`` fixture provides a valid
        # VOXHUB_SERVER_CONFIG; force the list-stores handler to blow up.
        def boom(_args):  # type: ignore[no-untyped-def]
            raise RuntimeError('boom')

        monkeypatch.setattr(server_cli, '_run_list_stores', boom)
        monkeypatch.setattr('sys.argv', ['voxhub-server', 'list-stores'])

        with pytest.raises(SystemExit) as excinfo:
            server_cli.main()
        assert excinfo.value.code == 1

        out = capsys.readouterr().out
        envelope = json.loads(out.strip().splitlines()[-1])
        assert envelope['error'] is True
        assert envelope['code'] == 'internal_error'
        assert envelope['message'] == 'boom'

    def test_settings_loaded_on_entry(self, capsys, monkeypatch):
        called: list[bool] = []

        original_load = server_cli.load_settings

        def spy() -> object:
            called.append(True)
            return original_load()

        monkeypatch.setattr(server_cli, 'load_settings', spy)
        # Stub the handler so we only exercise the pre-handler plumbing.
        monkeypatch.setattr(server_cli, '_run_list_stores', lambda _args: None)
        monkeypatch.setattr('sys.argv', ['voxhub-server', 'list-stores'])

        server_cli.main()
        assert called, 'expected load_settings to be invoked'


# ===========================================================================
# main() — settings-path stores_dir resolution
# ===========================================================================


class TestMainStoresDirResolution:
    """Covers main()'s resolution of ``stores_dir`` from ``[storage]`` settings.

    PR 1 semantics: ``stores_dir`` comes exclusively from
    ``settings.storage.stores_dir``.  There is no positional override and
    no silent default — any missing / invalid configuration is a hard
    failure with a structured ``storage_misconfigured`` envelope.
    """

    @pytest.mark.parametrize(
        'subcommand',
        [
            'list-stores',
            'prepare-pull',
            'integrate-annotations',
            'validate-attributes',
            'healthcheck',
        ],
    )
    def test_handler_receives_settings_stores_dir(
        self,
        subcommand,
        stores_dir_factory,
        server_config_env,
        monkeypatch,
        tmp_path,
    ):
        """Every stores-dir command receives ``settings.storage.stores_dir``
        via ``args.stores_dir``."""
        root = stores_dir_factory(('alpha',))
        server_config_env(root)

        captured: dict[str, object] = {}

        def spy(args):  # type: ignore[no-untyped-def]
            captured['stores_dir'] = args.stores_dir

        for name in (
            '_run_list_stores',
            '_run_prepare_pull',
            '_run_integrate_annotations',
            '_run_validate_attributes',
            '_run_healthcheck',
        ):
            monkeypatch.setattr(server_cli, name, spy)

        argv = ['voxhub-server', subcommand]
        if subcommand == 'integrate-annotations':
            argv += [
                str(tmp_path / 'staging'),
                '--annotator-id',
                'alice',
                '--machine-id',
                'm',
                '--nano-id',
                'abcd1234',
            ]
        elif subcommand == 'prepare-pull':
            argv += ['--store', 'alpha']

        monkeypatch.setattr('sys.argv', argv)
        server_cli.main()

        assert captured['stores_dir'] == str(root)

    def test_positional_stores_dir_is_rejected_by_argparse(
        self,
        stores_dir_factory,
        server_config_env,
        monkeypatch,
        capsys,
    ):
        """A caller passing a positional (legacy behaviour) gets an
        argparse error — the positional is gone from the subparsers."""
        root = stores_dir_factory(('alpha',))
        server_config_env(root)
        monkeypatch.setattr(
            'sys.argv',
            ['voxhub-server', 'list-stores', str(root)],
        )
        with pytest.raises(SystemExit) as excinfo:
            server_cli.main()
        assert excinfo.value.code == 2
        assert 'unrecognized arguments' in capsys.readouterr().err.lower()

    def test_missing_config_file_emits_storage_misconfigured(
        self,
        monkeypatch,
        capsys,
        tmp_path,
    ):
        """No config file → structured ``storage_misconfigured`` envelope."""
        monkeypatch.setenv('VOXHUB_SERVER_CONFIG', str(tmp_path / 'no-such.toml'))
        monkeypatch.setattr('sys.argv', ['voxhub-server', 'list-stores'])

        with pytest.raises(SystemExit) as excinfo:
            server_cli.main()
        assert excinfo.value.code == 1

        envelope = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert envelope['error'] is True
        assert envelope['code'] == 'storage_misconfigured'

    def test_invalid_storage_toml_emits_storage_misconfigured(
        self,
        monkeypatch,
        capsys,
        tmp_path,
    ):
        """A ``[storage].stores_dir`` that points at a non-directory fails
        at settings-load time with a structured envelope."""
        bad_dir = tmp_path / 'does-not-exist'
        config_path = tmp_path / 'server.toml'
        config_path.write_text(f"[storage]\nstores_dir = '{bad_dir}'\n")
        monkeypatch.setenv('VOXHUB_SERVER_CONFIG', str(config_path))
        monkeypatch.setattr('sys.argv', ['voxhub-server', 'list-stores'])

        with pytest.raises(SystemExit) as excinfo:
            server_cli.main()
        assert excinfo.value.code == 1

        envelope = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert envelope['error'] is True
        assert envelope['code'] == 'storage_misconfigured'
        assert str(bad_dir) in envelope['message']

    def test_missing_stores_dir_key_emits_storage_misconfigured(
        self,
        monkeypatch,
        capsys,
        tmp_path,
    ):
        """A ``[storage]`` section without a ``stores_dir`` key is rejected."""
        config_path = tmp_path / 'server.toml'
        config_path.write_text('[storage]\n')
        monkeypatch.setenv('VOXHUB_SERVER_CONFIG', str(config_path))
        monkeypatch.setattr('sys.argv', ['voxhub-server', 'list-stores'])

        with pytest.raises(SystemExit) as excinfo:
            server_cli.main()
        assert excinfo.value.code == 1

        envelope = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert envelope['error'] is True
        assert envelope['code'] == 'storage_misconfigured'

    def test_missing_storage_section_emits_storage_misconfigured(
        self,
        monkeypatch,
        capsys,
        tmp_path,
    ):
        """A config file without a ``[storage]`` section at all is rejected
        — settings are strict, not best-effort."""
        config_path = tmp_path / 'server.toml'
        config_path.write_text('[logging]\nstderr_level = "ERROR"\n')
        monkeypatch.setenv('VOXHUB_SERVER_CONFIG', str(config_path))
        monkeypatch.setattr('sys.argv', ['voxhub-server', 'list-stores'])

        with pytest.raises(SystemExit) as excinfo:
            server_cli.main()
        assert excinfo.value.code == 1

        envelope = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert envelope['error'] is True
        assert envelope['code'] == 'storage_misconfigured'


# ===========================================================================
# Staging-dir settings + validator
# ===========================================================================


class TestStagingDirSettings:
    """Covers ``[storage].staging_dir`` parsing in ``load_settings`` and
    the ``args.staging_root`` injection in ``main()``.
    """

    def test_staging_dir_defaults_to_gettempdir(
        self, stores_dir_factory, server_config_env
    ):
        """An omitted ``[storage].staging_dir`` falls back to
        ``tempfile.gettempdir()`` so existing deployments don't need to
        rewrite their config."""
        import tempfile as _tempfile

        from voxhub_core.server.settings import load_settings

        server_config_env(stores_dir_factory(('alpha',)))
        settings = load_settings()
        assert settings.storage.staging_dir == Path(_tempfile.gettempdir()).resolve()

    def test_staging_dir_honoured_when_declared(
        self, stores_dir_factory, server_config_env, tmp_path
    ):
        from voxhub_core.server.settings import load_settings

        staging = tmp_path / 'operator_staging'
        staging.mkdir()
        server_config_env(
            stores_dir_factory(('alpha',)),
            extra=f"staging_dir = '{staging}'\n",
        )
        settings = load_settings()
        assert settings.storage.staging_dir == staging.resolve()

    def test_staging_dir_equal_to_stores_dir_is_rejected(
        self, stores_dir_factory, server_config_env
    ):
        from voxhub_core.server.settings import SettingsError, load_settings

        root = stores_dir_factory(('alpha',))
        server_config_env(root, extra=f"staging_dir = '{root}'\n")
        with pytest.raises(SettingsError) as excinfo:
            load_settings()
        assert 'must not equal' in str(excinfo.value)

    def test_missing_staging_dir_path_is_rejected(
        self, stores_dir_factory, server_config_env, tmp_path
    ):
        from voxhub_core.server.settings import SettingsError, load_settings

        nonexistent = tmp_path / 'does_not_exist'
        server_config_env(
            stores_dir_factory(('alpha',)),
            extra=f"staging_dir = '{nonexistent}'\n",
        )
        with pytest.raises(SettingsError) as excinfo:
            load_settings()
        assert 'does not exist' in str(excinfo.value)

    def test_main_injects_staging_root_from_settings(
        self, stores_dir_factory, server_config_env, tmp_path, monkeypatch
    ):
        """main() propagates ``settings.storage.staging_dir`` to handlers
        via ``args.staging_root``."""
        root = stores_dir_factory(('alpha',))
        staging = tmp_path / 'operator_staging'
        staging.mkdir()
        server_config_env(root, extra=f"staging_dir = '{staging}'\n")

        captured: dict[str, str] = {}

        def spy(args):  # type: ignore[no-untyped-def]
            captured['staging_root'] = args.staging_root

        monkeypatch.setattr(server_cli, '_run_prepare_pull', spy)
        monkeypatch.setattr(
            'sys.argv', ['voxhub-server', 'prepare-pull', '--store', 'alpha']
        )
        server_cli.main()

        assert Path(captured['staging_root']) == staging.resolve()


class TestValidateEchoedStagingDir:
    """Covers ``_validate_echoed_staging_dir`` — the confinement helper
    that ``cleanup`` and ``integrate-annotations`` apply to every
    client-echoed staging path."""

    def test_accepts_path_inside_root_with_prefix(self, tmp_path):
        target = tmp_path / 'vxhb-staging-alice'
        target.mkdir()
        result = server_cli._validate_echoed_staging_dir(str(target), tmp_path)
        assert result == target.resolve()

    def test_rejects_path_outside_root(self, tmp_path):
        root = tmp_path / 'root'
        root.mkdir()
        sibling = tmp_path / 'vxhb-staging-sibling'
        sibling.mkdir()
        with pytest.raises(ValueError, match='outside'):
            server_cli._validate_echoed_staging_dir(str(sibling), root)

    def test_rejects_path_without_prefix(self, tmp_path):
        target = tmp_path / 'user_data'
        target.mkdir()
        with pytest.raises(ValueError, match='prefix'):
            server_cli._validate_echoed_staging_dir(str(target), tmp_path)

    def test_rejects_symlink_escape(self, tmp_path):
        """``resolve()`` follows symlinks before the confinement check,
        so a symlink pointing outside the staging root is rejected."""
        root = tmp_path / 'root'
        root.mkdir()
        outside = tmp_path / 'outside-target'
        outside.mkdir()
        lnk = root / 'vxhb-staging-sneaky'
        lnk.symlink_to(outside)
        with pytest.raises(ValueError, match='outside'):
            server_cli._validate_echoed_staging_dir(str(lnk), root)


# ===========================================================================
# _run_catalog_{refresh,show,stats}
# ===========================================================================


class TestCatalogCommand:
    """Covers the ``voxhub-server catalog`` admin subcommand (PR 4)."""

    # -- refresh -----------------------------------------------------------

    def test_refresh_no_existing_cache_builds_one(
        self, stores_dir_factory, server_argv, parsed_stdout
    ):
        root = stores_dir_factory(('alpha', 'bravo'))
        cache_path = root / '.meta' / 'catalog.json'
        assert not cache_path.exists()

        server_cli._run_catalog_refresh(server_argv(stores_dir=root, store=None))

        payload = parsed_stdout()
        assert payload['protocol_version'] == PROTOCOL_VERSION
        assert payload['catalog_version'] == 1
        assert payload['store_count'] == 2
        assert 'built_at' in payload
        assert cache_path.is_file()

    def test_refresh_on_existing_cache_bumps_version(
        self, stores_dir_factory, server_argv, parsed_stdout
    ):
        root = stores_dir_factory(('alpha',))
        server_cli._run_list_stores(server_argv(stores_dir=root))
        first = parsed_stdout()
        assert first['catalog_version'] == 1

        server_cli._run_catalog_refresh(server_argv(stores_dir=root, store=None))
        refreshed = parsed_stdout()
        assert refreshed['catalog_version'] == 2

    def test_refresh_single_store_only_touches_target(
        self, stores_dir_factory, server_argv, parsed_stdout, monkeypatch
    ):
        from voxhub_core.server import catalog_cache as cc

        root = stores_dir_factory(('alpha', 'bravo', 'charlie'))
        # Seed the cache so the single-store path splices rather than
        # falling back to a full rebuild.
        server_cli._run_list_stores(server_argv(stores_dir=root))
        parsed_stdout()

        # A full-walk rebuild goes through ``_build_snapshot``; a
        # single-store refresh must not invoke it.
        def _no_full_rebuild(*_a: object, **_kw: object) -> object:
            raise AssertionError('single-store refresh must not full-rebuild')

        monkeypatch.setattr(cc, '_build_snapshot', _no_full_rebuild)

        server_cli._run_catalog_refresh(server_argv(stores_dir=root, store='bravo'))
        payload = parsed_stdout()

        assert payload['catalog_version'] == 2
        assert payload['store_count'] == 3

        # The on-disk catalog must still contain all three stores.
        data = json.loads((root / '.meta' / 'catalog.json').read_text())
        assert set(data['stores']) == {'alpha', 'bravo', 'charlie'}

    def test_refresh_nonexistent_store_returns_store_not_found(
        self, stores_dir_factory, server_argv, parsed_stdout
    ):
        root = stores_dir_factory(('alpha',))
        server_cli._run_list_stores(server_argv(stores_dir=root))
        parsed_stdout()

        with pytest.raises(SystemExit) as excinfo:
            server_cli._run_catalog_refresh(
                server_argv(stores_dir=root, store='does-not-exist')
            )
        assert excinfo.value.code == 1

        envelope = parsed_stdout()
        assert envelope['error'] is True
        assert envelope['code'] == 'store_not_found'
        assert envelope['protocol_version'] == PROTOCOL_VERSION
        assert 'does-not-exist' in envelope['message']

    def test_refresh_store_removed_from_disk_drops_entry(
        self, stores_dir_factory, server_argv, parsed_stdout
    ):
        """A store present in the cache but absent on disk is a valid
        refresh target — the operator wants to drop the stale entry. Only
        names that are nowhere (disk + cache) error out."""
        root = stores_dir_factory(('alpha', 'bravo'))
        server_cli._run_list_stores(server_argv(stores_dir=root))
        parsed_stdout()

        shutil.rmtree(root / 'bravo.zarr')

        server_cli._run_catalog_refresh(server_argv(stores_dir=root, store='bravo'))
        payload = parsed_stdout()

        assert payload['store_count'] == 1
        data = json.loads((root / '.meta' / 'catalog.json').read_text())
        assert set(data['stores']) == {'alpha'}

    # -- show --------------------------------------------------------------

    def test_show_returns_full_snapshot(
        self, stores_dir_factory, server_argv, parsed_stdout
    ):
        root = stores_dir_factory(('alpha',), with_annotations=True)
        server_cli._run_catalog_show(server_argv(stores_dir=root))

        payload = parsed_stdout()
        assert payload['protocol_version'] == PROTOCOL_VERSION
        assert payload['catalog_version'] == 1
        assert 'built_at' in payload
        assert 'stores_dir_fingerprint' in payload
        assert len(payload['stores']) == 1
        entry = payload['stores'][0]
        assert entry['name'] == 'alpha'
        assert len(entry['annotations']) == 1

    def test_show_store_entries_match_list_stores(
        self, stores_dir_factory, server_argv, parsed_stdout
    ):
        root = stores_dir_factory(('alpha', 'bravo'), with_annotations=True)

        server_cli._run_list_stores(server_argv(stores_dir=root))
        list_payload = parsed_stdout()

        server_cli._run_catalog_show(server_argv(stores_dir=root))
        show_payload = parsed_stdout()

        list_by_name = {s['name']: s for s in list_payload['stores']}
        show_by_name = {s['name']: s for s in show_payload['stores']}
        assert list_by_name == show_by_name

    # -- stats -------------------------------------------------------------

    def test_stats_fresh_build_reports_fingerprint_match(
        self, stores_dir_factory, server_argv, parsed_stdout
    ):
        root = stores_dir_factory(('alpha', 'bravo'))
        server_cli._run_list_stores(server_argv(stores_dir=root))
        parsed_stdout()

        server_cli._run_catalog_stats(server_argv(stores_dir=root))
        stats = parsed_stdout()

        assert stats['status'] == 'ok'
        assert stats['fingerprint_match'] is True
        assert stats['store_count'] == 2
        assert stats['catalog_version'] == 1
        assert stats['cache_file_size_bytes'] > 0
        assert stats['age_s'] >= 0.0

    def test_stats_after_out_of_band_add_reports_fingerprint_mismatch(
        self, stores_dir_factory, server_argv, parsed_stdout
    ):
        from _core_helpers import create_zarr_store

        root = stores_dir_factory(('alpha',))
        server_cli._run_list_stores(server_argv(stores_dir=root))
        parsed_stdout()

        create_zarr_store(root / 'bravo.zarr')

        server_cli._run_catalog_stats(server_argv(stores_dir=root))
        stats = parsed_stdout()

        assert stats['status'] == 'ok'
        assert stats['fingerprint_match'] is False

    def test_stats_missing_cache_reports_missing_status(
        self, stores_dir_factory, server_argv, parsed_stdout
    ):
        root = stores_dir_factory(('alpha',))
        # Do NOT prime the cache.
        assert not (root / '.meta' / 'catalog.json').exists()

        server_cli._run_catalog_stats(server_argv(stores_dir=root))
        stats = parsed_stdout()

        assert stats['status'] == 'missing'
        assert stats['protocol_version'] == PROTOCOL_VERSION

    def test_stats_corrupt_cache_reports_corrupt_status(
        self, stores_dir_factory, server_argv, parsed_stdout
    ):
        root = stores_dir_factory(('alpha',))
        server_cli._run_list_stores(server_argv(stores_dir=root))
        parsed_stdout()

        cache_path = root / '.meta' / 'catalog.json'
        cache_path.write_text('{not valid json at all')

        server_cli._run_catalog_stats(server_argv(stores_dir=root))
        stats = parsed_stdout()

        assert stats['status'] == 'corrupt'
        assert stats['cache_file_size_bytes'] > 0

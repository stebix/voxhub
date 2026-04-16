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
    write_seg_nrrd,
)

from voxhub_core.server import cli as server_cli
from voxhub_schema import PROTOCOL_VERSION

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

    def test_uses_explicit_staging_dir_when_provided(
        self, stores_dir_factory, tmp_path, server_argv, parsed_stdout
    ):
        root = stores_dir_factory(('alpha',))
        explicit = tmp_path / 'explicit_staging'
        server_cli._run_prepare_pull(
            server_argv(stores_dir=root, store='alpha', staging_dir=str(explicit))
        )

        payload = parsed_stdout()
        assert Path(payload['staging_dir']) == explicit
        assert explicit.is_dir()
        assert (explicit / 'raw.nrrd').is_file()
        assert (explicit / '.voxhub_pull.json').is_file()

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

    def test_compression_flag_propagates_to_stage(
        self, stores_dir_factory, server_argv, parsed_stdout, monkeypatch
    ):
        root = stores_dir_factory(('alpha',))
        captured: dict[str, object] = {}

        original_stage = server_cli.stage

        def spy_stage(*args, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            return original_stage(*args, **kwargs)

        monkeypatch.setattr(server_cli, 'stage', spy_stage)

        server_cli._run_prepare_pull(
            server_argv(stores_dir=root, store='alpha', compress=True)
        )
        payload = parsed_stdout()
        try:
            assert captured['compress'] is True
        finally:
            shutil.rmtree(payload['staging_dir'], ignore_errors=True)

    def test_stage_failure_writes_error_envelope_and_exits(
        self, stores_dir_factory, server_argv, parsed_stdout, monkeypatch
    ):
        root = stores_dir_factory(('alpha',))

        def boom(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError('staging blew up')

        monkeypatch.setattr(server_cli, 'stage', boom)

        with pytest.raises(SystemExit) as excinfo:
            server_cli._run_prepare_pull(server_argv(stores_dir=root, store='alpha'))
        assert excinfo.value.code == 1

        envelope = parsed_stdout()
        assert envelope['error'] is True
        assert envelope['code'] == 'prepare_pull_failed'
        assert 'staging blew up' in envelope['message']
        assert envelope['protocol_version'] == PROTOCOL_VERSION

    def test_store_not_found_fails_before_staging(
        self, stores_dir_factory, tmp_path, server_argv, parsed_stdout, monkeypatch
    ):
        """Missing --store errors early with no staging dir created."""
        root = stores_dir_factory(('alpha',))

        # Sentinel to assert stage is never called.
        calls: list[int] = []

        def sentinel(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            calls.append(1)
            raise AssertionError('stage must not be called when store is absent')

        monkeypatch.setattr(server_cli, 'stage', sentinel)

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

    def test_reused_staging_dir_is_sanitised(
        self, stores_dir_factory, tmp_path, server_argv, parsed_stdout
    ):
        """--staging-dir pointed at a dir with prior prepare-pull debris:
        orphans from the previous run are removed so the new manifest
        and on-disk layout stay in agreement."""
        from voxhub_schema import PullManifest

        root = stores_dir_factory(('alpha',))
        explicit = tmp_path / 'explicit_staging'
        explicit.mkdir()

        # Plant debris that a prior prepare-pull invocation might leave:
        #   * orphan reference files (the within-run #4 fix deletes
        #     *partial* writes; across runs we clear the whole tree),
        #   * a stale manifest (shouldn't be trusted if the new run
        #     fails to write its own),
        #   * a nested <store>/ dir (from a crash mid-flatten),
        #   * stale raw.nrrd.
        ref_dir = explicit / 'reference'
        ref_dir.mkdir()
        leftover = ref_dir / 'orphan-from-prior-run.seg.nrrd'
        leftover.write_bytes(b'stale contents')
        (explicit / '.voxhub_pull.json').write_text('{"stale": true}')
        (explicit / 'raw.nrrd').write_bytes(b'stale raw bytes')
        nested = explicit / 'alpha'
        nested.mkdir()
        (nested / 'raw.nrrd').write_bytes(b'stale nested raw')

        server_cli._run_prepare_pull(
            server_argv(stores_dir=root, store='alpha', staging_dir=str(explicit))
        )

        payload = parsed_stdout()
        assert Path(payload['staging_dir']) == explicit

        # Orphans are gone.
        assert not leftover.exists()
        assert not nested.exists()

        # Fresh manifest is in place and agrees with on-disk state.
        assert (explicit / '.voxhub_pull.json').is_file()
        manifest = PullManifest.read(explicit)
        assert manifest.store_name == 'alpha'
        assert manifest.annotations == []

        # Fresh raw is genuinely fresh (checksum matches the new manifest,
        # not the planted stub).
        raw_bytes = (explicit / 'raw.nrrd').read_bytes()
        assert raw_bytes != b'stale raw bytes'
        assert manifest.raw_checksum == payload['raw_checksum']

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
        correct_checksum = server_cli._compute_sha256(seg_file)

        server_cli._run_integrate_annotations(
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

    def test_checksum_mismatch_writes_error_envelope_and_exits(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
    ):
        stores_dir = stores_dir_factory(('alpha',))
        staging = staging_dir_with_annotations(store_names=['alpha'])

        bogus = f'sha256:{"0" * 64}'
        with pytest.raises(SystemExit) as excinfo:
            server_cli._run_integrate_annotations(
                _integrate_argv(
                    server_argv,
                    stores_dir=stores_dir,
                    staging_dir=staging,
                    checksums=[f'segmentation.seg.nrrd:{bogus}'],
                )
            )
        assert excinfo.value.code == 1

        envelope = parsed_stdout()
        assert envelope['code'] == 'checksum_mismatch'
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

        server_cli._run_integrate_annotations(
            _integrate_argv(
                server_argv,
                stores_dir=stores_dir,
                staging_dir=staging,
                expected_ontology=['does-not-exist'],
            )
        )

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

        server_cli._run_integrate_annotations(
            _integrate_argv(server_argv, stores_dir=stores_dir, staging_dir=staging)
        )

        store_result = parsed_stdout()['stores']['alpha']
        assert store_result['status'] == 'failed'
        assert _written_annotations(stores_dir / 'alpha.zarr') == []
        errors = [i for i in store_result['issues'] if i['severity'] == 'error']
        assert errors

    def test_force_allows_integration_despite_errors(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        server_argv,
        parsed_stdout,
    ):
        stores_dir = stores_dir_factory(('alpha',))
        staging = staging_dir_with_annotations(
            store_names=['alpha'],
            seg_label_map=np.zeros((5, 5, 5), dtype=np.int16),
            seg_segments=[{'id': 's0', 'name': 'cochlea', 'label_value': 1}],
        )

        server_cli._run_integrate_annotations(
            _integrate_argv(
                server_argv, stores_dir=stores_dir, staging_dir=staging, force=True
            )
        )

        store_result = parsed_stdout()['stores']['alpha']
        assert store_result['status'] == 'integrated'
        assert len(_written_annotations(stores_dir / 'alpha.zarr')) == 1

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

        server_cli._run_integrate_annotations(
            _integrate_argv(server_argv, stores_dir=stores_dir, staging_dir=staging)
        )

        payload = parsed_stdout()
        assert payload['stores']['alpha']['status'] == 'failed'
        errors = [
            i for i in payload['stores']['alpha']['issues'] if i['severity'] == 'error'
        ]
        assert errors
        # bravo unaffected.
        assert payload['stores']['bravo']['status'] == 'integrated'


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

        server_cli._run_integrate_annotations(
            _integrate_argv(server_argv, stores_dir=stores_dir, staging_dir=staging)
        )

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

        server_cli._run_integrate_annotations(
            _integrate_argv(
                server_argv,
                stores_dir=stores_dir,
                staging_dir=staging,
                expected_ontology=['inner-ear-landmarks'],
            )
        )

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

    def test_noop_when_staging_dir_missing(
        self, tmp_path, server_argv, parsed_stdout, capsys
    ):
        missing = tmp_path / 'does-not-exist'

        server_cli._run_cleanup(server_argv(staging_dir=str(missing)))

        # Reading stderr first would consume the JSON stdout too, so we
        # parse stdout through the fixture which calls readouterr().
        payload = parsed_stdout()
        assert payload['status'] == 'ok'

    @pytest.mark.xfail(
        reason='current implementation has no path safety check — '
        'documented concern from docs/testing/server-cli.md §4.4',
        strict=True,
    )
    def test_refuses_to_remove_non_staging_path(
        self, tmp_path, server_argv, parsed_stdout
    ):
        # A path that looks nothing like a staging directory (no dt-* prefix,
        # not under system tmpdir) should be refused.  If this xfail ever
        # flips to passing, _run_cleanup now rejects suspicious paths.
        suspicious = tmp_path / 'user-data'
        suspicious.mkdir()

        server_cli._run_cleanup(server_argv(staging_dir=str(suspicious)))

        # We expect either a non-ok status OR the directory to remain.
        payload = parsed_stdout()
        assert payload['status'] != 'ok' or suspicious.exists()


# ===========================================================================
# _run_gc
# ===========================================================================


class TestGc:
    """Covers voxhub_core.server.cli._run_gc.

    Isolation: ``_run_gc`` scans ``tempfile.gettempdir()``.  Every test
    monkey-patches that to point at a per-test ``tmp_path`` so the real /tmp
    is never touched.
    """

    @staticmethod
    def _isolate(monkeypatch, fake_tmp: Path) -> None:
        import tempfile

        fake_tmp.mkdir(exist_ok=True)
        monkeypatch.setattr(tempfile, 'gettempdir', lambda: str(fake_tmp))

    def test_removes_dirs_older_than_ttl(
        self, tmp_path, server_argv, parsed_stdout, monkeypatch
    ):
        self._isolate(monkeypatch, tmp_path / 'fake_tmp')
        old = tmp_path / 'fake_tmp' / 'vxhb-staging-old'
        old.mkdir()
        # 48 hours in the past.
        import os as _os

        old_ts = old.stat().st_mtime - 48 * 3600
        _os.utime(old, (old_ts, old_ts))

        server_cli._run_gc(server_argv(ttl_hours=24.0))

        payload = parsed_stdout()
        assert str(old) in payload['removed']
        assert payload['count'] == 1
        assert not old.exists()

    def test_emits_staging_dir_reaped_event_on_gc(
        self, tmp_path, server_argv, parsed_stdout, monkeypatch, caplog
    ):
        """GC reap emits ``staging_dir_reaped`` with ``reason='gc_unacked'``."""
        from voxhub_schema import PROTOCOL_VERSION, PullManifest

        self._isolate(monkeypatch, tmp_path / 'fake_tmp')
        old = tmp_path / 'fake_tmp' / 'vxhb-staging-old'
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
            server_cli._run_gc(server_argv(ttl_hours=24.0))
        parsed_stdout()

        reaped_events = [
            r for r in caplog.records if "'event': 'staging_dir_reaped'" in r.message
        ]
        assert len(reaped_events) == 1
        msg = reaped_events[0].message
        assert "'reason': 'gc_unacked'" in msg
        assert "'store_name': 'patient-013'" in msg
        assert "'had_manifest': True" in msg

    def test_keeps_dirs_newer_than_ttl(
        self, tmp_path, server_argv, parsed_stdout, monkeypatch
    ):
        self._isolate(monkeypatch, tmp_path / 'fake_tmp')
        recent = tmp_path / 'fake_tmp' / 'vxhb-staging-recent'
        recent.mkdir()

        server_cli._run_gc(server_argv(ttl_hours=24.0))

        payload = parsed_stdout()
        assert payload['count'] == 0
        assert recent.exists()

    def test_ignores_non_staging_prefix(
        self, tmp_path, server_argv, parsed_stdout, monkeypatch
    ):
        self._isolate(monkeypatch, tmp_path / 'fake_tmp')
        other = tmp_path / 'fake_tmp' / 'foo-bar'
        other.mkdir()
        import os as _os

        old_ts = other.stat().st_mtime - 48 * 3600
        _os.utime(other, (old_ts, old_ts))

        server_cli._run_gc(server_argv(ttl_hours=24.0))

        payload = parsed_stdout()
        assert payload['count'] == 0
        assert other.exists()

    def test_ignores_files_only_dirs(
        self, tmp_path, server_argv, parsed_stdout, monkeypatch
    ):
        self._isolate(monkeypatch, tmp_path / 'fake_tmp')
        staging_file = tmp_path / 'fake_tmp' / 'vxhb-staging-file'
        staging_file.write_text('data')
        import os as _os

        old_ts = staging_file.stat().st_mtime - 48 * 3600
        _os.utime(staging_file, (old_ts, old_ts))

        server_cli._run_gc(server_argv(ttl_hours=24.0))

        payload = parsed_stdout()
        assert payload['count'] == 0
        assert staging_file.exists()

    def test_count_matches_removed_length(
        self, tmp_path, server_argv, parsed_stdout, monkeypatch
    ):
        self._isolate(monkeypatch, tmp_path / 'fake_tmp')
        import os as _os

        for name in ('vxhb-staging-a', 'vxhb-staging-b', 'vxhb-staging-c'):
            p = tmp_path / 'fake_tmp' / name
            p.mkdir()
            _os.utime(p, (p.stat().st_mtime - 48 * 3600,) * 2)

        server_cli._run_gc(server_argv(ttl_hours=24.0))

        payload = parsed_stdout()
        assert payload['count'] == len(payload['removed'])
        assert payload['count'] == 3

    def test_default_ttl_24_hours(
        self, tmp_path, server_argv, parsed_stdout, monkeypatch
    ):
        """Exercising argparse isn't possible at function layer; instead,
        assert that server_argv's default matches the documented default."""
        self._isolate(monkeypatch, tmp_path / 'fake_tmp')
        ns = server_argv()  # no override
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

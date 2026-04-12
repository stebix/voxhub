"""Tests for voxhub_core.server.provenance.

Covers record_provenance (dual-write to zarr attrs + .meta/provenance.jsonl)
and validate_provenance_jsonl.  Concurrent-append scenarios live in
test_concurrency.py.

Plan: docs/testing/concurrency-and-provenance.md §3
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import ClassVar

import pytest
import zarr

from voxhub_core.server.provenance import (
    record_provenance,
    validate_provenance_jsonl,
)
from voxhub_schema import IssueRecord

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


_DEFAULT_ANN_PATH = 'annotations/alice-xyz45678/inner-ear-structures-20260101-ab12/data'


def _call_record_provenance(
    stores_dir: Path,
    store_name: str = 'alpha',
    *,
    annotation_path: str = _DEFAULT_ANN_PATH,
    annotator_id: str = 'bob',
    machine_id: str = 'machine-7',
    nano_id: str = 'deadbeef',
    pull_session_id: str = 'dt-pull-session',
    ontology: str = 'inner-ear-structures',
    ontology_version: int = 1,
    source_nrrd_checksum: str = 'sha256:' + '0' * 64,
    source_file: str = 'segmentation.seg.nrrd',
    issues: list[IssueRecord] | None = None,
) -> None:
    record_provenance(
        stores_dir,
        store_name,
        annotation_path,
        annotator_id=annotator_id,
        machine_id=machine_id,
        nano_id=nano_id,
        pull_session_id=pull_session_id,
        ontology=ontology,
        ontology_version=ontology_version,
        source_nrrd_checksum=source_nrrd_checksum,
        source_file=source_file,
        issues=issues,
    )


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding='utf-8').splitlines()
        if line.strip()
    ]


# ===========================================================================
# record_provenance — happy paths
# ===========================================================================


class TestRecordProvenance:
    """Covers voxhub_core.server.provenance.record_provenance — happy paths."""

    _EXPECTED_ATTR_KEYS: ClassVar[frozenset[str]] = frozenset(
        {
            'integrated_at',
            'annotator_id',
            'machine_id',
            'nano_id',
            'pull_session_id',
            'source_nrrd_checksum',
            'source_file',
            'ontology',
            'ontology_version',
        }
    )

    def test_writes_zarr_array_attributes(self, stores_dir_factory):
        root = stores_dir_factory(('alpha',), with_annotations=True)
        _call_record_provenance(root)

        arr = zarr.open_group(root / 'alpha.zarr', mode='r')[
            'annotations/alice-xyz45678/inner-ear-structures-20260101-ab12/data'
        ]
        attrs = dict(arr.attrs)
        assert attrs.keys() >= self._EXPECTED_ATTR_KEYS
        assert attrs['annotator_id'] == 'bob'
        assert attrs['machine_id'] == 'machine-7'
        assert attrs['nano_id'] == 'deadbeef'
        assert attrs['pull_session_id'] == 'dt-pull-session'
        assert attrs['ontology'] == 'inner-ear-structures'
        assert attrs['ontology_version'] == 1
        assert isinstance(attrs['integrated_at'], str)
        assert isinstance(attrs['source_nrrd_checksum'], str)

    def test_appends_single_line_to_jsonl_index(self, stores_dir_factory):
        root = stores_dir_factory(('alpha',), with_annotations=True)
        _call_record_provenance(root)

        jsonl = root / '.meta' / 'provenance.jsonl'
        assert jsonl.is_file()
        records = _read_jsonl(jsonl)
        assert len(records) == 1
        record = records[0]
        expected_keys = {
            'event',
            'session_id',
            'pull_session_id',
            'store',
            'annotation_path',
            'annotator_id',
            'machine_id',
            'timestamp',
            'ontology',
            'ontology_version',
            'issues',
        }
        assert expected_keys <= record.keys()
        assert record['event'] == 'push'
        assert record['store'] == 'alpha'

    def test_creates_meta_directory_if_missing(self, stores_dir_factory):
        root = stores_dir_factory(('alpha',), with_annotations=True)
        assert not (root / '.meta').exists()

        _call_record_provenance(root)

        assert (root / '.meta').is_dir()
        assert (root / '.meta' / 'provenance.jsonl').is_file()

    def test_subsequent_calls_append_not_overwrite(self, stores_dir_factory):
        root = stores_dir_factory(('alpha',), with_annotations=True)
        _call_record_provenance(root)
        _call_record_provenance(root, annotator_id='carol', nano_id='cafebabe')

        records = _read_jsonl(root / '.meta' / 'provenance.jsonl')
        assert len(records) == 2
        assert records[0]['annotation_path'] == records[1]['annotation_path']
        assert records[0]['annotator_id'] == 'bob'
        assert records[1]['annotator_id'] == 'carol'

    def test_timestamp_is_iso_utc(self, stores_dir_factory):
        root = stores_dir_factory(('alpha',), with_annotations=True)
        _call_record_provenance(root)

        arr = zarr.open_group(root / 'alpha.zarr', mode='r')[_DEFAULT_ANN_PATH]
        parsed = datetime.fromisoformat(str(arr.attrs['integrated_at']))
        offset = parsed.utcoffset()
        assert offset is not None
        assert offset.total_seconds() == 0

    def test_issues_list_empty_when_none_passed(self, stores_dir_factory):
        root = stores_dir_factory(('alpha',), with_annotations=True)
        _call_record_provenance(root, issues=None)

        record = _read_jsonl(root / '.meta' / 'provenance.jsonl')[0]
        assert record['issues'] == []

    def test_issues_list_populated_when_warnings_passed(self, stores_dir_factory):
        root = stores_dir_factory(('alpha',), with_annotations=True)
        issues = [
            IssueRecord(severity='warning', message='coarse voxel spacing'),
            IssueRecord(severity='warning', message='extra segment label'),
        ]
        _call_record_provenance(root, issues=issues)

        record = _read_jsonl(root / '.meta' / 'provenance.jsonl')[0]
        assert record['issues'] == [
            {'severity': 'warning', 'message': 'coarse voxel spacing'},
            {'severity': 'warning', 'message': 'extra segment label'},
        ]

    def test_nested_annotation_path_traversal(self, stores_dir_factory):
        root = stores_dir_factory(('alpha',), with_annotations=True)
        _call_record_provenance(root, annotation_path=_DEFAULT_ANN_PATH)

        # The deepest node (the data array) carries the provenance attrs.
        arr = zarr.open_group(root / 'alpha.zarr', mode='r')[_DEFAULT_ANN_PATH]
        assert arr.attrs['annotator_id'] == 'bob'

        # Intermediate group does not pick up the leaf attrs.
        instance_group = zarr.open_group(root / 'alpha.zarr', mode='r')[
            'annotations/alice-xyz45678/inner-ear-structures-20260101-ab12'
        ]
        assert 'annotator_id' not in dict(instance_group.attrs)

    def test_strips_leading_trailing_slash(self, stores_dir_factory):
        root = stores_dir_factory(('alpha',), with_annotations=True)
        _call_record_provenance(root, annotation_path=f'/{_DEFAULT_ANN_PATH}/')

        arr = zarr.open_group(root / 'alpha.zarr', mode='r')[_DEFAULT_ANN_PATH]
        assert arr.attrs['annotator_id'] == 'bob'


# ===========================================================================
# record_provenance — durability
# ===========================================================================


class TestRecordProvenanceDurability:
    """Covers fsync/flush behavior that makes the JSONL index durable."""

    def test_fsync_called_on_jsonl_write(self, stores_dir_factory, monkeypatch):
        root = stores_dir_factory(('alpha',), with_annotations=True)
        calls: list[int] = []
        real_fsync = os.fsync

        def _recording_fsync(fd: int) -> None:
            calls.append(fd)
            real_fsync(fd)

        monkeypatch.setattr('voxhub_core.server.provenance.os.fsync', _recording_fsync)
        _call_record_provenance(root)
        assert len(calls) == 1

    def test_jsonl_flushed_before_function_returns(self, stores_dir_factory):
        root = stores_dir_factory(('alpha',), with_annotations=True)
        _call_record_provenance(root)

        # Fresh handle sees the newly-appended line immediately.
        jsonl = root / '.meta' / 'provenance.jsonl'
        contents = jsonl.read_text(encoding='utf-8')
        assert contents.endswith('\n')
        assert len([line for line in contents.splitlines() if line.strip()]) == 1


# ===========================================================================
# record_provenance — error paths
# ===========================================================================


class TestRecordProvenanceErrors:
    """Covers error propagation from record_provenance."""

    def test_missing_zarr_store_raises(self, stores_dir_factory):
        root = stores_dir_factory(('alpha',), with_annotations=True)

        with pytest.raises((FileNotFoundError, KeyError, ValueError)):
            _call_record_provenance(root, store_name='does-not-exist')

    def test_missing_annotation_path_raises(self, stores_dir_factory):
        root = stores_dir_factory(('alpha',), with_annotations=True)

        with pytest.raises(KeyError):
            _call_record_provenance(
                root, annotation_path='annotations/missing/group/data'
            )

    @pytest.mark.skipif(
        os.geteuid() == 0,  # type: ignore[attr-defined]
        reason='root bypasses chmod permissions',
    )
    def test_readonly_meta_directory(self, stores_dir_factory):
        root = stores_dir_factory(('alpha',), with_annotations=True)
        meta_dir = root / '.meta'
        meta_dir.mkdir()
        meta_dir.chmod(0o555)

        try:
            with pytest.raises(PermissionError):
                _call_record_provenance(root)
            assert not (meta_dir / 'provenance.jsonl').exists()
        finally:
            meta_dir.chmod(0o755)


# ===========================================================================
# validate_provenance_jsonl
# ===========================================================================


class TestValidateProvenanceJsonl:
    """Covers voxhub_core.server.provenance.validate_provenance_jsonl."""

    def test_missing_file_returns_empty_list(self, tmp_path):
        assert validate_provenance_jsonl(tmp_path / 'nope.jsonl') == []

    def test_empty_file_returns_empty_list(self, tmp_path):
        path = tmp_path / 'provenance.jsonl'
        path.touch()
        assert validate_provenance_jsonl(path) == []

    def test_valid_file_returns_empty_list(self, tmp_path, provenance_jsonl_factory):
        path = provenance_jsonl_factory(
            tmp_path / 'provenance.jsonl',
            entries=[
                {'event': 'push', 'store': 'alpha', 'n': 1},
                {'event': 'push', 'store': 'beta', 'n': 2},
                {'event': 'push', 'store': 'gamma', 'n': 3},
            ],
        )
        assert validate_provenance_jsonl(path) == []

    def test_malformed_line_reports_line_number(self, tmp_path, provenance_jsonl_factory):
        path = provenance_jsonl_factory(
            tmp_path / 'provenance.jsonl',
            entries=[
                {'ok': True, 'n': 1},
                'not-json-at-all',
                {'ok': True, 'n': 3},
            ],
        )
        errors = validate_provenance_jsonl(path)
        assert len(errors) == 1
        assert 'line 2' in errors[0]

    def test_multiple_malformed_lines_all_reported(
        self, tmp_path, provenance_jsonl_factory
    ):
        path = provenance_jsonl_factory(
            tmp_path / 'provenance.jsonl',
            entries=[
                'broken-1',
                {'ok': True},
                '{"missing": "quote}',
                'also broken',
            ],
        )
        errors = validate_provenance_jsonl(path)
        assert len(errors) == 3
        assert any('line 1' in e for e in errors)
        assert any('line 3' in e for e in errors)
        assert any('line 4' in e for e in errors)

    def test_blank_lines_ignored(self, tmp_path, provenance_jsonl_factory):
        path = provenance_jsonl_factory(
            tmp_path / 'provenance.jsonl',
            entries=[
                {'ok': True, 'n': 1},
                '',
                {'ok': True, 'n': 2},
                '   ',
                {'ok': True, 'n': 3},
            ],
        )
        assert validate_provenance_jsonl(path) == []

    def test_utf8_handling(self, stores_dir_factory):
        root = stores_dir_factory(('alpha',), with_annotations=True)
        _call_record_provenance(root, annotator_id='müller-李')

        # json.dumps escapes non-ASCII by default, but the round-trip must
        # restore the original string without mojibake.
        record = _read_jsonl(root / '.meta' / 'provenance.jsonl')[0]
        assert record['annotator_id'] == 'müller-李'
        assert validate_provenance_jsonl(root / '.meta' / 'provenance.jsonl') == []

"""Tests for voxhub_core.server.provenance.

Covers record_provenance (dual-write to zarr attrs + .meta/provenance.jsonl)
and validate_provenance_jsonl.  Concurrent-append scenarios live in
test_concurrency.py.

Plan: docs/testing/concurrency-and-provenance.md §3

Each test is currently skipped.  Remove the skip marker as tests are
implemented in a downstream worktree.

Fixtures expected:
    - zarr_root_factory
    - provenance_jsonl_factory
"""

import pytest

pytestmark = pytest.mark.skip(
    reason='stub — see docs/testing/concurrency-and-provenance.md §3'
)


# ===========================================================================
# record_provenance — happy paths
# ===========================================================================


class TestRecordProvenance:
    """Covers voxhub_core.server.provenance.record_provenance — happy paths."""

    def test_writes_zarr_array_attributes(self, zarr_root_factory):
        """Call record_provenance on a pre-populated annotation array → array
        attrs now have integrated_at, annotator_id, machine_id, nano_id,
        pull_session_id, source_nrrd_checksum, source_file, ontology,
        ontology_version. Types match expectations (ISO string timestamps,
        str ontology, int version)."""
        del zarr_root_factory

    def test_appends_single_line_to_jsonl_index(self, zarr_root_factory):
        """.meta/provenance.jsonl exists after call, has exactly one line,
        parses as dict with expected keys."""
        del zarr_root_factory

    def test_creates_meta_directory_if_missing(self, zarr_root_factory):
        """.meta/ doesn't exist pre-call → created with parents=True."""
        del zarr_root_factory

    def test_subsequent_calls_append_not_overwrite(self, zarr_root_factory):
        """Call twice → two lines in JSONL, both parse, both reference the
        same annotation_path."""
        del zarr_root_factory

    def test_timestamp_is_iso_utc(self, zarr_root_factory):
        """integrated_at parses as ISO 8601 with explicit UTC offset
        (datetime.fromisoformat(...) must succeed and tzinfo must not be None)."""
        del zarr_root_factory

    def test_issues_list_empty_when_none_passed(self, zarr_root_factory):
        """issues=None → JSONL record has issues: [] (not issues: null)."""
        del zarr_root_factory

    def test_issues_list_populated_when_warnings_passed(self, zarr_root_factory):
        """Pass two IssueRecords → JSONL record has both serialized with
        severity and message fields."""
        del zarr_root_factory

    def test_nested_annotation_path_traversal(self, zarr_root_factory):
        """annotation_path='annotations/alice-xyz/seg-20260101-ab12/data' →
        record_provenance correctly navigates via node[part] and updates
        attrs on the deepest array."""
        del zarr_root_factory

    def test_strips_leading_trailing_slash(self, zarr_root_factory):
        """Path with leading '/' or trailing '/' still resolves correctly
        via annotation_path.strip('/')."""
        del zarr_root_factory


# ===========================================================================
# record_provenance — durability
# ===========================================================================


class TestRecordProvenanceDurability:
    """Covers fsync/flush behavior that makes the JSONL index durable."""

    def test_fsync_called_on_jsonl_write(
        self, zarr_root_factory, monkeypatch
    ):
        """Monkey-patch os.fsync → assert called with the JSONL file
        descriptor at least once. Regression guard: the strategic plan
        mandates fsync as 'cheap insurance'."""
        del zarr_root_factory, monkeypatch

    def test_jsonl_flushed_before_function_returns(self, zarr_root_factory):
        """After record_provenance returns, opening the JSONL file in a
        fresh file handle sees the new line (no lingering buffering)."""
        del zarr_root_factory


# ===========================================================================
# record_provenance — error paths
# ===========================================================================


class TestRecordProvenanceErrors:
    """Covers error propagation from record_provenance."""

    def test_missing_zarr_store_raises(self, zarr_root_factory):
        """store_name points at nonexistent .zarr directory → zarr.open_group
        raises; record_provenance propagates without silent failure."""
        del zarr_root_factory

    def test_missing_annotation_path_raises(self, zarr_root_factory):
        """annotation_path traverses a group that doesn't exist → KeyError
        propagates."""
        del zarr_root_factory

    def test_readonly_meta_directory(self, zarr_root_factory):
        """.meta/ exists but is read-only → PermissionError propagates, no
        partial JSONL write (verify JSONL is unchanged after the raise)."""
        del zarr_root_factory


# ===========================================================================
# validate_provenance_jsonl
# ===========================================================================


class TestValidateProvenanceJsonl:
    """Covers voxhub_core.server.provenance.validate_provenance_jsonl."""

    def test_missing_file_returns_empty_list(self, tmp_path):
        """Per the docstring at server/provenance.py:118, a missing file is
        not an error — returns []."""
        del tmp_path

    def test_empty_file_returns_empty_list(self, tmp_path):
        """Zero-byte file → []."""
        del tmp_path

    def test_valid_file_returns_empty_list(
        self, tmp_path, provenance_jsonl_factory
    ):
        """Several valid JSON lines → []."""
        del tmp_path, provenance_jsonl_factory

    def test_malformed_line_reports_line_number(
        self, tmp_path, provenance_jsonl_factory
    ):
        """One bad line in an otherwise valid file → one error string in
        the result, mentioning the line number."""
        del tmp_path, provenance_jsonl_factory

    def test_multiple_malformed_lines_all_reported(
        self, tmp_path, provenance_jsonl_factory
    ):
        """Three malformed lines → three error strings."""
        del tmp_path, provenance_jsonl_factory

    def test_blank_lines_ignored(
        self, tmp_path, provenance_jsonl_factory
    ):
        """Valid JSON with blank lines interspersed → still []."""
        del tmp_path, provenance_jsonl_factory

    def test_utf8_handling(self, zarr_root_factory):
        """Annotator name with non-ASCII chars (e.g. 'müller', '李') round-
        trips through the JSONL without mojibake."""
        del zarr_root_factory

"""Tests for the ``voxhub-server rpc`` subcommand (wire protocol v2).

Covers the pinned JSON-over-stdin contract from
``docs/plans/c-transport-rpc-implementation-plan.md``: golden
request/response pairs per method, error envelopes (malformed stdin,
unknown method, missing fields, protocol mismatch), injection-shaped
params round-tripping as literals, shim/rpc response equivalence, and
the path-keyed checksum regression (basename collision, triage
2026-07-11 P1).

Function-level tests drive ``_run_rpc`` with a monkeypatched stdin;
subprocess-level tests drive ``python -m voxhub_core.server.cli rpc``
with the request on real stdin to assert exit codes and the absence of
tracebacks.
"""

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from _core_helpers import SHAPE, default_seg_label_map, write_seg_nrrd

from voxhub_core.server import cli as server_cli
from voxhub_schema import PROTOCOL_VERSION

# ===========================================================================
# Helpers
# ===========================================================================


def _request(method: str, params: dict | None = None, **envelope) -> dict:
    """Build a well-formed rpc request envelope (overridable via kwargs)."""
    req: dict = {
        'protocol_version': PROTOCOL_VERSION,
        'method': method,
        'params': params or {},
    }
    req.update(envelope)
    return req


def _bare_hex(path: Path) -> str:
    """Bare 64-hex sha256 of a file (compute_sha256 returns 'sha256:<hex>')."""
    return server_cli.compute_sha256(path).removeprefix('sha256:')


@pytest.fixture
def run_rpc(server_argv, monkeypatch):
    """Invoke ``_run_rpc`` with a JSON payload on a fake stdin.

    Returns a callable ``(payload, **argv_overrides) -> None``.
    ``payload`` may be a dict (dumped to JSON) or a raw string (written
    verbatim, for malformed-stdin cases).
    """

    def _run(payload, **argv_overrides) -> None:
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        monkeypatch.setattr('sys.stdin', io.StringIO(raw))
        server_cli._run_rpc(server_argv(**argv_overrides))

    return _run


# ===========================================================================
# Path-keyed checksum regression (basename collision — triage P1)
# ===========================================================================


class TestChecksumPathKeyedRegression:
    """Two stores with the SAME basename and DIFFERENT contents must both
    verify via staging-dir-relative path-keyed checksum entries.

    Under the old basename-keyed dict, one shared ``segmentation.seg.nrrd``
    key covered both files, so at most one store's checksum could match.
    """

    def test_two_stores_same_basename_different_contents_both_integrate(
        self,
        stores_dir_factory,
        staging_dir_with_annotations,
        run_rpc,
        parsed_stdout,
    ):
        stores_dir = stores_dir_factory(('alpha', 'bravo'))
        staging = staging_dir_with_annotations(store_names=['alpha', 'bravo'])
        # Give bravo different (still ontology-valid) bytes than alpha.
        bravo_map = default_seg_label_map()
        bravo_map[3, 3, 3] = 1
        write_seg_nrrd(
            staging / 'bravo' / 'segmentation.seg.nrrd',
            bravo_map,
            [
                {'id': 's0', 'name': 'cochlea', 'label_value': 1, 'color': '1 0 0'},
                {'id': 's1', 'name': 'vestibule', 'label_value': 2, 'color': '0 1 0'},
                {
                    'id': 's2',
                    'name': 'semicircular_canals',
                    'label_value': 3,
                    'color': '0 0 1',
                },
            ],
        )
        alpha_seg = staging / 'alpha' / 'segmentation.seg.nrrd'
        bravo_seg = staging / 'bravo' / 'segmentation.seg.nrrd'
        assert _bare_hex(alpha_seg) != _bare_hex(bravo_seg)

        run_rpc(
            _request(
                'integrate-annotations',
                {
                    'staging_dir': str(staging),
                    'annotator_id': 'alice',
                    'machine_id': 'machine-xyz',
                    'nano_id': 'deadbeef',
                    'expected_ontology': ['inner-ear-structures'],
                    'checksums': [
                        {
                            'path': 'alpha/segmentation.seg.nrrd',
                            'sha256': _bare_hex(alpha_seg),
                        },
                        {
                            'path': 'bravo/segmentation.seg.nrrd',
                            'sha256': _bare_hex(bravo_seg),
                        },
                    ],
                },
            ),
            stores_dir=stores_dir,
        )

        payload = parsed_stdout()
        assert payload['stores']['alpha']['status'] == 'integrated'
        assert payload['stores']['bravo']['status'] == 'integrated'

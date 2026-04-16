"""Shared fixtures for voxhub-client tests.

The e2e suite imports zarr-store seeding helpers from the core test
package (``_core_helpers.py``).  Duplicating that logic into the
client test tree would drift; instead we extend ``sys.path`` to the
core tests dir so the helpers are importable with zero surface cost.
"""

import argparse
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_CLIENT_TESTS = Path(__file__).parent
_CORE_TESTS = _CLIENT_TESTS.parent.parent / 'voxhub-core' / 'tests'
if str(_CORE_TESTS) not in sys.path:
    sys.path.insert(0, str(_CORE_TESTS))

from _core_helpers import (  # pyright: ignore[reportMissingImports]  # noqa: E402
    create_zarr_store,
    populate_store_annotation,
)
from loopback import (  # pyright: ignore[reportMissingImports]  # noqa: E402
    LoopbackRsyncTransfer,
    LoopbackSshRunner,
)

from voxhub_client.identity import Identity  # noqa: E402
from voxhub_client.server_config import ServerConfig  # noqa: E402


@pytest.fixture
def loopback_pull_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> SimpleNamespace:
    """Drive ``voxhub_client.cli._run_pull`` with transports replaced.

    Yields a namespace with:

    * ``stores_dir`` — root the spawned server resolves store names in.
    * ``server_config_path`` — TOML passed via ``VOXHUB_SERVER_CONFIG``.
      ``stderr_level = 'INFO'`` is set so cleanup's
      ``staging_dir_reaped`` log event reaches the runner's stderr
      capture (default server level is WARNING).
    * ``session_dest`` — local destination directory (pull target).
    * ``pull_log_path`` — rebinding of the user-facing audit log so
      tests don't touch ``$HOME``.
    * ``captured_runners`` — list populated on each ``SshRunner(...)``
      construction.  Access the most recent via ``runner()``.
    * ``run_pull(store, *, compress=False, include=())`` — callable
      that builds an argparse namespace and invokes the real
      ``_run_pull``.

    The four module-scope names in ``voxhub_client.cli``
    (``SshRunner``, ``RsyncTransfer``, ``get_identity``,
    ``get_server``) are monkeypatched in place; production code is
    unchanged.
    """
    import voxhub_client.cli as client_cli
    from voxhub_client import pull_log as client_pull_log

    stores_dir = tmp_path / 'stores'
    stores_dir.mkdir()

    config_path = tmp_path / 'server.toml'
    config_path.write_text(
        f"[storage]\nstores_dir = '{stores_dir}'\n\n[logging]\nstderr_level = 'INFO'\n"
    )

    session_dest = tmp_path / 'session'
    session_dest.mkdir()

    pull_log_path = tmp_path / 'pulls.jsonl'
    monkeypatch.setattr(client_pull_log, '_PULL_LOG', pull_log_path)

    identity = Identity(
        annotator_id='alice',
        nano_id='test1234',
        machine_id='m-test',
    )
    server = ServerConfig(host='localhost', port=None)
    monkeypatch.setattr(client_cli, 'get_identity', lambda: identity)
    monkeypatch.setattr(client_cli, 'get_server', lambda: server)

    captured_runners: list[LoopbackSshRunner] = []

    def _runner_factory(target: Any) -> LoopbackSshRunner:
        runner = LoopbackSshRunner(
            target=target,
            server_config_path=config_path,
        )
        captured_runners.append(runner)
        return runner

    monkeypatch.setattr(client_cli, 'SshRunner', _runner_factory)
    monkeypatch.setattr(client_cli, 'RsyncTransfer', LoopbackRsyncTransfer)

    def _run_pull(
        store: str,
        *,
        compress: bool = False,
        include: tuple[str, ...] = (),
    ) -> None:
        ns = argparse.Namespace(
            store=store,
            dest=str(session_dest),
            compress=compress,
            include_existing_annotations=list(include) if include else None,
        )
        client_cli._run_pull(ns)

    return SimpleNamespace(
        stores_dir=stores_dir,
        server_config_path=config_path,
        session_dest=session_dest,
        pull_log_path=pull_log_path,
        captured_runners=captured_runners,
        runner=lambda: captured_runners[-1] if captured_runners else None,
        run_pull=_run_pull,
    )


_STORE_NAME = 'alpha'

_SEG_SEGMENTS: list[dict[str, Any]] = [
    {'id': 's0', 'name': 'cochlea', 'label_value': 1, 'color': [1.0, 0.0, 0.0]},
    {'id': 's1', 'name': 'vestibule', 'label_value': 2, 'color': [0.0, 1.0, 0.0]},
]

_LMK_LABELS: list[str] = ['round_window', 'oval_window', 'cochlear_apex']


@pytest.fixture
def raw_only_store(loopback_pull_env: SimpleNamespace) -> str:
    """Seed a single store with the raw volume only; return its name."""
    store_path = loopback_pull_env.stores_dir / f'{_STORE_NAME}.zarr'
    create_zarr_store(store_path)
    return _STORE_NAME


@pytest.fixture
def store_with_annotations(
    loopback_pull_env: SimpleNamespace,
) -> tuple[str, list[str]]:
    """Seed a store with one seg + one landmark annotation.

    Returns ``(store_name, [seg_zarr_path, lmk_zarr_path])`` — the
    second element is ready to pass through
    ``run_pull(include=...)``.

    The landmark annotation requires a real ``(N, 3)`` float64 array,
    not the default label-map shape.  We overwrite the
    ``populate_store_annotation`` default with a correctly shaped
    array (pattern lifted from ``test_extraction._seed_landmarks``).
    """
    import numpy as np
    import zarr

    store_path = loopback_pull_env.stores_dir / f'{_STORE_NAME}.zarr'
    create_zarr_store(store_path)

    seg_path = populate_store_annotation(
        store_path,
        annotator_id='alice',
        nano_id='xyz45678',
        ontology='inner-ear-structures',
        date_str='20260101',
        short_random='ab12',
        kind='segmentation',
        segments=_SEG_SEGMENTS,
    )

    lmk_path = populate_store_annotation(
        store_path,
        annotator_id='alice',
        nano_id='xyz45678',
        ontology='inner-ear-landmarks',
        date_str='20260101',
        short_random='cd34',
        kind='landmarks',
        labels=_LMK_LABELS,
    )
    # Replace the default label-map-shaped array with an (N, 3) float64
    # points array so extract_landmarks' shape check passes.
    root = zarr.open_group(store_path, mode='r+')
    del root[f'{lmk_path}/data']
    points = np.array(
        [[-4.0, -5.0, -6.0], [-3.0, -4.0, -5.0], [-2.0, -3.0, -4.0]],
        dtype=np.float64,
    )
    arr = root.create_array(f'{lmk_path}/data', data=points)
    arr.update_attributes(
        {
            'annotator_id': 'alice',
            'nano_id': 'xyz45678',
            'integrated_at': '2026-01-01T00:00:00+00:00',
            'kind': 'landmarks',
            'labels': _LMK_LABELS,
            'ontology': 'inner-ear-landmarks',
            'ontology_version': 1,
        }
    )

    return _STORE_NAME, [seg_path, lmk_path]

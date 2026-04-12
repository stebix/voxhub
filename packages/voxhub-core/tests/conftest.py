"""Shared fixtures for voxhub-core tests."""

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import pytest
from _core_helpers import (
    create_zarr_store,
    populate_store_annotation,
    write_remote_manifest,
)

from voxhub_schema.ontology import load_ontology

# -- Logging: route structlog to stderr before any server handler runs -------


@pytest.fixture(autouse=True, scope='session')
def _configure_server_logging() -> None:
    """Force the server's logging pipeline so log output never pollutes stdout.

    Without this, structlog falls back to its default dev renderer which
    writes to stdout — breaking the single-JSON-line contract asserted by
    the ``parsed_stdout`` fixture.
    """
    from voxhub_core.server.logging import configure_logging

    configure_logging()


# -- Ontology fixtures -------------------------------------------------------


@pytest.fixture
def inner_ear_ontology():
    return load_ontology('inner-ear-structures')


@pytest.fixture
def landmark_ontology():
    return load_ontology('inner-ear-landmarks')


@pytest.fixture
def unconstrained_ontology():
    return load_ontology('unconstrained')


# -- Zarr root / WIP dir builders -------------------------------------------


@pytest.fixture
def zarr_root_factory(
    tmp_path: Path,
) -> Callable[..., Path]:
    """Build a ``stores/`` directory containing one or more zarr stores.

    Returns a callable ``(store_names=('default',), with_annotations=False,
    corrupt=(), dataset_attributes=None) -> Path`` producing the root
    directory.  Stores are created as ``<root>/<name>.zarr``.
    """

    def _build(
        store_names: Iterable[str] = ('default',),
        *,
        with_annotations: bool = False,
        corrupt: Iterable[str] = (),
        dataset_attributes: dict[str, dict[str, Any]] | None = None,
    ) -> Path:
        root = tmp_path / 'stores'
        root.mkdir(exist_ok=True)
        corrupt_set = set(corrupt)
        da_map = dataset_attributes or {}

        for name in store_names:
            store_path = root / f'{name}.zarr'
            create_zarr_store(store_path)

            if name in corrupt_set:
                # Delete the raw/full directory to trigger a probe error.
                import shutil

                shutil.rmtree(store_path / 'raw')

            if with_annotations:
                populate_store_annotation(store_path)

            if name in da_map:
                import zarr

                group = zarr.open_group(store_path, mode='r+')
                group.update_attributes({'dataset_attributes': da_map[name]})

        return root

    return _build


@pytest.fixture
def wip_dir_with_manifest(
    tmp_path: Path,
) -> Callable[..., Path]:
    """Build a WIP directory containing per-store subdirs plus a manifest.

    Returns a callable ``(store_names=..., ontologies=..., include_seg=True,
    include_lmk=False, seg_label_map=None, seg_segments=None,
    lmk_points=None, lmk_labels=None) -> Path`` producing the WIP directory.
    The zarr root is paired independently — this fixture only produces the
    client-side payload.
    """
    from _core_helpers import build_wip_dir_entries

    def _build(
        store_names: Iterable[str] = ('default',),
        *,
        ontologies: Iterable[str] = ('inner-ear-structures',),
        include_seg: bool = True,
        include_lmk: bool = False,
        seg_label_map: Any = None,
        seg_segments: list[dict[str, Any]] | None = None,
        lmk_points: list[list[float]] | None = None,
        lmk_labels: list[str] | None = None,
        lmk_coordinate_system: str = 'LPS',
    ) -> Path:
        wip_dir = tmp_path / 'wip'
        wip_dir.mkdir(exist_ok=True)

        store_list = list(store_names)
        for name in store_list:
            build_wip_dir_entries(
                wip_dir / name,
                include_seg=include_seg,
                include_lmk=include_lmk,
                seg_label_map=seg_label_map,
                seg_segments=seg_segments,
                lmk_points=lmk_points,
                lmk_labels=lmk_labels,
                lmk_coordinate_system=lmk_coordinate_system,
            )

        write_remote_manifest(
            wip_dir,
            store_names=store_list,
            expected_ontologies=list(ontologies),
        )
        return wip_dir

    return _build


# -- Argparse helpers --------------------------------------------------------


_DEFAULT_NAMESPACE_FIELDS: dict[str, Any] = {
    # Shared positional argument across list-stores, prepare-pull,
    # validate-attributes, healthcheck, and integrate-annotations.
    'zarr_root': None,
    # prepare-pull
    'stores': None,
    'ontologies': None,
    'wip_dir': None,
    'include_existing_annotations': None,
    'compress': False,
    # integrate-annotations
    'annotator_id': 'alice',
    'machine_id': 'machine-abc',
    'nano_id': 'abcd1234',
    'checksums': None,
    'force': False,
    # gc
    'ttl_hours': 24.0,
}


@pytest.fixture
def server_argv() -> Callable[..., argparse.Namespace]:
    """Build an ``argparse.Namespace`` shaped like a parsed voxhub-server
    invocation.  Every attribute expected by any ``_run_X`` handler is
    populated with a safe default; callers override via kwargs.
    """

    def _build(**overrides: Any) -> argparse.Namespace:
        fields = dict(_DEFAULT_NAMESPACE_FIELDS)
        fields.update(overrides)
        # Normalise Path objects to str (argparse gives str too).
        for key in ('zarr_root', 'wip_dir'):
            val = fields.get(key)
            if isinstance(val, Path):
                fields[key] = str(val)
        return argparse.Namespace(**fields)

    return _build


@pytest.fixture
def parsed_stdout(capsys: pytest.CaptureFixture[str]) -> Callable[[], dict[str, Any]]:
    """Read captured stdout, assert a single-line JSON object, return parsed.

    Must be called AFTER the handler has been invoked.  ``capsys.readouterr()``
    is destructive — calling this fixture twice returns the second call's output.
    """

    def _parse() -> dict[str, Any]:
        out = capsys.readouterr().out
        assert out.strip(), f'expected JSON stdout, got empty: {out!r}'
        # The server writes exactly one JSON line plus a trailing newline.
        lines = [line for line in out.split('\n') if line.strip()]
        assert len(lines) == 1, (
            f'expected single JSON line on stdout, got {len(lines)}: {out!r}'
        )
        return json.loads(lines[0])

    return _parse


# -- Subprocess runner -------------------------------------------------------


@pytest.fixture(scope='session')
def subprocess_server() -> Callable[..., subprocess.CompletedProcess[str]]:
    """Run ``voxhub-server`` via ``python -m voxhub_core.server.cli``.

    Returns a callable ``(*args, check=True, env=None) -> CompletedProcess``.
    stdout/stderr are captured as text.  Prefer ``-m`` invocation for
    hermeticity; a separate test exercises the installed entry point script.
    """

    def _run(
        *args: str,
        check: bool = False,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        cmd = [sys.executable, '-m', 'voxhub_core.server.cli', *args]
        full_env = os.environ.copy()
        if env:
            full_env.update(env)
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=check,
            env=full_env,
        )

    return _run

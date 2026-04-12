"""Shared fixtures for voxhub-core tests."""

import argparse
import json
import multiprocessing as mp
import os
import subprocess
import sys
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
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


@pytest.fixture(autouse=True)
def _default_server_config(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Provide a minimal ``VOXHUB_SERVER_CONFIG`` for every test.

    ``load_settings()`` is strict: missing config, missing ``[storage]``,
    or an invalid ``stores_dir`` raises ``SettingsError``.  Most tests
    exercise handlers / subprocesses that call ``load_settings()``
    indirectly — they don't care about the concrete path.  This fixture
    ensures those tests see a valid config by default.

    Tests that need to drive specific storage behaviour use the
    ``server_config_env`` fixture (or their own monkeypatch.setenv)
    after this one to override the default.
    """
    stores_dir = tmp_path_factory.mktemp('_default_stores')
    config_path = tmp_path_factory.mktemp('_default_server_cfg') / 'server.toml'
    config_path.write_text(f"[storage]\nstores_dir = '{stores_dir}'\n")
    monkeypatch.setenv('VOXHUB_SERVER_CONFIG', str(config_path))
    return config_path


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


# -- Zarr root / staging dir builders ---------------------------------------


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
def staging_dir_with_manifest(
    tmp_path: Path,
) -> Callable[..., Path]:
    """Build a staging directory containing per-store subdirs plus a manifest.

    Returns a callable ``(store_names=..., ontologies=..., include_seg=True,
    include_lmk=False, seg_label_map=None, seg_segments=None,
    lmk_points=None, lmk_labels=None) -> Path`` producing the staging
    directory.  The zarr root is paired independently — this fixture only
    produces the client-side payload.
    """
    from _core_helpers import build_staging_dir_entries

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
        staging_dir = tmp_path / 'staging'
        staging_dir.mkdir(exist_ok=True)

        store_list = list(store_names)
        for name in store_list:
            build_staging_dir_entries(
                staging_dir / name,
                include_seg=include_seg,
                include_lmk=include_lmk,
                seg_label_map=seg_label_map,
                seg_segments=seg_segments,
                lmk_points=lmk_points,
                lmk_labels=lmk_labels,
                lmk_coordinate_system=lmk_coordinate_system,
            )

        write_remote_manifest(
            staging_dir,
            store_names=store_list,
            expected_ontologies=list(ontologies),
        )
        return staging_dir

    return _build


# -- Server config env -------------------------------------------------------


@pytest.fixture
def server_config_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., Path]:
    """Write a temporary ``server.toml`` with ``[storage].stores_dir`` and
    point ``VOXHUB_SERVER_CONFIG`` at it.

    Returns a callable ``(stores_dir: Path, *, extra: str = '') -> Path``
    that writes the TOML and returns its path.  ``extra`` is appended
    verbatim (useful for injecting additional sections or malformed TOML
    in error-path tests).
    """

    def _build(stores_dir: Path, *, extra: str = '') -> Path:
        config_path = tmp_path / 'server.toml'
        body = f"[storage]\nstores_dir = '{stores_dir}'\n"
        if extra:
            body = body + extra if extra.startswith('\n') else body + '\n' + extra
        config_path.write_text(body)
        monkeypatch.setenv('VOXHUB_SERVER_CONFIG', str(config_path))
        return config_path

    return _build


# -- Argparse helpers --------------------------------------------------------


_DEFAULT_NAMESPACE_FIELDS: dict[str, Any] = {
    # Shared injected attribute (populated from settings.storage.stores_dir
    # by main()) used by list-stores, prepare-pull, validate-attributes,
    # healthcheck, and integrate-annotations.
    'stores_dir': None,
    # prepare-pull
    'stores': None,
    'ontologies': None,
    'staging_dir': None,
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
        for key in ('stores_dir', 'staging_dir'):
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


# -- Provenance / concurrency fixtures ---------------------------------------


@pytest.fixture
def provenance_jsonl_factory() -> Callable[..., Path]:
    """Write a provenance JSONL file at ``path`` from ``entries``.

    Each entry may be either a ``dict`` (serialised to JSON) or a raw string
    (written verbatim; useful for injecting malformed lines).
    """

    def _build(path: Path, *, entries: Iterable[dict[str, Any] | str]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            for entry in entries:
                if isinstance(entry, dict):
                    f.write(json.dumps(entry, ensure_ascii=False) + '\n')
                else:
                    f.write(entry + ('\n' if not entry.endswith('\n') else ''))
        return path

    return _build


@pytest.fixture
def concurrent_integrate_runner() -> Callable[..., list[dict[str, Any]]]:
    """Launch N ``integrate-annotations`` subprocesses in parallel.

    Each invocation is a dict with keys ``zarr_root``, ``staging_dir``,
    ``annotator_id``, ``nano_id`` and optionally ``machine_id``, ``force``.
    Returns a list of result dicts preserving invocation order, each carrying
    ``returncode``, parsed JSON ``stdout`` (best-effort, may be ``None``),
    ``raw_stdout``, ``stderr`` and the original ``invocation`` dict.
    """

    import tempfile as _tempfile

    def _run(
        invocations: list[dict[str, Any]],
        *,
        timeout: float = 120.0,
    ) -> list[dict[str, Any]]:
        procs: list[tuple[dict[str, Any], subprocess.Popen[str]]] = []
        for inv in invocations:
            # Each invocation gets its own TOML so concurrent tests that
            # target different ``zarr_root``s don't collide.
            cfg_fd, cfg_path = _tempfile.mkstemp(suffix='.toml', prefix='voxhub-cfg-')
            with os.fdopen(cfg_fd, 'w') as fh:
                fh.write(f"[storage]\nstores_dir = '{inv['zarr_root']}'\n")
            cmd = [
                sys.executable,
                '-m',
                'voxhub_core.server.cli',
                'integrate-annotations',
                str(inv['staging_dir']),
                '--annotator-id',
                inv['annotator_id'],
                '--machine-id',
                inv.get('machine_id', 'machine-abc'),
                '--nano-id',
                inv['nano_id'],
            ]
            if inv.get('force'):
                cmd.append('--force')
            full_env = os.environ.copy()
            full_env['VOXHUB_SERVER_CONFIG'] = cfg_path
            if inv.get('env'):
                full_env.update(inv['env'])
            p = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=full_env,
            )
            procs.append((inv, p))

        results: list[dict[str, Any]] = []
        for inv, p in procs:
            out, err = p.communicate(timeout=timeout)
            lines = [ln for ln in out.splitlines() if ln.strip()]
            parsed: dict[str, Any] | None = None
            if lines:
                try:
                    parsed = json.loads(lines[-1])
                except json.JSONDecodeError:
                    parsed = None
            results.append(
                {
                    'returncode': p.returncode,
                    'stdout': parsed,
                    'raw_stdout': out,
                    'stderr': err,
                    'invocation': inv,
                }
            )
        return results

    return _run


def _hold_lock_child(
    zarr_path_str: str,
    acquired: Any,
    release: Any,
    acquire_timeout: float,
) -> None:
    """Forked child entry point: acquire store_lock, signal, hold until release."""
    from voxhub_core.server.locks import store_lock

    with store_lock(Path(zarr_path_str), timeout=acquire_timeout):
        acquired.set()
        # Wait generously; parent signals release via event.
        release.wait(timeout=max(acquire_timeout * 4, 60.0))


@pytest.fixture
def held_lock() -> Callable[..., Any]:
    """Context manager that holds a ``store_lock`` on a zarr path via a child.

    Usage::

        with held_lock(zarr_path):
            ...  # lock is held by a separate process for the duration

    Uses the ``fork`` start method (Linux only — matches the deployment
    target per CLAUDE.md) so the worker can invoke ``store_lock`` without
    module-pickling gymnastics.
    """

    @contextmanager
    def _held(
        zarr_path: Path,
        *,
        acquire_timeout: float = 30.0,
    ) -> Iterator[None]:
        ctx = mp.get_context('fork')
        acquired = ctx.Event()
        release = ctx.Event()
        proc = ctx.Process(
            target=_hold_lock_child,
            args=(str(zarr_path), acquired, release, acquire_timeout),
        )
        proc.start()
        try:
            if not acquired.wait(timeout=acquire_timeout):
                proc.terminate()
                raise RuntimeError(
                    f'held_lock child did not acquire the lock within {acquire_timeout}s'
                )
            yield
        finally:
            release.set()
            proc.join(timeout=10.0)
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=5.0)

    return _held

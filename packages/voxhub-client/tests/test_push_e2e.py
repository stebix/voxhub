"""End-to-end loopback tests for ``voxhub push``.

Mirrors ``test_pull_e2e.py``: the real client ``_run_push`` orchestrator
drives the real server entrypoint (``python -m voxhub_core.server.cli``)
through the loopback shims (``loopback.py``) — no ``ssh``, ``sshd``,
``rsync``, or network listener.  The real sshd + rsync coverage lives in
``test_e2e_sshd.py``.

Headline properties proven at this layer:

* a pushed annotation lands at
  ``annotations/<annotator>-<nanoid>/<ontology>-<date>-<rand>/`` with
  full provenance in the zarr attrs,
* ``.meta/provenance.jsonl`` gains exactly one line per integrated file,
* the local manifest status flips to ``integrated``,
* a validation error means ZERO server contact,
* in-flight corruption (checksum mismatch) fails the store server-side
  and integrates nothing,
* a cleanup failure is a warning, never a failed push.
"""

import argparse
import json
import re
import shutil
from pathlib import Path
from types import SimpleNamespace

import attrs
import pytest
import zarr
from _core_helpers import (  # pyright: ignore[reportMissingImports]
    default_seg_label_map,
    write_seg_nrrd,
)
from loopback import (  # pyright: ignore[reportMissingImports]
    LoopbackRsyncTransfer,
    LoopbackSshRunner,
)

from voxhub_client import cli as client_cli
from voxhub_client.ssh import RemoteError
from voxhub_schema import PROTOCOL_VERSION, RemoteManifest

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

_SEG_SEGMENTS: list[dict[str, object]] = [
    {'id': 's0', 'name': 'cochlea', 'label_value': 1, 'color': '1 0 0'},
    {'id': 's1', 'name': 'vestibule', 'label_value': 2, 'color': '0 1 0'},
    {'id': 's2', 'name': 'semicircular_canals', 'label_value': 3, 'color': '0 0 1'},
]

_INSTANCE_RE = re.compile(r'inner-ear-structures-\d{8}-[_\-0-9a-z]{4}')


@pytest.fixture
def loopback_push_env(
    loopback_pull_env: SimpleNamespace,
    raw_only_store: str,
) -> SimpleNamespace:
    """A pulled session ready to annotate and push over the loopback shims.

    Runs a real pull first (same monkeypatch surface as
    ``loopback_pull_env``), then exposes ``run_push`` driving the real
    ``_run_push`` against the session directory.
    """
    env = loopback_pull_env
    env.run_pull(raw_only_store)
    session: Path = env.session_dest

    def _write_seg(name: str = 'my-work.seg.nrrd') -> Path:
        return write_seg_nrrd(session / name, default_seg_label_map(), _SEG_SEGMENTS)

    def _run_push(
        *,
        ontology: list[str] | None = None,
        unconstrained: bool = False,
        validate_only: bool = False,
        force: bool = False,
    ) -> None:
        ns = argparse.Namespace(
            session_dir=str(session),
            ontology=ontology,
            unconstrained=unconstrained,
            validate_only=validate_only,
            force=force,
        )
        client_cli._run_push(ns)

    return SimpleNamespace(
        env=env,
        session=session,
        store=raw_only_store,
        zarr_path=env.stores_dir / f'{raw_only_store}.zarr',
        provenance_path=env.stores_dir / '.meta' / 'provenance.jsonl',
        write_seg=_write_seg,
        run_push=_run_push,
    )


def _annotation_instances(zarr_path: Path, annotator_slug: str) -> list[str]:
    """Instance-dir names under ``annotations/<slug>/`` (empty if none)."""
    root = zarr.open_group(zarr_path, mode='r')
    try:
        group = root['annotations'][annotator_slug]
    except KeyError:
        return []
    return sorted(group.group_keys())


def test_push_end_to_end(loopback_push_env: SimpleNamespace) -> None:
    """Full push: annotation lands in zarr with provenance attrs, exactly
    one provenance line, manifest flips, staging dir reaped."""
    lb = loopback_push_env
    seg = lb.write_seg()
    seg_digest = client_cli._compute_sha256(seg)

    lb.run_push(ontology=['inner-ear-structures'])

    # -- annotation landed at the annotator-scoped path --------------------
    instances = _annotation_instances(lb.zarr_path, 'alice-test1234')
    assert len(instances) == 1
    assert _INSTANCE_RE.fullmatch(instances[0]), instances[0]

    # -- zarr attrs carry full provenance -----------------------------------
    root = zarr.open_group(lb.zarr_path, mode='r')
    arr = root[f'annotations/alice-test1234/{instances[0]}/data']
    a = dict(arr.attrs)
    assert a['annotator_id'] == 'alice'
    assert a['machine_id'] == 'm-test'
    assert a['nano_id'] == 'test1234'
    assert a['ontology'] == 'inner-ear-structures'
    assert a['ontology_version'] == 1
    assert a['source_nrrd_checksum'] == seg_digest
    assert a['pull_session_id'].startswith('vxhb-staging-')
    # No warnings were accepted, so the forced stamp must be absent.
    assert 'forced' not in a

    # -- exactly one provenance line -----------------------------------------
    lines = lb.provenance_path.read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record['event'] == 'push'
    assert record['store'] == lb.store
    assert record['annotator_id'] == 'alice'
    assert record['ontology'] == 'inner-ear-structures'
    assert record['issues'] == []
    assert 'forced' not in record
    assert f'annotations/alice-test1234/{instances[0]}/data' == record['annotation_path']

    # -- local manifest flipped ----------------------------------------------
    local = RemoteManifest.read(lb.session)
    assert local.protocol_version == PROTOCOL_VERSION
    assert local.stores[lb.store].status == 'integrated'
    assert local.pull_session_id == record['pull_session_id']

    # -- push staging dir reaped by the cleanup ACK ---------------------------
    runner: LoopbackSshRunner = lb.env.runner()
    prepare_push_responses = [
        r for r in runner.last_responses if set(r) == {'protocol_version', 'staging_dir'}
    ]
    assert len(prepare_push_responses) == 1
    assert not Path(prepare_push_responses[0]['staging_dir']).exists()


def test_push_response_parses_via_models(loopback_push_env: SimpleNamespace) -> None:
    """The raw integrate stdout parses through IntegrateResponse — wire drift
    between the live server and the schema models fails here."""
    from voxhub_schema import IntegrateResponse

    lb = loopback_push_env
    lb.write_seg()
    lb.run_push(ontology=['inner-ear-structures'])

    runner: LoopbackSshRunner = lb.env.runner()
    integrate_raw = next(r for r in runner.last_responses if 'stores' in r)
    parsed = IntegrateResponse.from_dict(integrate_raw)
    assert parsed.protocol_version == PROTOCOL_VERSION
    result = parsed.stores[lb.store]
    assert result.status == 'integrated'
    assert len(result.annotations) == 1
    assert result.annotations[0].path.startswith('annotations/alice-test1234/')


def test_push_validation_error_means_zero_ssh(
    loopback_push_env: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An error-severity pre-flight issue aborts before ANY server contact."""
    import numpy as np

    lb = loopback_push_env
    write_seg_nrrd(
        lb.session / 'bad-shape.seg.nrrd',
        np.zeros((4, 4, 4), dtype=np.int16),
        _SEG_SEGMENTS,
    )
    runners_after_pull = len(lb.env.captured_runners)

    with pytest.raises(SystemExit) as excinfo:
        lb.run_push(ontology=['inner-ear-structures'])
    assert excinfo.value.code == 1

    # No runner was even constructed past the pull's.
    assert len(lb.env.captured_runners) == runners_after_pull
    err = capsys.readouterr().err
    assert 'validation error' in err
    # Nothing landed server-side.
    assert _annotation_instances(lb.zarr_path, 'alice-test1234') == []
    assert not lb.provenance_path.exists()


def test_push_server_checksum_mismatch_fails_store(
    loopback_push_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A file corrupted after checksum computation (in flight) fails the
    store server-side; nothing is integrated and the failure is rendered."""

    @attrs.define
    class _TamperingTransfer(LoopbackRsyncTransfer):
        """Corrupts every uploaded .seg.nrrd after the transfer lands."""

        def push(
            self, local_path: str, remote_path: str, *, progress: bool = True
        ) -> None:
            super().push(local_path, remote_path, progress=progress)
            landed = self.staging_root / remote_path.lstrip('/')
            for f in landed.rglob('*.seg.nrrd'):
                f.write_bytes(f.read_bytes() + b'CORRUPTED-IN-FLIGHT')

    lb = loopback_push_env
    monkeypatch.setattr(client_cli, 'RsyncTransfer', _TamperingTransfer)
    lb.write_seg()

    with pytest.raises(SystemExit) as excinfo:
        lb.run_push(ontology=['inner-ear-structures'])
    assert excinfo.value.code == 1

    captured = capsys.readouterr()
    assert 'Checksum mismatch' in captured.out
    assert 'failed' in captured.out

    # Nothing integrated: no annotation group, no provenance line, no
    # manifest flip.
    assert _annotation_instances(lb.zarr_path, 'alice-test1234') == []
    assert not lb.provenance_path.exists()
    with pytest.raises(FileNotFoundError):
        RemoteManifest.read(lb.session)


def test_push_cleanup_failure_is_nonfatal(
    loopback_push_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failing cleanup ACK degrades to a warning; the push still succeeds
    and the server keeps the annotation (GC reaps the staging dir later)."""

    @attrs.define
    class _CleanupFailsRunner(LoopbackSshRunner):
        def run(self, method, params, *, timeout=None):
            if method == 'cleanup':
                raise RemoteError('boom', 'simulated cleanup failure')
            return super().run(method, params, timeout=timeout)

    lb = loopback_push_env
    config_path = lb.env.server_config_path
    leaked_runners: list[_CleanupFailsRunner] = []

    def _factory(target: object) -> _CleanupFailsRunner:
        runner = _CleanupFailsRunner(target=target, server_config_path=config_path)
        leaked_runners.append(runner)
        return runner

    monkeypatch.setattr(client_cli, 'SshRunner', _factory)
    lb.write_seg()

    lb.run_push(ontology=['inner-ear-structures'])  # must not raise

    captured = capsys.readouterr()
    assert 'GC will reap' in captured.err
    assert 'push complete' in captured.out
    assert len(_annotation_instances(lb.zarr_path, 'alice-test1234')) == 1

    # The staging dir was deliberately left behind (that is the point);
    # reap it so the loopback default staging root (gettempdir) stays clean.
    staging_dir = Path(leaked_runners[0].last_responses[0]['staging_dir'])
    shutil.rmtree(staging_dir, ignore_errors=True)


def test_push_unconstrained_explicit_opt_out(
    loopback_push_env: SimpleNamespace,
) -> None:
    """--unconstrained pushes without ontology flags and records the
    unconstrained ontology in provenance."""
    lb = loopback_push_env
    lb.write_seg()

    lb.run_push(unconstrained=True)

    instances = _annotation_instances(lb.zarr_path, 'alice-test1234')
    assert len(instances) == 1
    assert instances[0].startswith('unconstrained-')
    record = json.loads(lb.provenance_path.read_text().splitlines()[0])
    assert record['ontology'] == 'unconstrained'
    local = RemoteManifest.read(lb.session)
    assert local.stores[lb.store].expected_ontologies == ['unconstrained']

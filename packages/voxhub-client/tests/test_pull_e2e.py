"""End-to-end loopback tests for ``voxhub pull``.

Drives the real server entrypoint (``python -m voxhub_core.server.cli``)
from the real client ``_run_pull`` orchestrator.  ``SshRunner`` and
``RsyncTransfer`` are replaced with loopback shims (see
``loopback.py``); ``get_identity`` / ``get_server`` return fixture-
stubbed values.  No ``ssh``, ``sshd``, ``rsync``, or network listener
is required — the suite must pass on a disconnected machine.

The headline property this layer proves (not provable at the unit
level) is **rename-then-verify**: after a successful pull, moving the
session directory must not invalidate the trust sidecar, because the
sidecar hashes a sibling file via a relative path.
"""

import hashlib
import json
import shutil
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from voxhub_schema import PROTOCOL_VERSION
from voxhub_schema.models import PrepareResponse

pytestmark = [pytest.mark.e2e, pytest.mark.slow]


def _compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return f'sha256:{h.hexdigest()}'


def test_pull_raw_only(
    loopback_pull_env: SimpleNamespace,
    raw_only_store: str,
) -> None:
    """Raw-only pull lands all session artefacts, locked, no skipped ones."""
    loopback_pull_env.run_pull(raw_only_store)

    dest: Path = loopback_pull_env.session_dest
    raw_path = dest / 'raw.nrrd'
    manifest_path = dest / '.voxhub_pull.json'
    sidecar_path = dest / '.voxhub_pull.sha256'

    assert raw_path.is_file()
    assert manifest_path.is_file()
    assert sidecar_path.is_file()

    assert stat.S_IMODE(raw_path.stat().st_mode) == 0o444
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o444
    assert stat.S_IMODE(sidecar_path.stat().st_mode) == 0o444

    ref_dir = dest / 'reference'
    assert not ref_dir.exists() or not any(ref_dir.iterdir())

    response = loopback_pull_env.runner().last_responses[0]
    assert response['skipped_annotations'] == []
    assert response['store_name'] == raw_only_store
    assert response['raw_name'] == 'raw.nrrd'


def test_pull_with_annotations(
    loopback_pull_env: SimpleNamespace,
    store_with_annotations: tuple[str, list[str]],
) -> None:
    """Seg + landmark reference files land and parse as Slicer-compatible."""
    import nrrd

    store_name, ann_paths = store_with_annotations
    loopback_pull_env.run_pull(store_name, include=tuple(ann_paths))

    dest: Path = loopback_pull_env.session_dest
    ref_dir = dest / 'reference'
    assert ref_dir.is_dir()

    seg_files = list(ref_dir.glob('*.seg.nrrd'))
    lmk_files = list(ref_dir.glob('*.mrk.json'))
    assert len(seg_files) == 1, f'expected 1 seg file, got {seg_files}'
    assert len(lmk_files) == 1, f'expected 1 mrk file, got {lmk_files}'

    seg_data, _ = nrrd.read(str(seg_files[0]))
    assert seg_data.ndim == 3

    mrk = json.loads(lmk_files[0].read_text())
    assert mrk['markups'][0]['type'] == 'Fiducial'
    assert mrk['markups'][0]['coordinateSystem'] == 'LPS'
    assert len(mrk['markups'][0]['controlPoints']) == 3

    manifest = json.loads((dest / '.voxhub_pull.json').read_text())
    assert len(manifest['annotations']) == 2

    response = loopback_pull_env.runner().last_responses[0]
    assert response['skipped_annotations'] == []


def test_pull_store_not_found(
    loopback_pull_env: SimpleNamespace,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Missing store → client exits 1 with a descriptive error on stderr."""
    with pytest.raises(SystemExit) as exc_info:
        loopback_pull_env.run_pull('does-not-exist')
    assert exc_info.value.code == 1

    # Rich markup ``[store_not_found]`` gets interpreted as a style tag
    # and stripped from the terminal output, so we assert on the
    # message body rather than the structured code.
    captured = capsys.readouterr()
    assert 'Store not found' in captured.err
    assert 'does-not-exist' in captured.err

    dest: Path = loopback_pull_env.session_dest
    assert not (dest / 'raw.nrrd').exists()
    assert not (dest / '.voxhub_pull.json').exists()
    assert not (dest / '.voxhub_pull.sha256').exists()


def test_pull_surfaces_skipped_annotations(
    loopback_pull_env: SimpleNamespace,
    raw_only_store: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A missing annotation path surfaces in the response and the summary."""
    bogus_path = 'annotations/alice-xyz45678/not-real-ab12'
    loopback_pull_env.run_pull(raw_only_store, include=(bogus_path,))

    response = loopback_pull_env.runner().last_responses[0]
    skipped = response['skipped_annotations']
    assert len(skipped) == 1
    assert skipped[0]['path'] == bogus_path
    assert 'not found' in skipped[0]['reason'].lower()

    out = capsys.readouterr().out
    assert bogus_path in out
    assert 'Skipped' in out


def test_pull_sidecar_after_rename(
    loopback_pull_env: SimpleNamespace,
    raw_only_store: str,
    tmp_path: Path,
) -> None:
    """After renaming the session dir, the sidecar still matches the manifest.

    This is the core property of the in-session trust anchor: it moves
    with the data rather than being keyed to an absolute path.
    """
    loopback_pull_env.run_pull(raw_only_store)

    dest: Path = loopback_pull_env.session_dest
    moved = tmp_path / 'moved-session'
    shutil.move(str(dest), str(moved))

    sidecar_content = (moved / '.voxhub_pull.sha256').read_text().strip()
    assert sidecar_content.startswith('sha256:')

    recomputed = _compute_sha256(moved / '.voxhub_pull.json')
    assert sidecar_content == recomputed


def test_prepare_pull_response_matches_schema(
    loopback_pull_env: SimpleNamespace,
    raw_only_store: str,
) -> None:
    """The raw server stdout parses as a ``PrepareResponse`` — catches wire drift."""
    loopback_pull_env.run_pull(raw_only_store)

    response = loopback_pull_env.runner().last_responses[0]
    parsed = PrepareResponse.from_dict(response)

    assert parsed.protocol_version == PROTOCOL_VERSION
    assert parsed.store_name == raw_only_store
    assert parsed.raw_name == 'raw.nrrd'
    assert parsed.raw_checksum.startswith('sha256:')
    assert len(parsed.shape) == 3
    assert len(parsed.origin_lps) == 3
    assert len(parsed.spacing_mm) == 3
    assert len(parsed.space_directions) == 3


def test_pull_acks_staging_dir_reaped(
    loopback_pull_env: SimpleNamespace,
    raw_only_store: str,
) -> None:
    """Cleanup ACK fires, reaps the staging dir, emits the client_ack event."""
    loopback_pull_env.run_pull(raw_only_store)

    runner = loopback_pull_env.runner()
    prepare_response = runner.last_responses[0]
    staging_dir = Path(prepare_response['staging_dir'])

    assert not staging_dir.exists(), (
        f'expected staging dir to be reaped, still present: {staging_dir}'
    )

    ack_events = [
        e
        for e in runner.last_stderr_events
        if e.get('event') == 'staging_dir_reaped' and e.get('reason') == 'client_ack'
    ]
    assert len(ack_events) == 1, (
        f'expected exactly one client_ack reap event, got {ack_events}'
    )


def test_pull_twice_into_same_dest(
    loopback_pull_env: SimpleNamespace,
    raw_only_store: str,
) -> None:
    """A refresh pull to the same dest succeeds and refreshes the sidecar.

    The first pull locks ``.voxhub_pull.sha256`` read-only (0o444); the
    second must be able to rewrite it rather than failing with a
    ``PermissionError`` misreported as an invalid session.
    """
    loopback_pull_env.run_pull(raw_only_store)
    dest: Path = loopback_pull_env.session_dest
    sidecar_path = dest / '.voxhub_pull.sha256'
    assert stat.S_IMODE(sidecar_path.stat().st_mode) == 0o444

    # Second pull to the same dest — must not raise, must re-lock.
    loopback_pull_env.run_pull(raw_only_store)

    # The sidecar was refreshed to match the freshly re-staged manifest
    # (``prepared_at`` differs per prepare-pull, so the digest changes).
    refreshed = sidecar_path.read_text().strip()
    assert refreshed.startswith('sha256:')
    assert refreshed == _compute_sha256(dest / '.voxhub_pull.json')
    # The refreshed sidecar is locked read-only again.
    assert stat.S_IMODE(sidecar_path.stat().st_mode) == 0o444

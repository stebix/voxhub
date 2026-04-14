"""Client-side cache of the most recent ``list-stores`` response.

The server (see ``voxhub_core.server.catalog_cache``) bumps a
monotonic ``catalog_version`` counter on every catalog mutation. The
client keeps the last response on disk, tagged with that version, and
sends it back on the next call as ``--if-version N``. When the server
replies ``{"unchanged": true}``, the client reuses the cached payload
without re-parsing megabytes of JSON.

Correctness boundary
--------------------
The server is the single source of truth. The client never serves data
without a round-trip; the optimisation is purely the response payload
size. Cache corruption can only force one extra full re-fetch, never
wrong data.

This module is fully self-contained — it must not import
``voxhub_core`` (architecture rule, enforced by
``test_architecture.py::test_client_does_not_import_core``).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import attrs

if TYPE_CHECKING:
    from voxhub_client.ssh import SshRunner, SshTarget


CACHE_FILENAME: str = 'catalog.json'
DEFAULT_CACHE_DIRNAME: str = 'voxhub'

# Filesystem-safe characters for the per-server cache directory name.
# Anything outside ``[A-Za-z0-9._-]`` is replaced with ``_``.
_SAFE_KEY_RE: re.Pattern[str] = re.compile(r'[^A-Za-z0-9._-]')


def default_cache_root() -> Path:
    """Return ``~/.cache/voxhub`` (XDG-style on Linux).

    Notes
    -----
    On macOS ``~/.cache`` is non-standard; the deployment target is
    Linux-heavy so we accept that for now. Migrating to ``platformdirs``
    is a one-line change if the user base broadens.
    """
    return Path.home() / '.cache' / DEFAULT_CACHE_DIRNAME


def server_key_for(target: SshTarget) -> str:
    """Derive a stable, filesystem-safe cache key from an ``SshTarget``.

    The key encodes ``user``, ``host``, and ``port`` so multi-server
    annotators get isolated caches; reserved characters (``@``, ``:``)
    are replaced with ``_`` so the key works as a directory name.

    Parameters
    ----------
    target : SshTarget
        The active SSH target.

    Returns
    -------
    str
        A directory-safe key, e.g. ``voxhub_at_annotate.lab.edu`` or
        ``voxhub_at_host_p2222`` when a port is set.
    """
    raw = f'{target.user}@{target.host}'
    if target.port is not None:
        raw = f'{raw}:{target.port}'
    return _sanitise_key(raw)


def _sanitise_key(raw: str) -> str:
    """Replace any character outside ``[A-Za-z0-9._-]`` with ``_``."""
    cleaned = _SAFE_KEY_RE.sub('_', raw)
    if not cleaned:
        msg = f'server_key sanitises to empty string: {raw!r}'
        raise ValueError(msg)
    return cleaned


@attrs.define
class ClientCatalogCache:
    """On-disk cache of ``list-stores`` payloads, keyed by SSH target.

    Layout::

        <cache_root>/
            <server_key_a>/catalog.json
            <server_key_b>/catalog.json

    The payload schema is::

        {
            'catalog_version': int,
            'stores': list[dict],
        }

    Atomic writes use ``tempfile.mkstemp`` + ``os.replace`` so concurrent
    readers never observe a torn file. Read failures (missing file,
    corrupt JSON, schema mismatch) all degrade to ``None`` so the caller
    falls back to a full server fetch — the worst case is one wasted
    round-trip's worth of bytes, never wrong data.

    Parameters
    ----------
    cache_root : Path
        Directory containing one subdirectory per server. Created
        on first write.
    """

    cache_root: Path = attrs.field(factory=default_cache_root)

    def _server_dir(self, server_key: str) -> Path:
        # Re-sanitise defensively: callers that pass in raw SSH targets
        # via ``server_key_for`` are already safe; passing an arbitrary
        # string straight in must not let path separators escape the
        # cache root.
        safe = _sanitise_key(server_key)
        return self.cache_root / safe

    def _cache_file(self, server_key: str) -> Path:
        return self._server_dir(server_key) / CACHE_FILENAME

    def read(self, server_key: str) -> dict[str, Any] | None:
        """Return the cached payload, or ``None`` if absent / unreadable.

        Returns
        -------
        dict | None
            A dict with keys ``catalog_version`` (int) and ``stores``
            (list of dicts). ``None`` on any error path: the caller
            must treat a ``None`` as "no cache" and refetch in full.
        """
        path = self._cache_file(server_key)
        try:
            raw = path.read_bytes()
        except (FileNotFoundError, OSError, IsADirectoryError):
            return None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        version = data.get('catalog_version')
        stores = data.get('stores')
        if not isinstance(version, int) or not isinstance(stores, list):
            return None
        return {'catalog_version': version, 'stores': stores}

    def write(
        self,
        server_key: str,
        catalog_version: int,
        stores: list[dict[str, Any]],
    ) -> None:
        """Atomically persist *catalog_version* and *stores* for *server_key*.

        Uses ``tempfile.mkstemp`` in the destination directory followed
        by ``os.replace`` so readers always see either the previous
        complete file or the new complete file. The temp file is removed
        on any error path.

        Parameters
        ----------
        server_key : str
            Sanitised server identifier; see ``server_key_for``.
        catalog_version : int
            The ``catalog_version`` returned by the server.
        stores : list[dict]
            The ``stores`` list as returned by ``list-stores``.
        """
        target_dir = self._server_dir(server_key)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / CACHE_FILENAME

        payload = json.dumps(
            {'catalog_version': catalog_version, 'stores': stores},
            ensure_ascii=False,
        )

        fd, tmp_name = tempfile.mkstemp(
            prefix='.' + CACHE_FILENAME + '.',
            suffix='.tmp',
            dir=str(target_dir),
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, target)
        except Exception:
            if tmp_path.exists():
                with contextlib.suppress(OSError):
                    tmp_path.unlink()
            raise

    def clear(self, server_key: str | None = None) -> None:
        """Delete one server's cache, or every server's cache.

        Parameters
        ----------
        server_key : str | None
            If given, removes only that server's directory. If ``None``,
            removes the entire ``cache_root``. Both forms are no-ops
            when nothing is on disk.
        """
        target = self.cache_root if server_key is None else self._server_dir(server_key)
        if target.is_dir():
            shutil.rmtree(target)


def list_stores_cached(
    runner: SshRunner,
    cache: ClientCatalogCache,
    server_key: str,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Run ``list-stores`` with client-side ``catalog_version`` short-circuit.

    Always performs an SSH round-trip — the optimisation lives in the
    response payload size, not in skipping the call. When the server's
    catalog is unchanged since the cached version, the server replies
    ``{"unchanged": true}`` and this helper splices the cached
    ``stores`` list into the returned dict so the caller sees a uniform
    shape regardless of cache hit/miss.

    Parameters
    ----------
    runner : SshRunner
        SSH transport for the server.
    cache : ClientCatalogCache
        On-disk client cache.
    server_key : str
        Per-server cache key (see ``server_key_for``).
    force : bool, optional
        If ``True``, omit ``--if-version`` so the server always returns
        the full payload. Useful for ``--no-cache`` debugging and for
        rebuilding a corrupt cache.

    Returns
    -------
    dict
        Always shaped like a full ``list-stores`` response:
        ``{'protocol_version', 'catalog_version', 'stores'}``.
    """
    cached = None if force else cache.read(server_key)

    args: list[str] = ['list-stores']
    if cached is not None:
        args += ['--if-version', str(cached['catalog_version'])]

    response = runner.run(*args)

    if response.get('unchanged') and cached is not None:
        return {
            'protocol_version': response.get('protocol_version'),
            'catalog_version': response['catalog_version'],
            'stores': cached['stores'],
        }

    cache.write(
        server_key,
        int(response['catalog_version']),
        list(response['stores']),
    )
    return response

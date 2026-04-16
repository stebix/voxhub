"""Append-only audit log of local ``voxhub pull`` invocations.

Stored at ``~/.local/share/voxhub/pulls.jsonl`` — one JSON object per
line, human-readable with ``jq``.

This log is a **user-facing audit trail only** (what has been pulled,
from where, when).  It has no trust role: push does not read it, and it
plays no part in detecting tampered pull manifests.  That job belongs to
the in-session ``.voxhub_pull.sha256`` sidecar, which moves with the
session directory under rename/move/copy and does not depend on absolute
paths in ``$HOME``.

Keeping the log trust-free lets the annotator freely reorganise their
working directories without breaking push.
"""

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_PULL_LOG: Path = Path.home() / '.local' / 'share' / 'voxhub' / 'pulls.jsonl'


def append_entry(entry: dict[str, object]) -> None:
    """Append a single entry to ``pulls.jsonl``.

    Best-effort — OS errors are swallowed with a warning.  A failing
    audit write must never make a successful pull look failed.

    Parameters
    ----------
    entry : dict[str, object]
        Arbitrary JSON-serializable dict.  The caller owns the schema.
    """
    try:
        _PULL_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_PULL_LOG, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(entry) + '\n')
    except OSError as exc:
        log.warning('failed to append to pull log %s: %s', _PULL_LOG, exc)

"""Annotator identity management.

Stores identity in ``~/.config/voxhub/identity.json``.
"""

from __future__ import annotations

import hashlib
import json
import platform
import uuid
from pathlib import Path

import attrs

from voxhub_schema import generate_nano_id

_CONFIG_DIR = Path.home() / '.config' / 'voxhub'
_IDENTITY_FILE = _CONFIG_DIR / 'identity.json'


@attrs.define
class Identity:
    """Annotator identity."""

    annotator_id: str
    nano_id: str
    machine_id: str

    def to_dict(self) -> dict[str, str]:
        """Serialize to a plain dict."""
        return attrs.asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, str]) -> Identity:
        """Deserialize from a plain dict."""
        return cls(
            annotator_id=d['annotator_id'],
            nano_id=d['nano_id'],
            machine_id=d['machine_id'],
        )


def get_machine_id() -> str:
    """Compute a stable machine-specific identifier.

    Hashes a combination of hardware identifiers (MAC address, CPU
    identifier, hostname) to produce a stable hex string.

    Returns
    -------
    str
        Hex digest of the machine fingerprint.
    """
    mac = hex(uuid.getnode())
    hostname = platform.node()
    cpu = platform.processor() or platform.machine()
    fingerprint = f'{mac}:{hostname}:{cpu}'
    return hashlib.sha256(fingerprint.encode()).hexdigest()[:16]


def get_identity() -> Identity:
    """Read the configured annotator identity.

    Returns
    -------
    Identity

    Raises
    ------
    FileNotFoundError
        If the identity file does not exist.  The user should run
        ``set-identity`` first.
    """
    if not _IDENTITY_FILE.exists():
        msg = (
            f'Identity not configured. Run '
            f"'voxhub set-identity <name>' first.\n"
            f'Expected: {_IDENTITY_FILE}'
        )
        raise FileNotFoundError(msg)

    data = json.loads(_IDENTITY_FILE.read_text())
    return Identity.from_dict(data)


def set_identity(annotator_id: str) -> Identity:
    """Set the annotator identity.

    Generates a new nano-ID, computes the machine ID, and writes
    the identity file.

    Parameters
    ----------
    annotator_id : str
        Human-readable annotator identifier (e.g. ``'alice'``).

    Returns
    -------
    Identity
        The newly created identity.
    """
    identity = Identity(
        annotator_id=annotator_id,
        nano_id=generate_nano_id(),
        machine_id=get_machine_id(),
    )

    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _IDENTITY_FILE.write_text(json.dumps(identity.to_dict(), indent=2) + '\n')

    return identity

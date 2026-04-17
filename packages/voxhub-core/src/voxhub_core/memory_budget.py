"""Memory-budget helpers for in-RAM array materialization.

voxhub's extraction paths materialize a full zarr array into RAM
(``arr[:]``) before writing it out as NRRD.  On a small VPS (4 GB) with
multiple concurrent annotators this can OOM-kill the server.  This
module provides:

- :func:`estimate_array_bytes` — predicted RAM cost of an ``arr[:]``.
- :func:`read_available_bytes` — best-effort read of MemAvailable from
  ``/proc/meminfo`` (Linux only); ``None`` elsewhere.
- :class:`MemoryBudget` — a configurable warn/refuse policy.
- :func:`check` — evaluate a planned allocation against the budget,
  returning structured :class:`MemoryWarning` records (and optionally
  raising :class:`MemoryBudgetError`).

The local CLI typically uses :meth:`MemoryBudget.warn_only`; the server
constructs a stricter budget from its TOML configuration.
"""

from collections.abc import Sequence
from pathlib import Path

import attrs
import numpy as np

# Multiplier on raw array size that accounts for transient copies in the
# write path (``np.ascontiguousarray(...).tobytes()`` doubles peak).
_WRITE_PATH_TRANSIENT_FACTOR: float = 2.0

_MEMINFO_PATH = Path('/proc/meminfo')


class MemoryBudgetError(MemoryError):
    """Raised when an allocation would exceed the configured headroom.

    Subclass of ``MemoryError`` so callers that already catch the broad
    OOM-style error continue to work.  The ``warning`` attribute carries
    the structured :class:`MemoryWarning` that triggered the refusal.
    """

    def __init__(self, message: str, warning: 'MemoryWarning') -> None:
        super().__init__(message)
        self.warning = warning


@attrs.define(frozen=True)
class MemoryWarning:
    """A single advisory about a planned in-RAM materialization.

    Parameters
    ----------
    code : str
        Stable identifier: ``'large_volume'`` or ``'low_memory_available'``.
    message : str
        Human-readable summary suitable for logs and CLI output.
    volume_bytes : int
        Predicted RAM cost of the planned allocation (raw array bytes;
        transient write-path copies are multiplied in via the budget's
        ``safety_factor``).
    available_bytes : int | None
        Snapshot of ``MemAvailable`` at the time of the check.  ``None``
        when the platform cannot report it (non-Linux).
    threshold_bytes : int | None
        Threshold the allocation crossed.  For ``large_volume`` this is
        ``warn_threshold_bytes``; for ``low_memory_available`` it is the
        required headroom (``volume_bytes * safety_factor``).
    """

    code: str
    message: str
    volume_bytes: int
    available_bytes: int | None
    threshold_bytes: int | None

    def to_dict(self) -> dict[str, object]:
        return {
            'code': self.code,
            'message': self.message,
            'volume_bytes': self.volume_bytes,
            'available_bytes': self.available_bytes,
            'threshold_bytes': self.threshold_bytes,
        }


@attrs.define(frozen=True)
class MemoryBudget:
    """Policy for warning about and refusing oversized allocations.

    Parameters
    ----------
    warn_threshold_bytes : int
        Emit a ``large_volume`` warning when a planned allocation reaches
        this size.  Set to ``2**63 - 1`` to disable the size warning.
    refuse_when_low : bool
        When ``True``, raise :class:`MemoryBudgetError` if available RAM
        is below ``volume_bytes * safety_factor``.  When ``False`` the
        same condition only emits a ``low_memory_available`` warning.
    safety_factor : float
        Multiplier on ``volume_bytes`` that accounts for transient write
        copies.  ``2.0`` matches the current ``arr[:] + tobytes()`` peak;
        bump to ``3.0`` for the segmentation path which has an extra
        ``astype`` copy.
    """

    warn_threshold_bytes: int = 256 * 1024 * 1024
    refuse_when_low: bool = False
    safety_factor: float = _WRITE_PATH_TRANSIENT_FACTOR

    @classmethod
    def warn_only(
        cls,
        warn_threshold_bytes: int = 256 * 1024 * 1024,
    ) -> 'MemoryBudget':
        """Build a budget that warns but never refuses.  Suitable for the local CLI."""
        return cls(
            warn_threshold_bytes=warn_threshold_bytes,
            refuse_when_low=False,
            safety_factor=_WRITE_PATH_TRANSIENT_FACTOR,
        )

    @classmethod
    def disabled(cls) -> 'MemoryBudget':
        """Build a budget that never warns or refuses.  Useful for tests."""
        return cls(
            warn_threshold_bytes=2**63 - 1,
            refuse_when_low=False,
            safety_factor=_WRITE_PATH_TRANSIENT_FACTOR,
        )


def estimate_array_bytes(
    shape: Sequence[int],
    dtype: object,
) -> int:
    """Return ``np.prod(shape) * itemsize`` for a planned ``arr[:]`` materialization.

    Parameters
    ----------
    shape : Sequence[int]
        Array shape.
    dtype : object
        Element dtype, in any form ``np.dtype(...)`` accepts (a
        ``numpy.dtype``, a scalar type like ``np.uint16``, or a string
        like ``'float32'``).
    """
    itemsize = np.dtype(dtype).itemsize  # type: ignore[arg-type]
    n = 1
    for s in shape:
        n *= int(s)
    return n * itemsize


def read_available_bytes() -> int | None:
    """Return ``MemAvailable`` from ``/proc/meminfo`` in bytes; ``None`` if unknown.

    ``MemAvailable`` is the kernel's best estimate of memory available
    for new allocations without swapping; it is the right number to gate
    against, not ``MemFree``.  Returns ``None`` on platforms without
    ``/proc/meminfo`` (macOS, Windows) so callers can skip the check
    rather than fail.
    """
    try:
        with open(_MEMINFO_PATH, encoding='ascii') as fh:
            for line in fh:
                if line.startswith('MemAvailable:'):
                    parts = line.split()
                    # Format: ``MemAvailable:   12345 kB``
                    return int(parts[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def check(
    volume_bytes: int,
    *,
    budget: MemoryBudget,
    context: str | None = None,
) -> list[MemoryWarning]:
    """Evaluate a planned allocation against ``budget``.

    Parameters
    ----------
    volume_bytes : int
        Predicted raw size of the in-RAM array.
    budget : MemoryBudget
        Policy controlling warn thresholds and whether to refuse.
    context : str | None
        Short identifier (e.g. zarr store name) prepended to the warning
        message — purely cosmetic.

    Returns
    -------
    list[MemoryWarning]
        Zero or more advisories, in order: size warning first, then
        memory-availability warning.

    Raises
    ------
    MemoryBudgetError
        If ``budget.refuse_when_low`` is set and available memory falls
        below ``volume_bytes * budget.safety_factor``.
    """
    warnings: list[MemoryWarning] = []
    prefix = f'[{context}] ' if context else ''

    if volume_bytes >= budget.warn_threshold_bytes:
        warnings.append(
            MemoryWarning(
                code='large_volume',
                message=(
                    f'{prefix}volume materialization will use '
                    f'{_fmt_mb(volume_bytes)} of RAM '
                    f'(threshold: {_fmt_mb(budget.warn_threshold_bytes)})'
                ),
                volume_bytes=volume_bytes,
                available_bytes=read_available_bytes(),
                threshold_bytes=budget.warn_threshold_bytes,
            )
        )

    available = read_available_bytes()
    required = int(volume_bytes * budget.safety_factor)
    if available is not None and available < required:
        msg = (
            f'{prefix}only {_fmt_mb(available)} RAM available; '
            f'planned allocation needs ~{_fmt_mb(required)} '
            f'(volume {_fmt_mb(volume_bytes)} x safety {budget.safety_factor})'
        )
        warning = MemoryWarning(
            code='low_memory_available',
            message=msg,
            volume_bytes=volume_bytes,
            available_bytes=available,
            threshold_bytes=required,
        )
        warnings.append(warning)
        if budget.refuse_when_low:
            raise MemoryBudgetError(msg, warning)

    return warnings


def _fmt_mb(n: int) -> str:
    return f'{n / (1024 * 1024):.1f} MB'

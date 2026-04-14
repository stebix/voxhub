"""Canonical parsers for voxhub naming conventions.

The annotator slug ``<annotator_id>-<nano_id>`` appears in zarr paths
(``annotations/<slug>/<instance>/data``) and is the single source of truth
for per-annotation attribution.  The nano-ID alphabet includes ``-``,
so a naive ``rsplit('-', 1)`` is ambiguous — the nano-ID suffix has
fixed width (``NANO_ID_LENGTH``) and must be parsed positionally.

This module is deliberately placed in ``voxhub-schema`` (the shared
leaf of the dependency DAG) so both the server (``voxhub-core``) and
the client (``voxhub-client``) can validate slugs from a single
canonical implementation.
"""

from voxhub_schema.nano_id import ALPHABET, DEFAULT_SIZE

NANO_ID_ALPHABET: frozenset[str] = frozenset(ALPHABET)
"""Character set of a valid nano-ID, as a frozenset for O(1) membership."""

NANO_ID_LENGTH: int = DEFAULT_SIZE
"""Fixed width of an annotator slug's trailing nano-ID."""


class AnnotatorSlugError(ValueError):
    """Raised when an annotator slug does not conform to the expected schema.

    The expected schema is ``<annotator_id>-<nano_id>`` where:
      * ``annotator_id`` is a non-empty string (no further alphabet constraint)
      * ``nano_id`` is exactly ``NANO_ID_LENGTH`` characters drawn from
        ``NANO_ID_ALPHABET``
      * the two are separated by a single ``-``
    """


def parse_annotator_slug(slug: str) -> tuple[str, str]:
    """Parse an annotator slug into ``(annotator_id, nano_id)``.

    The nano-ID has fixed width (``NANO_ID_LENGTH``) and is the trailing
    portion of the slug, preceded by a literal ``-`` separator.  Everything
    before that separator is the annotator ID.

    Parameters
    ----------
    slug : str
        A slug of the form ``<annotator_id>-<nano_id>``.

    Returns
    -------
    tuple[str, str]
        ``(annotator_id, nano_id)``.

    Raises
    ------
    AnnotatorSlugError
        If the slug is too short, is missing the ``-`` separator at the
        expected position, has an empty annotator ID, or has a nano-ID
        suffix that contains characters outside the nano-ID alphabet.
    """
    if len(slug) < NANO_ID_LENGTH + 1:
        msg = (
            f'annotator slug {slug!r} is too short: expected '
            f"'<annotator_id>-<{NANO_ID_LENGTH}-char nano_id>' "
            f'(min {NANO_ID_LENGTH + 2} chars), got {len(slug)} chars'
        )
        raise AnnotatorSlugError(msg)

    sep_index = len(slug) - NANO_ID_LENGTH - 1
    if slug[sep_index] != '-':
        msg = (
            f'annotator slug {slug!r}: expected {"-"!r} separator at '
            f'position {sep_index} (immediately before the trailing '
            f'{NANO_ID_LENGTH}-char nano_id), got {slug[sep_index]!r}'
        )
        raise AnnotatorSlugError(msg)

    annotator_id = slug[:sep_index]
    nano_id = slug[sep_index + 1 :]

    if not annotator_id:
        msg = (
            f'annotator slug {slug!r}: annotator_id prefix is empty '
            f'(slug starts with the {"-"!r} separator)'
        )
        raise AnnotatorSlugError(msg)

    bad = sorted(set(nano_id) - NANO_ID_ALPHABET)
    if bad:
        msg = (
            f'annotator slug {slug!r}: nano_id suffix {nano_id!r} contains '
            f'characters outside the nano-ID alphabet: {bad!r}'
        )
        raise AnnotatorSlugError(msg)

    return annotator_id, nano_id

"""Nano-ID generation utility.

Shared by both client (identity setup) and core (annotation path creation).
"""

from random import random

ALPHABET: str = '_-0123456789abcdefghijklmnpqrstuvwxyz'
"""37 chars — no 'o' to avoid 0/o confusion."""

DEFAULT_SIZE: int = 8
"""37^8 ~ 3.5 trillion possible IDs."""


def generate_nano_id(alphabet: str = ALPHABET, size: int = DEFAULT_SIZE) -> str:
    """Generate a random nano-ID string.

    Parameters
    ----------
    alphabet : str
        Character set to draw from.
    size : int
        Length of the generated ID.

    Returns
    -------
    str
        Random ID of the requested length.
    """
    alphabet_len = len(alphabet)
    nano_id = ''
    for _ in range(size):
        nano_id += alphabet[int(random() * alphabet_len) | 0]
    return nano_id

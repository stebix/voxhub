"""Tests for nano-ID generation."""

from voxhub_schema.nano_id import ALPHABET, DEFAULT_SIZE, generate_nano_id


class TestNanoId:
    def test_default_length(self):
        assert len(generate_nano_id()) == DEFAULT_SIZE

    def test_custom_length(self):
        assert len(generate_nano_id(size=4)) == 4
        assert len(generate_nano_id(size=16)) == 16

    def test_uses_only_alphabet_characters(self):
        allowed = set(ALPHABET)
        for _ in range(200):
            nid = generate_nano_id()
            assert set(nid).issubset(allowed), f'{nid!r} has chars outside alphabet'

    def test_no_letter_o(self):
        """The alphabet excludes 'o' to avoid 0/o confusion."""
        assert 'o' not in ALPHABET
        # Probabilistic: over 2000 IDs (16k chars), 'o' should never appear.
        ids = ''.join(generate_nano_id() for _ in range(2000))
        assert 'o' not in ids

    def test_ids_are_not_constant(self):
        """Two consecutive calls should (almost certainly) differ."""
        ids = {generate_nano_id() for _ in range(100)}
        assert len(ids) > 1

    def test_custom_alphabet(self):
        nid = generate_nano_id(alphabet='ab', size=10)
        assert len(nid) == 10
        assert set(nid).issubset({'a', 'b'})

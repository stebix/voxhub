"""Tests for annotator-slug parsing."""

import pytest

from voxhub_schema.naming import (
    NANO_ID_ALPHABET,
    NANO_ID_LENGTH,
    AnnotatorSlugError,
    parse_annotator_slug,
)


class TestParseAnnotatorSlug:
    def test_simple_slug(self):
        assert parse_annotator_slug('alice-abcd1234') == ('alice', 'abcd1234')

    def test_multi_char_annotator_id(self):
        assert parse_annotator_slug('alice-wonderland-xyz45678') == (
            'alice-wonderland',
            'xyz45678',
        )

    def test_nano_id_starting_with_hyphen(self):
        """Positional parse handles a nano-ID whose first char is '-'."""
        # annotator_id='alice', nano_id='-bcd1234' (8 chars).
        # Full slug: 'alice' + '-' + '-bcd1234' = 'alice--bcd1234' (14 chars).
        annotator, nano = parse_annotator_slug('alice--bcd1234')
        assert annotator == 'alice'
        assert nano == '-bcd1234'

    def test_nano_id_ending_with_hyphen(self):
        # annotator_id='alice', nano_id='bcd1234-' (8 chars).
        annotator, nano = parse_annotator_slug('alice-bcd1234-')
        assert annotator == 'alice'
        assert nano == 'bcd1234-'

    def test_nano_id_with_underscores(self):
        # annotator_id='alice', nano_id='ab_cd_12' (8 chars, contains '_').
        annotator, nano = parse_annotator_slug('alice-ab_cd_12')
        assert annotator == 'alice'
        assert nano == 'ab_cd_12'

    def test_single_char_annotator(self):
        annotator, nano = parse_annotator_slug('a-bcdefghi')
        assert annotator == 'a'
        assert nano == 'bcdefghi'

    def test_empty_raises(self):
        with pytest.raises(AnnotatorSlugError, match='too short'):
            parse_annotator_slug('')

    def test_too_short_raises(self):
        # Len 8 (< NANO_ID_LENGTH + 1 = 9).
        with pytest.raises(AnnotatorSlugError, match='too short'):
            parse_annotator_slug('abcd1234')

    def test_missing_separator_raises(self):
        # Len 10, but position 1 is not '-'.
        with pytest.raises(AnnotatorSlugError, match=r'expected.*separator'):
            parse_annotator_slug('ab' + 'c' * 8)

    def test_empty_annotator_id_raises(self):
        # Len 9: slug = '-' + 8 chars. sep_index=0, slug[0]='-'. Annotator empty.
        with pytest.raises(AnnotatorSlugError, match='annotator_id prefix is empty'):
            parse_annotator_slug('-abcd1234')

    def test_invalid_nano_id_alphabet_raises(self):
        # Capital letters are not in the nano-ID alphabet.
        with pytest.raises(AnnotatorSlugError, match='outside the nano-ID alphabet'):
            parse_annotator_slug('alice-ABCD1234')

    def test_nano_id_forbidden_char_o(self):
        # 'o' is explicitly excluded from the nano-ID alphabet.
        with pytest.raises(AnnotatorSlugError, match='outside the nano-ID alphabet'):
            parse_annotator_slug('alice-oooooooo')

    def test_error_message_names_slug(self):
        slug = 'alice-ABCD1234'
        with pytest.raises(AnnotatorSlugError, match='alice-ABCD1234'):
            parse_annotator_slug(slug)

    def test_alphabet_includes_hyphen_and_underscore(self):
        assert '-' in NANO_ID_ALPHABET
        assert '_' in NANO_ID_ALPHABET

    def test_alphabet_excludes_letter_o(self):
        assert 'o' not in NANO_ID_ALPHABET

    def test_nano_id_length_is_eight(self):
        assert NANO_ID_LENGTH == 8

"""Word-subsequence title matching — models.title_words / titles_match.

The rule: normalize a title to a tuple of words (case/punctuation ignored, '&'
→ 'and'); two titles match if one is a word-level subsequence of the other. It
absorbs MusicBrainz-vs-Bandcamp differences without enumerating them; safety is
the caller's artist-scoping + uniqueness guard, not this rule.
"""

from __future__ import annotations

from harmonist.models import title_words, titles_match


def _m(a: str, b: str) -> bool:
    return titles_match(title_words(a), title_words(b))


def test_exact():
    assert _m("Music Industry 3. Fitness Industry 1.", "Music Industry 3. Fitness Industry 1.")


def test_trailing_ep_suffix():
    # The Mogwai case: MB drops "EP", Bandcamp keeps it.
    assert _m("Music Industry 3. Fitness Industry 1.", "Music Industry 3. Fitness Industry 1. EP")


def test_parenthetical_suffix():
    assert _m("Kid A", "Kid A (Deluxe Edition)")


def test_dropped_leading_the():
    assert _m("Bends", "The Bends")


def test_punctuation_and_case_ignored():
    assert _m("OK Computer", "ok:computer!")


def test_ampersand_normalized():
    assert _m("Sea & Cake", "Sea and Cake")


def test_non_contiguous_subsequence():
    # Words in order, gaps allowed (loose — but only ever compared within one artist).
    assert _m("Music Industry 1", "Music Industry 3 Fitness Industry 1")


def test_word_order_matters():
    assert not _m("Fitness Music", "Music Fitness Industry")


def test_different_titles_dont_match():
    assert not _m("Rave Tapes", "The Bad Fire")


def test_empty_never_matches():
    assert not _m("", "Anything")
    assert not _m("Anything", "")

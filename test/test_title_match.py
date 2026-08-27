"""Word-subsequence title matching — models.title_words / titles_match.

The rule: normalize a title to a tuple of words (case/punctuation ignored, '&'
→ 'and'); two titles match if one is a word-level subsequence of the other. It
absorbs MusicBrainz-vs-Bandcamp differences without enumerating them; safety is
the caller's artist-scoping + uniqueness guard, not this rule.
"""

from __future__ import annotations

from harmonist.models import title_with_disambiguation, title_words, titles_match


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


# ---------- the exact disambiguation rule, which is NOT the above (#283) ----------
#
# `titles_match` would accept "Obreel" against "Obreel (expanded edition)" — and
# against "(deluxe edition)" or "(2019 remaster)" just as readily, since it judges
# on words alone. That latitude is earned where it is used, inside an
# artist-scoped and uniqueness-guarded purchase match. Comparing a file's tags
# against the release they came from is a different question with a better answer
# available: the release states its disambiguation, so the accepted spelling can
# be built exactly instead of guessed at.


def test_the_picard_spelling_is_built_from_the_release():
    assert title_with_disambiguation("Obreel", "expanded edition") == "Obreel (expanded edition)"


def test_there_is_no_second_spelling_without_a_disambiguation():
    """The guard that stops a nonsense alias existing at all.

    Without it the function yields `Obreel ()`, which no file carries — so the
    behaviour above it looks correct while the value it is built on is garbage.
    Asserted here rather than through the tagger, where a broken guard produces a
    string that happens not to match anything and the test passes regardless.
    """
    assert title_with_disambiguation("Obreel", None) is None
    assert title_with_disambiguation("Obreel", "") is None
    assert title_with_disambiguation("Obreel", "   ") is None


def test_there_is_no_second_spelling_without_a_title():
    assert title_with_disambiguation(None, "expanded edition") is None
    assert title_with_disambiguation("", "expanded edition") is None

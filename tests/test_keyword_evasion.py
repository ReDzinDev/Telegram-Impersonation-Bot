"""
Keyword matching must tolerate separators and leet, without inventing false
positives (E-4).

A keyword hit is hard-wired to the ban band, which makes this the highest-value
evasion in the system — and it was defeated by a space. _match_wildcard_pattern
folds Unicode but then compares literal substrings, so "a d m i n", "a.d.m.i.n"
and "adm1n" all sailed past the keyword "admin".

The hazard in fixing it is over-matching. A bare keyword is a SUBSTRING match, so
naive gap tolerance turns ordinary names into bans. Measured against real-looking
names:

    keyword "mod"     -> "Mo Diaz"      false positive
    keyword "ceo"     -> "Ce Oliveira"  false positive
    keyword "admin"   -> "Ad Minister"  false positive
    keyword "support" -> "Sup Porter"   false positive

A minimum keyword length does not save this — "support" is seven characters and
still breaks. What separates the two cases is the SHAPE: an evasion puts
separators between nearly every character (4 gaps out of 4 possible), while an
innocent name has exactly one, at a word boundary (1 out of 6). So the rule is
proportional: a gapped match only counts when most of the available positions
really are gapped.
"""
import pytest

from src.utils.detector import check_name_similarity, check_reserved_keywords


def _kw(pattern):
    return [{"pattern": pattern, "is_regex": False}]


def _hit(pattern, text):
    return check_reserved_keywords(text, None, None, _kw(pattern)) == pattern


# ── evasions that must be caught ──────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "a d m i n",
    "a.d.m.i.n",
    "a-d-m-i-n",
    "a_d_m_i_n",
    "A D M I N",
    "a  d  m  i  n",
])
def test_separator_padded_keyword_is_caught(text):
    assert _hit("admin", text), f"{text!r} evaded the keyword 'admin'"


@pytest.mark.parametrize("text", ["adm1n", "4dmin", "@dmin", "adm!n".replace("!", "1")])
def test_leet_substituted_keyword_is_caught(text):
    assert _hit("admin", text), f"{text!r} evaded the keyword 'admin'"


def test_leet_and_separators_combined():
    assert _hit("admin", "4 d m 1 n")


def test_plain_keyword_still_matches_plainly():
    assert _hit("admin", "Group Admin")
    assert _hit("support", "Official Support Team")


# ── false positives that must NOT be introduced ───────────────────────────────
#
# Each of these matched under naive gap tolerance. They are ordinary names.

@pytest.mark.parametrize("keyword,name", [
    ("mod",     "Mo Diaz"),
    ("ceo",     "Ce Oliveira"),
    ("admin",   "Ad Minister"),
    ("support", "Sup Porter"),
    ("vip",     "Vi Pham"),
    ("mod",     "Mohammed Odeh"),
])
def test_ordinary_names_are_not_flagged(keyword, name):
    assert not _hit(keyword, name), (
        f"keyword {keyword!r} wrongly flagged the name {name!r}"
    )


def test_a_single_word_boundary_gap_is_not_an_evasion():
    """One gap in a seven-letter keyword is a name, not an attack."""
    assert not _hit("support", "Sup Porter")
    assert not _hit("moderator", "Moder Ator")


@pytest.mark.parametrize("keyword,name", [
    ("moderator", "Mod Era Tor"),
    ("moderator", "Mo De Ra Tor"),
    ("support",   "Sup Por Ter"),
])
def test_a_few_coincidental_gaps_are_still_not_an_evasion(keyword, name):
    """
    The proportional requirement, not just the two-gap floor. A three-part name
    supplies two or three gaps by coincidence; a nine-letter keyword needs five
    before we will believe it was padded deliberately.
    """
    assert not _hit(keyword, name), (
        f"keyword {keyword!r} wrongly flagged the name {name!r}"
    )


def test_short_keywords_do_not_gain_gap_tolerance_recklessly():
    """
    Two characters leaves no room to distinguish an evasion from a coincidence,
    so gap matching must not apply at all.
    """
    assert not _hit("hi", "H i")
    assert not _hit("ab", "A b")


# ── display-name leet folding ─────────────────────────────────────────────────
#
# _LEET_MAP was applied only to usernames, never to display names — even though
# display names are the primary impersonation vector.

def test_leet_display_name_matches_the_protected_name():
    match, matched, score = check_name_similarity("J0hn Sm1th", ["John Smith"], 85)
    assert match is True, f"scored only {score}"
    assert matched == "John Smith"


def test_heavier_leet_display_name_matches():
    match, _, score = check_name_similarity("J0hn 5m1th", ["John Smith"], 85)
    assert match is True, f"scored only {score}"


def test_leet_folding_does_not_collapse_distinct_names():
    """The pass must not make unrelated names look alike."""
    match, _, _ = check_name_similarity("Alice Zhang", ["Bob Smith"], 85)
    assert match is False


def test_digits_in_a_legitimately_different_name_still_differ():
    match, _, _ = check_name_similarity("User 12345", ["John Smith"], 85)
    assert match is False

"""
Unit tests for the pure detection primitives in src/utils/detector.py.
These need no DB or network — they pin the matching behaviour so future
refactors can't silently change who gets flagged.
"""
import pytest

from src.utils.detector import (
    _match_wildcard_pattern,
    _normalize_handle,
    check_reserved_keywords,
    check_username_similarity,
    check_name_similarity,
    check_homoglyph_danger,
)


# ── wildcard keyword matching ─────────────────────────────────────────────────

@pytest.mark.parametrize("pattern,text,expected", [
    ("admin",   "this is admin support", True),   # bare = substring
    ("admin",   "ADMIN",                 True),    # case-insensitive
    ("admin",   "administrator",         True),    # substring
    ("admin",   "the moderator",         False),
    ("admin*",  "admin support",         True),    # starts-with
    ("admin*",  "the admin",             False),   # not at start
    ("*admin",  "super admin",           True),    # ends-with
    ("*admin",  "admin super",           False),
    ("*admin*", "an administrator here", True),    # explicit contains
    ("*",       "anything",              False),   # bare star = ignored
    ("",        "anything",              False),
])
def test_match_wildcard_pattern(pattern, text, expected):
    assert _match_wildcard_pattern(pattern, text) is expected


def test_check_reserved_keywords_plain_and_wildcard():
    kws = [
        {"pattern": "support", "is_regex": False},
        {"pattern": "*ceo*",   "is_regex": False},
    ]
    assert check_reserved_keywords("Official Support", None, None, kws) == "support"
    assert check_reserved_keywords("Jane", "theceoguy", None, kws) == "*ceo*"
    assert check_reserved_keywords("Random Person", "random", None, kws) is None


def test_check_reserved_keywords_matches_bio():
    kws = [{"pattern": "giveaway", "is_regex": False}]
    assert check_reserved_keywords("Clean Name", "cleanuser", "join my giveaway", kws) == "giveaway"


def test_check_reserved_keywords_regex():
    kws = [{"pattern": r"official.*team", "is_regex": True}]
    assert check_reserved_keywords("Official Support Team", None, None, kws) == r"official.*team"
    assert check_reserved_keywords("unofficial", None, None, kws) is None


def test_check_reserved_keywords_bad_regex_is_skipped():
    kws = [{"pattern": r"(unclosed", "is_regex": True}]
    # Must not raise — bad regex is swallowed and treated as no match
    assert check_reserved_keywords("anything", None, None, kws) is None


# ── username normalization / similarity ───────────────────────────────────────

def test_normalize_handle_folds_leet_and_separators():
    assert _normalize_handle("J0hn_Smith") == "johnsmith"
    assert _normalize_handle("m1ke.admin") == "mikeadmin"
    assert _normalize_handle("a_b-c d") == "abcd"


def test_username_leet_variant_scores_as_high_as_clean():
    # j0hn_smith should match johnsmith well above threshold thanks to folding
    match, val, score = check_username_similarity("j0hn_smith", ["johnsmith"], threshold=85)
    assert match is True
    assert val == "johnsmith"
    assert score >= 85


def test_username_exact_match():
    match, val, score = check_username_similarity("cryptoboss", ["cryptoboss"], threshold=88)
    assert match is True and score == 100


def test_username_unrelated_below_threshold():
    match, val, score = check_username_similarity("totallydifferent", ["cryptoboss"], threshold=88)
    assert match is False


def test_username_empty_inputs():
    assert check_username_similarity("", ["x"], 85) == (False, None, 0)
    assert check_username_similarity("x", [], 85) == (False, None, 0)


# ── name similarity ───────────────────────────────────────────────────────────

def test_name_similarity_basic():
    match, val, score = check_name_similarity("John Smith", ["John Smith"], threshold=85)
    assert match is True and score == 100


def test_name_similarity_unrelated():
    match, _, _ = check_name_similarity("Zebra Quux", ["John Smith"], threshold=85)
    assert match is False


# ── name normalization: evasions that must now be caught ──────────────────────

@pytest.mark.parametrize("evasion", [
    "JOHN SMITH",              # all-caps
    "Ｊｏｈｎ　Ｓｍｉｔｈ",           # fullwidth unicode
    "Јоhn Ѕмітh",              # whole-script Cyrillic confusables
    "John​Smith",         # zero-width space injected
    "John Smith | Support",    # suffix-append dilution
])
def test_name_similarity_catches_unicode_evasions(evasion):
    match, val, score = check_name_similarity(evasion, ["John Smith"], threshold=85)
    assert match is True, f"{evasion!r} should match after normalization"
    assert val == "John Smith"
    assert score >= 85


def test_name_normalization_does_not_match_different_person():
    # Normalization must not turn an unrelated name into a match
    match, _, _ = check_name_similarity("Michael Brown", ["John Smith"], threshold=85)
    assert match is False


def test_keyword_matching_folds_confusables_and_fullwidth():
    kws = [{"pattern": "admin", "is_regex": False}]
    assert check_reserved_keywords("аdmin", None, None, kws) == "admin"       # Cyrillic а
    assert check_reserved_keywords("ａｄｍｉｎ", None, None, kws) == "admin"      # fullwidth
    assert check_reserved_keywords("moderator", None, None, kws) is None       # clean negative


def test_fold_text_strips_and_normalizes():
    from src.utils.detector import fold_text
    assert fold_text("ＡＢＣ") == "abc"
    assert fold_text("A​B‌C") == "abc"
    assert fold_text("  Hello   World  ") == "hello world"
    assert fold_text("") == ""


# ── homoglyph detection ───────────────────────────────────────────────────────

def test_homoglyph_flags_cyrillic_lookalike():
    # 'Аdmin' with a Cyrillic А (U+0410) is a classic mixed-script lookalike
    assert check_homoglyph_danger("Аdmin") is True


def test_homoglyph_clean_ascii_is_safe():
    assert check_homoglyph_danger("Admin") is False


# ── superset name matches (F-2) ───────────────────────────────────────────────
#
# fuzz.token_set_ratio returns 100 whenever one token set is a subset of the
# other, no matter how many extra words the longer side carries. Taking that
# score at face value means "John Smith Fan Club" is indistinguishable from
# "John Smith" and lands in the ban band at full confidence.
#
# A superset match is still real evidence — "John Smith | Support" is textbook
# impersonation — so the goal is not to reject it but to scale confidence with
# how much unmatched text surrounds the borrowed name.

WHITELIST_NAME = ["John Smith"]


def test_exact_name_match_keeps_full_confidence():
    match, matched, score = check_name_similarity("John Smith", WHITELIST_NAME, 85)
    assert (match, matched, score) == (True, "John Smith", 100)


def test_one_extra_token_stays_high_confidence():
    """'John Smith Support' is impersonation-shaped and should still ban."""
    match, _, score = check_name_similarity("John Smith Support", WHITELIST_NAME, 85)
    assert match is True
    assert score >= 90


def test_two_extra_tokens_flag_but_below_ban_band():
    """Enough signal to alert a human, not enough to ban unreviewed."""
    match, _, score = check_name_similarity("John Smith Fan Club", WHITELIST_NAME, 85)
    assert match is True
    assert 85 <= score < 90


def test_name_merely_mentioning_an_admin_is_not_flagged():
    match, matched, score = check_name_similarity(
        "I love John Smith memes", WHITELIST_NAME, 85
    )
    assert match is False
    assert score < 85


def test_superset_penalty_never_scores_below_plain_token_sort():
    """The penalty is a ceiling adjustment, not a way to undercut the base score."""
    from rapidfuzz import fuzz

    target = "John Smith a b c d e f g h"
    _, _, score = check_name_similarity(target, WHITELIST_NAME, 0)
    assert score >= int(fuzz.token_sort_ratio(target.casefold(), "john smith"))


# ── Unicode folding (E-1, E-5) ────────────────────────────────────────────────
#
# fold_text relied on NFKC plus a 60-entry hand-rolled confusable map, which left
# three documented holes:
#
#   E-1  Unicode small-capital and phonetic letters (U+1D00-1D2B, U+0250-02AF)
#        have NO NFKC decomposition and were almost entirely absent from the map.
#        This is the most common stylized-name style on Telegram, and it defeated
#        the reserved-keyword stage — the one hard-wired to the ban band.
#   E-5  NFKC ran BEFORE the combining-mark strip, so any accent with a
#        precomposed form was already a single code point by the time the filter
#        looked, and survived. Categories Mc and Me were never filtered at all.
#   E-5  The map was applied BEFORE casefold(), so it needed both cases of every
#        entry — and had the lowercase Cyrillic forms without their capitals.

from src.utils.detector import fold_text

SMALL_CAPS_JOHN_SMITH = "\u1d0a\u1d0f\u029c\u0274 \u0455\u1d0d\u026a\u1d1b\u029c"
SMALL_CAPS_ADMIN = "\u1d00\u1d05\u1d0d\u026a\u0274"


def test_small_capital_letters_fold_to_ascii():
    assert fold_text(SMALL_CAPS_JOHN_SMITH) == "john smith"


def test_stylized_name_matches_the_plain_whitelist_entry():
    match, matched, score = check_name_similarity(
        SMALL_CAPS_JOHN_SMITH, ["John Smith"], 85
    )
    assert match is True
    assert score == 100


def test_stylized_keyword_is_caught():
    """A keyword hit is hard-wired to the ban band, so this was the top evasion."""
    keywords = [{"pattern": "admin", "is_regex": False}]
    assert check_reserved_keywords(SMALL_CAPS_ADMIN, None, None, keywords) == "admin"


def test_phonetic_and_script_variants_fold():
    assert fold_text("\u0261oogle") == "google"        # LATIN SMALL LETTER SCRIPT G
    assert fold_text("\u0282mith") == "smith"          # S WITH HOOK


def test_precomposed_accents_are_stripped():
    """NFKC-before-strip meant these survived and scored 80 against the plain name."""
    assert fold_text("J\u00f4hn Sm\u00edth") == "john smith"
    assert fold_text("Jo\u0302hn") == "john"           # decomposed input too


def test_enclosing_and_spacing_marks_are_stripped():
    assert fold_text("A\u20dd") == "a"                 # COMBINING ENCLOSING CIRCLE


def test_uppercase_cyrillic_confusables_fold():
    """The map was applied before casefold, so capitals needed their own entries."""
    assert fold_text("\u0405mith") == "smith"          # CYRILLIC CAPITAL LETTER DZE
    assert fold_text("\u0410\u0412\u0421") == "abc"    # Cyrillic А В С


def test_plain_ascii_is_untouched():
    assert fold_text("John Smith") == "john smith"
    assert fold_text("  spaced   out  ") == "spaced out"
    assert fold_text("") == ""


def test_folding_is_idempotent():
    once = fold_text(SMALL_CAPS_JOHN_SMITH)
    assert fold_text(once) == once


def test_folding_does_not_collapse_distinct_ascii_names():
    """Over-aggressive folding would create false positives of its own."""
    assert fold_text("Alice") != fold_text("Bob")

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

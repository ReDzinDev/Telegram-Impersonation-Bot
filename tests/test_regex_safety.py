"""
Admin-supplied regex keywords must not be able to freeze the bot (F-4).

`/addkeyword r:<pattern>` validated only that the pattern COMPILES, never how
long it could take to run. Matching then used re.search on attacker-controlled
text (a bio, up to ~140 chars) with no bound.

Two things make this worse than it first looks:

  - CPython's `re` does not release the GIL, so moving check_user into a worker
    thread (R-1) did NOT mitigate it. Measured: an 8.4s match inside
    asyncio.to_thread let exactly ONE event-loop tick through. The whole process
    stalls — both Telegram clients, every group, not just the group that owns
    the keyword.
  - patterns were recompiled per text per call, so the cost was paid repeatedly.

Containment here is a timeout, not a cleverer pattern check: static analysis
cannot catch every catastrophic regex. Insert-time rejection and the subject cap
are there to make the timeout rare, not to replace it.
"""
import time

import pytest

from src.utils import detector
from src.utils.detector import (
    MAX_REGEX_SUBJECT,
    REGEX_MATCH_BUDGET,
    REGEX_MATCH_TIMEOUT,
    check_reserved_keywords,
    describe_unsafe_regex,
)


# A pattern that is still catastrophic under the `regex` engine. Note that
# `regex` optimises away the classic (a+)+$ and (x+x+)+y, so those are useless
# for testing containment — this one is not.
CATASTROPHIC = r"(a|a)*$"
EVIL_SUBJECT = "a" * 60 + "!"


def _kw(pattern, is_regex=True):
    return [{"pattern": pattern, "is_regex": is_regex}]


# ── match-time containment ────────────────────────────────────────────────────

def test_a_catastrophic_pattern_is_bounded_not_hung():
    started = time.monotonic()
    result = check_reserved_keywords(EVIL_SUBJECT, None, None, _kw(CATASTROPHIC))
    elapsed = time.monotonic() - started
    assert elapsed < 2.0, f"took {elapsed:.1f}s — not contained"
    assert result is None, "a timed-out pattern must not count as a match"


def test_a_single_match_is_bounded_by_the_per_pattern_timeout():
    """The load-bearing bound. Everything else narrows how often it is hit."""
    assert REGEX_MATCH_TIMEOUT <= 0.5
    started = time.monotonic()
    detector._regex_hits(CATASTROPHIC, EVIL_SUBJECT,
                         time.monotonic() + REGEX_MATCH_BUDGET)
    elapsed = time.monotonic() - started
    assert elapsed < REGEX_MATCH_TIMEOUT + 0.5, f"one match took {elapsed:.2f}s"


def test_many_catastrophic_patterns_share_one_budget():
    """
    Per-pattern timeouts multiply: 20 patterns x 3 texts x the timeout would be
    seconds of stall. A whole-call budget bounds the total.
    """
    keywords = [{"pattern": CATASTROPHIC, "is_regex": True} for _ in range(20)]
    started = time.monotonic()
    check_reserved_keywords(EVIL_SUBJECT, EVIL_SUBJECT, EVIL_SUBJECT, keywords)
    elapsed = time.monotonic() - started
    assert elapsed < REGEX_MATCH_BUDGET + 1.0, (
        f"took {elapsed:.1f}s against a {REGEX_MATCH_BUDGET}s budget"
    )


def test_the_budget_is_enforced_inside_the_matcher_too():
    """
    Not just as a loop early-exit: an already-expired deadline must stop a match
    from starting at all, so the bound holds however the matcher is reached.
    """
    expired = time.monotonic() - 1.0
    started = time.monotonic()
    assert detector._regex_hits(CATASTROPHIC, EVIL_SUBJECT, expired) is False
    assert time.monotonic() - started < 0.02, "ran the match despite a spent budget"


def test_oversized_subjects_are_truncated():
    """
    ReDoS cost grows with input length, so cap what reaches the engine.

    Asserts the truncation itself rather than a wall-clock time: the per-match
    timeout would bound this test either way, which would let the cap be
    silently removed.
    """
    assert MAX_REGEX_SUBJECT <= 1024
    huge = "a" * (MAX_REGEX_SUBJECT * 4) + "needle"
    prepared = detector._subject_for_regex(huge)
    assert len(prepared) == MAX_REGEX_SUBJECT


def test_a_sane_regex_still_matches():
    assert check_reserved_keywords(
        "Official Support Team", None, None, _kw(r"official.*team")
    ) == r"official.*team"


def test_regex_is_case_insensitive_as_before():
    assert check_reserved_keywords("OFFICIAL ADMIN", None, None, _kw(r"official")) \
        == r"official"


def test_regex_matches_against_folded_text_too():
    """
    E-2 in passing: the literal-keyword path folds Unicode but the regex path
    only saw raw text, so the regex form was strictly weaker than the identical
    literal.
    """
    cyrillic_admin = "\u0430dmin"          # Cyrillic а
    assert check_reserved_keywords(cyrillic_admin, None, None, _kw(r"admin")) \
        == r"admin"


def test_invalid_regex_is_skipped_not_raised():
    assert check_reserved_keywords("anything", None, None, _kw(r"(unclosed")) is None


def test_patterns_are_compiled_once_and_reused():
    detector._compiled_regex.cache_clear()
    for _ in range(25):
        check_reserved_keywords("some name", None, None, _kw(r"admin|support|mod"))
    info = detector._compiled_regex.cache_info()
    assert info.misses == 1, f"recompiled {info.misses} times"
    assert info.hits >= 20


# ── insert-time rejection ─────────────────────────────────────────────────────

@pytest.mark.parametrize("pattern", [
    r"(a+)+$",
    r"(a|a)*$",
    r"(x+x+)+y",
    r"(\w+\s?)*$",
    r"([a-z]*)*",
])
def test_nested_quantifiers_are_rejected_at_insert(pattern):
    reason = describe_unsafe_regex(pattern)
    assert reason is not None, f"{pattern!r} accepted"
    assert "quantifier" in reason.lower() or "nested" in reason.lower()


@pytest.mark.parametrize("pattern", [
    r"official.*ceo",
    r"admin|support|mod",
    r"^support$",
    r"a{2,3}",
    r"[A-Z]{3}\d+",
])
def test_reasonable_patterns_are_accepted(pattern):
    assert describe_unsafe_regex(pattern) is None, f"{pattern!r} wrongly rejected"


def test_unparseable_patterns_are_reported():
    assert describe_unsafe_regex(r"(unclosed") is not None


def test_absurdly_long_patterns_are_rejected():
    assert describe_unsafe_regex("a" * 600) is not None


# ── /addkeyword argument splitting ────────────────────────────────────────────

from src.handlers.commands import _split_keyword_entries


def test_bounded_repeat_is_not_split_on_its_comma():
    """
    `r:a{2,3}` used to become `r:a{2` and `3}`. re.compile("a{2") SUCCEEDS —
    it is the literal "a{2" — so both fragments were stored as working keywords
    and the admin was told it worked.
    """
    assert _split_keyword_entries("r:a{2,3}") == ["r:a{2,3}"]


def test_literal_keywords_still_split_on_commas():
    assert _split_keyword_entries("admin, support, *mod*") == [
        "admin", "support", "*mod*"
    ]


def test_literals_before_a_regex_are_kept_separate():
    assert _split_keyword_entries("admin, support, r:official.*(ceo|cto)") == [
        "admin", "support", "r:official.*(ceo|cto)"
    ]


def test_a_regex_containing_commas_survives_intact():
    assert _split_keyword_entries("r:^(admin|mod){1,2}$") == ["r:^(admin|mod){1,2}$"]


def test_empty_input_yields_nothing():
    assert _split_keyword_entries("") == []
    assert _split_keyword_entries("  ,  , ") == []


def test_add_keyword_actually_uses_the_safe_splitter():
    """
    A source-level guard on the CALL SITE. Testing _split_keyword_entries alone
    passes even if add_keyword goes back to raw.split(","), which is exactly the
    regression that shredded bounded repeats.
    """
    import re as _re
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent
              / "src" / "handlers" / "commands.py").read_text(encoding="utf-8")
    body = source[source.index("async def add_keyword"):source.index("async def remove_keyword")]
    assert "_split_keyword_entries(raw)" in body
    naive = _re.search(r'raw\.split\(\s*["\']\,["\']', body)
    assert naive is None, "add_keyword splits the raw argument on commas again"


def test_add_keyword_validates_patterns_before_storing():
    """The insert-time layer must be wired in, not just defined."""
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent
              / "src" / "handlers" / "commands.py").read_text(encoding="utf-8")
    body = source[source.index("async def add_keyword"):source.index("async def remove_keyword")]
    assert "describe_unsafe_regex(pattern)" in body

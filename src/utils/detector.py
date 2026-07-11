
import re
import unicodedata
from rapidfuzz import fuzz, process
from typing import List, Tuple, Optional
from confusable_homoglyphs import confusables

# Confusable (homoglyph) folding: map the most common non-Latin lookalikes to
# their Latin prototype so a name written entirely in Cyrillic/Greek letters
# (e.g. "Јоhn Ѕmіth", all-Cyrillic) folds to "john smith" and matches. This
# closes the whole-script confusable gap that is_dangerous() misses (it only
# flags *mixed*-script strings), and it works for names AND keywords.
_CONFUSABLE_MAP = str.maketrans({
    # Cyrillic → Latin
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x",
    "к": "k", "м": "m", "т": "t", "в": "b", "н": "h", "і": "i", "ј": "j",
    "ѕ": "s", "ԁ": "d", "ԛ": "q", "ѡ": "w", "ᴦ": "r", "ʏ": "y", "ɡ": "g",
    "З": "3", "Ч": "4",
    "А": "a", "Е": "e", "О": "o", "Р": "p", "С": "c", "У": "y", "Х": "x",
    "К": "k", "М": "m", "Т": "t", "В": "b", "Н": "h", "І": "i", "Ј": "j",
    # Greek → Latin
    "α": "a", "ο": "o", "ρ": "p", "ε": "e", "ι": "i", "κ": "k", "ν": "v",
    "τ": "t", "υ": "u", "χ": "x", "Α": "a", "Β": "b", "Ε": "e", "Ζ": "z",
    "Η": "h", "Ι": "i", "Κ": "k", "Μ": "m", "Ν": "n", "Ο": "o", "Ρ": "p",
    "Τ": "t", "Υ": "y", "Χ": "x",
})


def fold_text(s: str) -> str:
    """
    Aggressively normalize a string for comparison:
      - NFKC (folds fullwidth/math/stylized unicode to plain ASCII forms)
      - strip combining marks (Mn) and format chars (Cf: zero-width, RTL/LRM)
      - map common Cyrillic/Greek confusables to Latin
      - casefold (unicode-aware lowercasing) + collapse whitespace

    Used so that ALL-CAPS, ｆｕｌｌｗｉｄｔｈ, z̷a̷l̷g̷o̷, zero-width-laced, and
    whole-script-confusable variants of a name all compare equal to the plain
    form. Returns "" for falsy input.
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) not in ("Mn", "Cf"))
    s = s.translate(_CONFUSABLE_MAP)
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"\s+", " ", s.casefold()).strip()


def _name_score(a: str, b: str) -> int:
    """Best of token_sort (order-insensitive) and token_set (subset-tolerant,
    so 'John Smith | Support' still matches 'John Smith')."""
    return int(max(fuzz.token_sort_ratio(a, b), fuzz.token_set_ratio(a, b)))


# Leetspeak / lookalike character folding for username comparison.
# Scammers swap visually-similar characters (j0hn vs john, mike_admin vs
# mikeadmin) to dodge exact and even fuzzy matching. We score BOTH the raw
# lowercase form and a folded form, then keep the higher score — so an
# obfuscated handle scores as high as the clean one it imitates.
_LEET_MAP = str.maketrans({
    "0": "o", "1": "i", "3": "e", "4": "a", "5": "s",
    "7": "t", "8": "b", "9": "g", "@": "a", "$": "s",
})


def _normalize_handle(s: str) -> str:
    """Lowercase, drop separators (_ . - space), and fold leetspeak."""
    s = s.lower().translate(_LEET_MAP)
    return re.sub(r"[\s._\-]+", "", s)


def check_username_similarity(
    target: str, stored: List[str], threshold: int
) -> Tuple[bool, Optional[str], int]:
    if not target or not stored:
        return False, None, 0

    # Telegram usernames are case-insensitive
    target_lower = target.lower()
    stored_lower = [u.lower() for u in stored]

    # Pass 1 — raw lowercase fuzzy match
    best_val: Optional[str] = None
    best_score = 0
    raw = process.extractOne(target_lower, stored_lower, scorer=fuzz.ratio)
    if raw:
        best_val = stored[stored_lower.index(raw[0])]
        best_score = int(raw[1])

    # Pass 2 — separator-stripped + leetspeak-folded fuzzy match. Catches
    # j0hn_smith vs johnsmith that the raw pass scores too low.
    target_norm = _normalize_handle(target)
    stored_norm = [_normalize_handle(u) for u in stored]
    norm = process.extractOne(target_norm, stored_norm, scorer=fuzz.ratio)
    if norm and int(norm[1]) > best_score:
        best_score = int(norm[1])
        best_val = stored[stored_norm.index(norm[0])]

    if best_val is not None and best_score >= threshold:
        return True, best_val, best_score
    return False, None, 0


def check_name_similarity(
    target: str, stored: List[str], threshold: int
) -> Tuple[bool, Optional[str], int]:
    """
    Fuzzy-match a display name against stored whitelist names.

    Scores each candidate on BOTH the raw string (token_sort_ratio, preserves
    the original conservative behaviour) and a fold_text() skeleton
    (token_sort/token_set) that neutralizes case, fullwidth/stylized unicode,
    zero-width/RTL chars, and whole-script Cyrillic/Greek confusables. Keeps
    the higher score, so "JOHN SMITH", "Ｊｏｈｎ Ｓｍｉｔｈ", and all-Cyrillic
    "Јоhn Ѕmіth" now match "John Smith" instead of scoring ~0.
    """
    if not target or not stored:
        return False, None, 0

    t_fold = fold_text(target)
    best_val: Optional[str] = None
    best_score = 0
    for original in stored:
        raw = fuzz.token_sort_ratio(target, original)
        fold = _name_score(t_fold, fold_text(original)) if t_fold else 0
        score = max(raw, fold)
        if score > best_score:
            best_score = int(score)
            best_val = original

    if best_val is not None and best_score >= threshold:
        return True, best_val, best_score
    return False, None, 0


def check_homoglyph_danger(text: str) -> bool:
    if not text:
        return False
    return confusables.is_dangerous(text)


def _match_wildcard_pattern(pattern: str, text: str) -> bool:
    """
    Plain (non-regex) pattern matcher with optional `*` wildcards.

      foo      → substring match (`foo` appears anywhere)
      foo*     → text starts with `foo`
      *foo     → text ends with `foo`
      *foo*    → substring match (explicit form, same as bare `foo`)

    Wildcards are only meaningful at the start/end of the pattern;
    an interior `*` is treated literally to keep the surface small.
    All matching is case-insensitive.
    """
    if not pattern:
        return False
    # fold_text on both sides so a keyword like "admin" also catches "аdmin"
    # (Cyrillic а), "ａｄｍｉｎ" (fullwidth), and zero-width-laced variants —
    # the highest-severity check was previously the easiest to evade.
    p = fold_text(pattern)
    t = fold_text(text)
    starts_wild = p.startswith("*")
    ends_wild   = p.endswith("*")
    core = p.strip("*")
    if not core:
        return False  # pattern was just "*" / "**" — ignore

    if starts_wild and ends_wild:
        return core in t
    if ends_wild:
        return t.startswith(core)
    if starts_wild:
        return t.endswith(core)
    return core in t  # bare keyword = substring (unchanged behavior)


def check_reserved_keywords(
    full_name: str,
    username: Optional[str],
    bio: Optional[str],
    keywords: list[dict],
) -> Optional[str]:
    """
    Returns the first matched pattern if any reserved keyword/regex hits
    the user's name, username, or bio. Returns None if no match.

    Plain patterns support `*` wildcards at the start/end — see
    _match_wildcard_pattern for the rules.
    """
    if not keywords:
        return None
    texts = [t for t in [full_name, username, bio] if t]
    for kw in keywords:
        pattern = kw["pattern"]
        for text in texts:
            if kw["is_regex"]:
                try:
                    if re.search(pattern, text, re.IGNORECASE):
                        return pattern
                except re.error:
                    pass  # bad regex — skip silently
            else:
                if _match_wildcard_pattern(pattern, text):
                    return pattern
    return None

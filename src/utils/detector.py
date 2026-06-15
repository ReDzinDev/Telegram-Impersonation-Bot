
import re
from rapidfuzz import fuzz, process
from typing import List, Tuple, Optional
from confusable_homoglyphs import confusables

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
    if not target or not stored:
        return False, None, 0

    result = process.extractOne(target, stored, scorer=fuzz.token_sort_ratio)
    if result and result[1] >= threshold:
        return True, result[0], int(result[1])
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
    p = pattern.lower()
    t = text.lower()
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

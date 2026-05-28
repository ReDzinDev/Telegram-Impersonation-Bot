
import re
from rapidfuzz import fuzz, process
from typing import List, Tuple, Optional
from confusable_homoglyphs import confusables


def check_username_similarity(
    target: str, stored: List[str], threshold: int
) -> Tuple[bool, Optional[str], int]:
    if not target or not stored:
        return False, None, 0

    # Normalize: Telegram usernames are case-insensitive
    target_lower = target.lower()
    stored_lower = [u.lower() for u in stored]

    result = process.extractOne(target_lower, stored_lower, scorer=fuzz.ratio)
    if result and result[1] >= threshold:
        # Return the original stored value (not lowercased)
        original = stored[stored_lower.index(result[0])]
        return True, original, int(result[1])
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

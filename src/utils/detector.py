
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

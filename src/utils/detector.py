
import logging
import re
import unicodedata
from rapidfuzz import fuzz, process
from typing import List, Tuple, Optional
from confusable_homoglyphs import confusables

logger = logging.getLogger(__name__)

_ASCII_ALNUM = frozenset("abcdefghijklmnopqrstuvwxyz"
                         "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")

# Latin lookalikes whose Unicode NAME states the letter they imitate, e.g.
# "LATIN LETTER SMALL CAPITAL J" -> j. These have no NFKC/NFKD decomposition, so
# nothing but an explicit mapping catches them.
_LATIN_NAME_RE = re.compile(
    r"^LATIN (?:LETTER SMALL CAPITAL|SMALL LETTER|CAPITAL LETTER|LETTER) ([A-Z])\b"
)

# Ranges worth scanning by name: Latin Extended-B, IPA Extensions, Spacing
# Modifiers and Phonetic Extensions — where the small-capital and hooked forms
# live.
_NAME_SCAN_RANGES = ((0x0180, 0x0250), (0x0250, 0x02B0), (0x1D00, 0x1D80))


def _build_confusable_map() -> dict[int, str]:
    """
    Derive a lookalike → ASCII folding table from Unicode's own confusables
    data, rather than maintaining one by hand.

    The previous 60-entry hand-rolled map left the most common Telegram
    stylisation completely unfolded: Unicode small-capital and phonetic letters
    (U+1D00-1D2B, U+0250-02AF) have no NFKC decomposition and only three of them
    were listed — so "ᴀᴅᴍɪɴ" did not match the keyword "admin", and that stage is
    hard-wired to the ban band. Cherokee and Latin-hook lookalikes failed the
    same way.

    Two sources, in order of preference:

    1. The confusables table shipped with confusable_homoglyphs (the Unicode
       confusables.txt data, ~9.6k entries). It is a confusability *graph*, not a
       skeleton map, so we walk it in both directions and keep any edge where one
       end is ASCII. That is what turns Cherokee DU into "s".
    2. A pass over the Latin-lookalike ranges deriving the target letter from the
       character's Unicode name, which covers small capitals the graph misses.

    Costs ~20ms once at import. Falls back to ASCII-only folding if the data
    cannot be read, so a packaging problem degrades detection rather than
    preventing startup.
    """
    mapping: dict[int, str] = {}

    def offer(src: str, dst: str) -> None:
        if len(src) != 1 or src in _ASCII_ALNUM or dst not in _ASCII_ALNUM:
            return
        mapping.setdefault(ord(src), dst.lower())
        # fold_text casefolds BEFORE translating, so the casefolded form of the
        # lookalike needs an entry too or capitals slip through (Ѕ U+0405).
        #
        # But the casefolded form must itself be non-ASCII. Some non-ASCII
        # characters casefold straight ONTO an ASCII letter — U+017F LATIN SMALL
        # LETTER LONG S casefolds to "s" and is confusable with "f" — so without
        # this guard we would map plain "s" to "f" and silently break every name
        # comparison in the bot.
        folded = src.casefold()
        if len(folded) == 1 and folded not in _ASCII_ALNUM:
            mapping.setdefault(ord(folded), dst.lower())

    try:
        import json
        from pathlib import Path

        import confusable_homoglyphs

        table_path = Path(confusable_homoglyphs.__file__).parent / "confusables.json"
        table = json.loads(table_path.read_text(encoding="utf-8"))
        for key, entries in table.items():
            for entry in entries:
                other = entry.get("c", "")
                offer(key, other)
                offer(other, key)
    except Exception:  # pragma: no cover - packaging problem, not a logic path
        logger.warning(
            "Could not load the Unicode confusables table; homoglyph folding "
            "will be weaker. Stylized-alphabet impersonation may be missed."
        )

    for start, stop in _NAME_SCAN_RANGES:
        for codepoint in range(start, stop):
            if codepoint in mapping:
                continue
            try:
                name = unicodedata.name(chr(codepoint))
            except ValueError:
                continue
            match = _LATIN_NAME_RE.match(name)
            if match:
                mapping[codepoint] = match.group(1).lower()

    # Deliberate extras the confusables data does not assert, kept from the
    # original hand-rolled map: leet-style digit substitutions seen in the wild.
    mapping.setdefault(ord("З"), "3")
    mapping.setdefault(ord("Ч"), "4")
    return mapping


_CONFUSABLE_MAP = _build_confusable_map()


def fold_text(s: str) -> str:
    """
    Aggressively normalize a string for comparison:
      - NFKD (decomposes accents so the marks become separate code points)
      - strip combining marks (Mn/Mc/Me) and format chars (Cf: zero-width, RTL)
      - NFKC (recompose, folding fullwidth/math/stylized forms to plain ASCII)
      - casefold (unicode-aware lowercasing)
      - map confusables to their Latin prototype
      - collapse whitespace

    Used so that ALL-CAPS, ｆｕｌｌｗｉｄｔｈ, z̷a̷l̷g̷o̷, zero-width-laced,
    small-capital, and whole-script-confusable variants of a name all compare
    equal to the plain form. Returns "" for falsy input.

    The step ORDER is load-bearing, and two orderings were wrong before:

    NFKD must come first. Normalising to NFKC (composed) and *then* filtering
    category Mn cannot remove any accent that has a precomposed form — by then
    "ô" is one code point, not "o" plus a combining circumflex — so "Jôhn Smíth"
    kept its accents and scored 80 against "John Smith". Categories Mc and Me
    were never filtered at all.

    casefold must precede the confusable map. Translating first meant the map
    needed both cases of every entry, and it had the lowercase Cyrillic forms
    without their capitals — so "Ѕmith" (U+0405) folded to "ѕmith" and matched
    nothing.
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(
        ch for ch in s
        if unicodedata.category(ch) not in ("Mn", "Mc", "Me", "Cf")
    )
    s = unicodedata.normalize("NFKC", s)
    s = s.casefold()
    s = s.translate(_CONFUSABLE_MAP)
    return re.sub(r"\s+", " ", s).strip()


# Points deducted per unmatched token when one name's tokens are a strict
# subset of the other's. token_set_ratio returns a flat 100 for that shape
# regardless of how much extra text surrounds the borrowed name, which made
# "John Smith Fan Club" score identically to "John Smith" and land in the ban
# band at full confidence.
#
# 6 points per extra token produces a deliberate gradient against the default
# bands (flag at 85, ban at 90):
#
#   "John Smith"          0 extra -> 100  ban    (exact)
#   "John Smith Support"  1 extra ->  94  ban    (impersonation-shaped)
#   "John Smith Fan Club" 2 extra ->  88  alert  (human reviews)
#   "I love John Smith.." 3 extra ->  82  ignored
#
# Two extra tokens is where genuine impersonation ("John Smith | Support") and
# innocent reference ("John Smith Fan Club") become structurally identical, so
# that row alerts rather than acting — the safe outcome for both readings.
# Retune this constant to move the boundary.
_SUPERSET_TOKEN_PENALTY = 6


def _name_score(a: str, b: str) -> int:
    """
    Best of token_sort (order-insensitive) and token_set (subset-tolerant, so
    'John Smith | Support' still matches 'John Smith') — with the token_set
    score scaled down by how many tokens went unmatched, so a name that merely
    *contains* a whitelisted name cannot claim full confidence.
    """
    sort_score = fuzz.token_sort_ratio(a, b)
    set_score = fuzz.token_set_ratio(a, b)

    tokens_a, tokens_b = set(a.split()), set(b.split())
    if tokens_a and tokens_b and (tokens_a < tokens_b or tokens_b < tokens_a):
        # Strict subset: token_set_ratio is saturated at 100 by construction.
        # Charge for the surrounding text, but never score worse than the
        # plain token_sort comparison would have on its own.
        extra_tokens = abs(len(tokens_b) - len(tokens_a))
        set_score = max(
            sort_score, set_score - _SUPERSET_TOKEN_PENALTY * extra_tokens
        )

    return int(max(sort_score, set_score))


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

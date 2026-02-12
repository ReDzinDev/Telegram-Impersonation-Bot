
from fuzzywuzzy import fuzz, process
from typing import List, Tuple, Dict, Optional
from confusable_homoglyphs import confusables

def check_username_similarity(target_username: str, stored_usernames: List[str], threshold: int) -> Tuple[bool, Optional[str], int]:
    """
    Checks if a username is similar to any in the stored list.
    Returns: (Match Found?, Matched Username, Similarity Score)
    """
    if not target_username or not stored_usernames:
        return False, None, 0
    
    # Extract match with highest score
    match, score = process.extractOne(target_username, stored_usernames, scorer=fuzz.ratio)
    
    if score >= threshold:
        return True, match, score
    return False, None, 0

def check_name_similarity(target_name: str, stored_names: List[str], threshold: int) -> Tuple[bool, Optional[str], int]:
    """
    Checks if a full name (first + last) is similar to any in the stored list.
    Returns: (Match Found?, Matched Name, Similarity Score)
    """
    if not target_name or not stored_names:
        return False, None, 0
        
    match, score = process.extractOne(target_name, stored_names, scorer=fuzz.token_sort_ratio)
    
    if score >= threshold:
        return True, match, score
    return False, None, 0

def check_homoglyph_danger(text: str) -> bool:
    """
    Checks if the text contains dangerous mixed-script homoglyphs.
    """
    if not text:
        return False
    return confusables.is_dangerous(text)

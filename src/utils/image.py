
import logging
import imagehash
from PIL import Image
from io import BytesIO
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def compute_pfp_hash_bytes(image_data: bytes) -> Optional[str]:
    if not image_data:
        return None
    try:
        img = Image.open(BytesIO(image_data))
        return str(imagehash.phash(img))
    except Exception as e:
        logger.error(f"Error computing PFP hash: {e}")
        return None


def check_pfp_similarity(
    target_hex: str, stored_hashes: list[str], threshold: int = 10
) -> Tuple[bool, Optional[str], int]:
    """
    Returns (match_found, matched_hash, hamming_distance).
    Lower distance = more similar. Match when distance <= threshold.
    """
    if not target_hex:
        return False, None, 100

    try:
        target_hash = imagehash.hex_to_hash(target_hex)
    except ValueError:
        return False, None, 100

    best_match: Optional[str] = None
    min_dist = 100

    for stored_hex in stored_hashes:
        if not stored_hex:
            continue
        try:
            dist = target_hash - imagehash.hex_to_hash(stored_hex)
            if dist < min_dist:
                min_dist = dist
                best_match = stored_hex
        except ValueError:
            continue

    if min_dist <= threshold:
        return True, best_match, min_dist
    return False, None, min_dist

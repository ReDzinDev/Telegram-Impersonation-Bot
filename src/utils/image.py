
import logging
import imagehash
from PIL import Image
from io import BytesIO
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def compute_pfp_hash_bytes(image_data: bytes) -> Optional[str]:
    """
    Perceptual-hash a profile photo. Returns a hex phash string, or None if
    the bytes can't be hashed.

    Robust to animated / Premium video avatars: Telegram usually hands us a
    static JPEG preview, but for multi-frame formats (animated GIF/WEBP/APNG)
    we hash the FIRST frame so the result is deterministic. Frames are
    converted to RGB first — phash on palette ('P') or alpha ('RGBA') images
    can vary by decoder. A genuinely un-openable blob (true video container)
    returns None and is logged at debug, not error, so video-avatar users
    don't spam the logs.
    """
    if not image_data:
        return None
    try:
        img = Image.open(BytesIO(image_data))
        # Multi-frame image → pin to the first frame for a stable hash
        if getattr(img, "n_frames", 1) > 1:
            try:
                img.seek(0)
            except Exception:
                pass
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        return str(imagehash.phash(img))
    except Exception as e:
        logger.debug(f"Could not compute PFP hash (likely an animated/video avatar): {e}")
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

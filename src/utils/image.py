
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
        img = _load_image(image_data)
        return str(imagehash.phash(img))
    except Exception as e:
        logger.debug(f"Could not compute PFP hash (likely an animated/video avatar): {e}")
        return None


def _load_image(image_data: bytes) -> Image.Image:
    """Open bytes into a first-frame RGB/L PIL image (shared by the hashers)."""
    img = Image.open(BytesIO(image_data))
    if getattr(img, "n_frames", 1) > 1:
        try:
            img.seek(0)
        except Exception:
            pass
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    return img


def compute_pfp_hash_variants_bytes(image_data: bytes) -> list[str]:
    """
    Return perceptual hashes for the image AND its horizontal mirror.

    phash has no flip invariance, so mirroring an avatar is the cheapest way to
    dodge a hash match. Hashing both the original and the flipped image at
    *check* time (the suspect side) lets a mirrored clone still match the
    admin's single stored hash. Returns [] if the bytes can't be hashed.
    """
    if not image_data:
        return []
    try:
        img = _load_image(image_data)
    except Exception as e:
        logger.debug(f"Could not compute PFP hash variants: {e}")
        return []
    out = [str(imagehash.phash(img))]
    try:
        flipped = img.transpose(Image.FLIP_LEFT_RIGHT)
        out.append(str(imagehash.phash(flipped)))
    except Exception:
        pass
    return out


def check_pfp_similarity(
    target_hex, stored_hashes: list[str], threshold: int = 10
) -> Tuple[bool, Optional[str], int]:
    """
    Returns (match_found, matched_hash, hamming_distance).
    Lower distance = more similar. Match when distance <= threshold.

    target_hex may be a single hex string or a list of them (e.g. the original
    plus its mirror from compute_pfp_hash_variants_bytes); the best (smallest)
    distance across all candidates is used.
    """
    candidates = [target_hex] if isinstance(target_hex, str) else list(target_hex or [])
    target_hashes = []
    for hx in candidates:
        if not hx:
            continue
        try:
            target_hashes.append(imagehash.hex_to_hash(hx))
        except ValueError:
            continue
    if not target_hashes:
        return False, None, 100

    best_match: Optional[str] = None
    min_dist = 100

    for stored_hex in stored_hashes:
        if not stored_hex:
            continue
        try:
            stored_hash = imagehash.hex_to_hash(stored_hex)
        except ValueError:
            continue
        for th in target_hashes:
            dist = th - stored_hash
            if dist < min_dist:
                min_dist = dist
                best_match = stored_hex

    if min_dist <= threshold:
        return True, best_match, min_dist
    return False, None, min_dist

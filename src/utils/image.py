
import logging
import imagehash
from PIL import Image, ImageStat
from io import BytesIO
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# ── Degenerate-image rejection ────────────────────────────────────────────────
#
# phash keeps the DCT coefficients above the median. An image with no
# high-frequency detail has almost no coefficients above it, so nearly every bit
# collapses to the same value and EVERY such image produces the same hash:
# solid red, solid blue, solid white and a linear gradient all hash to
# 8000000000000000. Comparing two of those reports distance 0 — a perfect match
# between unrelated users.
#
# That is a false-ban path, not just noise: ban_and_log treats match_type "pfp"
# as full confidence, so the score bands cannot soften it. So we refuse to
# describe an image phash cannot describe, and the photo stage is skipped
# instead (callers already guard on an empty hash / empty variant list).
#
# Popcount is the load-bearing signal, not pixel variance — the linear gradient
# has a grayscale stddev of ~74 and is still degenerate. Measured margins:
# a photo and a cartoon avatar sit at popcount 32, a hard two-tone split at 3,
# bold stripes at 4. A cutoff of 3 clears all of them.
_MIN_HASH_POPCOUNT = 3
_MAX_HASH_POPCOUNT = 61   # an all-ones hash is equally uninformative

# Backstop for images that are visually flat but still yield a mid-range
# popcount (a tiny mark on an otherwise empty field). A real avatar essentially
# never falls below this; the structured test fixture measures ~57.
_MIN_PIXEL_STDDEV = 2.0


def _describable(img: Image.Image) -> bool:
    """False when the image carries too little detail for phash to distinguish."""
    try:
        if ImageStat.Stat(img.convert("L")).stddev[0] < _MIN_PIXEL_STDDEV:
            return False
    except Exception:
        pass  # stat failure shouldn't block hashing; popcount still applies
    return True


def _phash_or_none(img: Image.Image) -> Optional[str]:
    """phash the image, or None if the result would be a degenerate hash."""
    if not _describable(img):
        logger.debug("Refusing to hash a near-uniform image (no phash signal).")
        return None
    h = imagehash.phash(img)
    popcount = int(h.hash.sum())
    if not (_MIN_HASH_POPCOUNT <= popcount <= _MAX_HASH_POPCOUNT):
        logger.debug(
            f"Refusing degenerate phash {h} (popcount {popcount}) — "
            "flat or smooth image, would collide with every other such image."
        )
        return None
    return str(h)


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
        return _phash_or_none(img)
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
    base = _phash_or_none(img)
    if base is None:
        # Degenerate image — its mirror is equally undescribable, so there is
        # nothing to compare and the caller must skip the photo stage.
        return []
    out = [base]
    try:
        flipped = _phash_or_none(img.transpose(Image.FLIP_LEFT_RIGHT))
        if flipped is not None:
            out.append(flipped)
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

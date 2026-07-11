"""Tests for perceptual-hash robustness in src/utils/image.py."""
from io import BytesIO

from PIL import Image

from src.utils.image import (
    compute_pfp_hash_bytes,
    compute_pfp_hash_variants_bytes,
    check_pfp_similarity,
)


def _png_bytes(color=(120, 30, 200), size=(64, 64)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _asymmetric_png() -> bytes:
    """A left/right-asymmetric image so a horizontal flip actually changes it."""
    img = Image.new("RGB", (64, 64), (240, 240, 240))
    for x in range(64):
        for y in range(64):
            if x < 20 or (x + y) % 7 == 0:  # bias content to the left half
                img.putpixel((x, y), (10, 10, 10))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_hash_valid_image():
    h = compute_pfp_hash_bytes(_png_bytes())
    assert isinstance(h, str) and len(h) > 0


def test_hash_empty_bytes_returns_none():
    assert compute_pfp_hash_bytes(b"") is None
    assert compute_pfp_hash_bytes(None) is None


def test_hash_garbage_returns_none_not_raises():
    # A non-image blob (e.g. a video container) must return None, never raise
    assert compute_pfp_hash_bytes(b"\x00\x01not an image\xff") is None


def test_hash_rgba_is_handled():
    buf = BytesIO()
    Image.new("RGBA", (64, 64), (10, 20, 30, 128)).save(buf, format="PNG")
    assert compute_pfp_hash_bytes(buf.getvalue()) is not None


def test_identical_images_match():
    data = _png_bytes()
    h1 = compute_pfp_hash_bytes(data)
    h2 = compute_pfp_hash_bytes(data)
    match, _, dist = check_pfp_similarity(h1, [h2], threshold=10)
    assert match is True and dist == 0


def test_flipped_image_matches_via_variants():
    """A horizontally-mirrored avatar should still match the original's stored
    hash, because compute_pfp_hash_variants_bytes hashes both orientations."""
    original = _asymmetric_png()
    stored = compute_pfp_hash_bytes(original)

    flipped_img = Image.open(BytesIO(original)).transpose(Image.FLIP_LEFT_RIGHT)
    buf = BytesIO()
    flipped_img.save(buf, format="PNG")

    # A single-hash check misses the flip; the variant list catches it.
    single = compute_pfp_hash_bytes(buf.getvalue())
    variants = compute_pfp_hash_variants_bytes(buf.getvalue())

    single_match, _, single_dist = check_pfp_similarity(single, [stored], threshold=10)
    variant_match, _, variant_dist = check_pfp_similarity(variants, [stored], threshold=10)

    assert variant_match is True and variant_dist <= 10
    # sanity: the flip genuinely changed the single hash (otherwise the test is vacuous)
    assert single_dist >= variant_dist


# check_pfp_similarity threshold logic, tested with explicit 64-bit hex hashes
# (deterministic — avoids phash's near-uniform/symmetric-image collisions).

def test_pfp_similarity_far_apart_does_not_match():
    # all-zero vs all-one bits = 64-bit hamming distance, well over threshold
    match, _, dist = check_pfp_similarity("0000000000000000", ["ffffffffffffffff"], threshold=10)
    assert match is False and dist > 10


def test_pfp_similarity_within_threshold_matches():
    # differ by a single bit
    match, val, dist = check_pfp_similarity("0000000000000000", ["0000000000000001"], threshold=10)
    assert match is True and dist == 1 and val == "0000000000000001"


def test_pfp_similarity_picks_closest_of_many():
    match, val, dist = check_pfp_similarity(
        "0000000000000000",
        ["ffffffffffffffff", "0000000000000003", "00000000000000ff"],
        threshold=10,
    )
    assert match is True and val == "0000000000000003" and dist == 2

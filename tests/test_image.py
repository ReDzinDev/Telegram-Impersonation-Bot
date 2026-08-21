"""Tests for perceptual-hash robustness in src/utils/image.py."""
from io import BytesIO

from PIL import Image

from src.utils.image import (
    compute_pfp_hash_bytes,
    compute_pfp_hash_variants_bytes,
    check_pfp_similarity,
)


def _flat_png(color=(120, 30, 200), size=(64, 64)) -> bytes:
    """A solid-colour image. phash cannot describe this — see the degenerate-
    hash tests below — so it is only used where that is the point."""
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _structured_png(seed=(200, 60, 60), size=(64, 64)) -> bytes:
    """A real-looking avatar: high-frequency detail phash can actually encode."""
    img = Image.new("RGB", size, (30, 60, 120))
    for x in range(size[0]):
        for y in range(size[1]):
            if (x // 7 + y // 5) % 2 == 0:
                img.putpixel((x, y), seed)
    buf = BytesIO()
    img.save(buf, format="PNG")
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
    h = compute_pfp_hash_bytes(_structured_png())
    assert isinstance(h, str) and len(h) > 0


def test_hash_empty_bytes_returns_none():
    assert compute_pfp_hash_bytes(b"") is None
    assert compute_pfp_hash_bytes(None) is None


def test_hash_garbage_returns_none_not_raises():
    # A non-image blob (e.g. a video container) must return None, never raise
    assert compute_pfp_hash_bytes(b"\x00\x01not an image\xff") is None


def test_hash_rgba_is_handled():
    img = Image.open(BytesIO(_structured_png())).convert("RGBA")
    buf = BytesIO()
    img.save(buf, format="PNG")
    assert compute_pfp_hash_bytes(buf.getvalue()) is not None


def test_identical_images_match():
    data = _structured_png()
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


# ── degenerate hashes (F-1) ───────────────────────────────────────────────────
#
# phash works by keeping the DCT coefficients above the median. An image with no
# high-frequency detail — a solid colour, a smooth gradient — has essentially no
# coefficients above the median, so nearly every bit lands on the same value and
# every such image collapses to the SAME hash. Measured: solid red, solid blue,
# solid white and a linear gradient all produce 8000000000000000.
#
# That matters because the photo stage is treated as full confidence by
# ban_and_log, so two unrelated users with plain avatars looked like proof of
# impersonation. The hashers must refuse to describe an image phash cannot
# describe. Structured images are unaffected (popcount 32 for a photo, 3-4 even
# for a two-tone split), so the popcount cutoff has real margin.


def _gradient_png() -> bytes:
    buf = BytesIO()
    Image.linear_gradient("L").resize((64, 64)).convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def test_solid_colour_avatar_is_not_hashable():
    assert compute_pfp_hash_bytes(_flat_png((255, 0, 0))) is None
    assert compute_pfp_hash_bytes(_flat_png((0, 0, 255))) is None
    assert compute_pfp_hash_bytes(_flat_png((255, 255, 255))) is None


def test_smooth_gradient_avatar_is_not_hashable():
    """High pixel variance, still no phash signal — variance alone can't catch this."""
    assert compute_pfp_hash_bytes(_gradient_png()) is None


def test_structured_avatar_is_still_hashable():
    assert compute_pfp_hash_bytes(_structured_png()) is not None


def test_flat_avatar_yields_no_hash_variants():
    assert compute_pfp_hash_variants_bytes(_flat_png((10, 200, 10))) == []


def test_structured_avatar_still_yields_variants():
    assert len(compute_pfp_hash_variants_bytes(_structured_png())) == 2


def test_two_unrelated_flat_avatars_cannot_be_compared_at_all():
    """The end-to-end false-ban path: distinct plain avatars must not match."""
    admin_stored = compute_pfp_hash_bytes(_flat_png((255, 255, 255)))
    suspect = compute_pfp_hash_variants_bytes(_flat_png((0, 0, 255)))
    assert admin_stored is None
    assert suspect == []
    # With nothing to compare, the photo stage cannot produce a match.
    match, _, _ = check_pfp_similarity(suspect, [h for h in [admin_stored] if h])
    assert match is False


def _almost_flat_png() -> bytes:
    """
    Visually flat but with a single dark pixel. This slips past the popcount
    check (measured popcount 32) while carrying no distinguishing detail, so
    the pixel-variance backstop is the only thing that rejects it. Guards
    against that backstop being removed as redundant.
    """
    img = Image.new("RGB", (64, 64), (128, 128, 128))
    img.putpixel((5, 5), (0, 0, 0))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_visually_flat_avatar_with_a_single_mark_is_not_hashable():
    assert compute_pfp_hash_bytes(_almost_flat_png()) is None

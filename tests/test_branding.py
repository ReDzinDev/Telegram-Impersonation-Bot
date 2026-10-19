"""
The BotFather profile assets, pinned.

Two failure modes are worth a test. The first is mechanical: BotFather silently
rejects an over-long name/about/description, and the only feedback is a red
error in a chat window, long after the copy was written. The second is drift:
`PRIVACY.md` states retention windows as concrete day counts, and those numbers
are function defaults in `db.purge_old_records`. If someone tunes a default, the
published privacy policy becomes a false statement about what the operator does
with personal data. That is a worse kind of stale doc than a wrong command list.
"""
import inspect
import struct
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BRANDING = ROOT / "branding"

# BotFather's documented limits.
LIMITS = {
    "botfather_name.txt": 64,
    "botfather_about.txt": 120,
    "botfather_description.txt": 512,
}


def _copy(name: str) -> str:
    path = BRANDING / name
    assert path.exists(), f"missing branding asset: {path}"
    # BotFather counts the text it receives; a trailing newline from the editor
    # is not part of it.
    return path.read_text(encoding="utf-8").strip()


@pytest.mark.parametrize("name,limit", sorted(LIMITS.items()))
def test_copy_fits_botfather_limit(name, limit):
    text = _copy(name)
    assert text, f"{name} is empty"
    assert len(text) <= limit, (
        f"{name} is {len(text)} chars, over BotFather's {limit}-char limit"
    )


def test_name_is_a_single_line():
    assert "\n" not in _copy("botfather_name.txt")


def test_description_picture_is_640x360_png():
    path = BRANDING / "description_picture.png"
    assert path.exists(), f"missing {path}"
    raw = path.read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    # IHDR is always the first chunk: 8-byte signature, 4-byte length,
    # 4-byte type, then width and height as big-endian uint32.
    width, height = struct.unpack(">II", raw[16:24])
    assert (width, height) == (640, 360), (
        f"description picture is {width}x{height}; Telegram wants 640x360"
    )


# ── privacy policy ────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def privacy() -> str:
    path = ROOT / "PRIVACY.md"
    assert path.exists(), "PRIVACY.md is missing; it is linked from the bot profile"
    return path.read_text(encoding="utf-8")


def test_privacy_policy_states_the_real_retention_windows(privacy):
    """Every retention default in purge_old_records must appear in the policy."""
    from src.db import purge_old_records

    defaults = {
        name: param.default
        for name, param in inspect.signature(purge_old_records).parameters.items()
        if isinstance(param.default, int)
    }
    assert defaults, "purge_old_records has no int defaults; test needs updating"

    for name, days in defaults.items():
        assert f"{days} days" in privacy, (
            f"purge_old_records({name}={days}) but PRIVACY.md never says "
            f"'{days} days' — the published policy no longer matches the code"
        )


def test_privacy_policy_does_not_claim_message_content_is_unread(privacy):
    """
    The bot is a group admin, so Telegram delivers it every group message. The
    honest claim is that it never inspects or stores the text — not that it
    never receives it. This guards against the wording sliding back.
    """
    assert "never inspects, stores or forwards" in privacy
    assert "never reads, stores or forwards" not in privacy, (
        "PRIVACY.md claims the bot does not read messages it demonstrably receives"
    )


def test_privacy_policy_still_has_placeholders_flagged():
    """
    A reminder, not a rule: the operator fields must be filled in before the URL
    is handed to BotFather. This test documents the requirement and will start
    failing once they are filled, at which point delete it.
    """
    text = (ROOT / "PRIVACY.md").read_text(encoding="utf-8")
    if "[YOUR NAME OR ORGANISATION]" not in text:
        pytest.skip("operator details filled in; this reminder can be deleted")
    pytest.skip("PRIVACY.md still has operator placeholders — fill before publishing")

"""
Every advertised command must actually be registered (B-3).

/importwhitelist was documented in DOCUMENTATION.md and named in the bot's own
/clearwhitelist recovery message ("reply to this file with /importwhitelist"),
but no CommandHandler for it was ever registered — so the documented recovery
path for a wiped whitelist silently did nothing.

Five more commands (/settings /setbands /setthresholds /blocklist /protect) are
registered and advertised in Telegram's menu while appearing in none of the four
docs. These tests make the advertised set and the registered set agree, so the
mechanical half of that drift can't recur.
"""
import pathlib
import re

import pytest
from telegram.ext import CommandHandler


def _registered_commands(monkeypatch, tmp_path) -> set[str]:
    monkeypatch.chdir(tmp_path)          # keep PicklePersistence off the repo file
    from src.main import build_ptb_app

    app = build_ptb_app()
    found = set()
    for group in app.handlers.values():
        for h in group:
            if isinstance(h, CommandHandler):
                found.update(h.commands)
    return found


def test_advertised_commands_are_all_registered(monkeypatch, tmp_path):
    from src.main import BOT_COMMANDS

    registered = _registered_commands(monkeypatch, tmp_path)
    advertised = {c.command for c in BOT_COMMANDS}
    missing = advertised - registered
    assert missing == set(), f"advertised in Telegram's menu but not registered: {sorted(missing)}"


def test_registered_commands_are_all_advertised(monkeypatch, tmp_path):
    """Otherwise a working command is undiscoverable."""
    from src.main import BOT_COMMANDS

    registered = _registered_commands(monkeypatch, tmp_path)
    advertised = {c.command for c in BOT_COMMANDS}
    # /start is intentionally unadvertised: Telegram surfaces it automatically.
    unadvertised = registered - advertised - {"start"}
    assert unadvertised == set(), f"registered but not advertised: {sorted(unadvertised)}"


def test_importwhitelist_is_registered(monkeypatch, tmp_path):
    """The command the bot's own recovery message tells admins to use."""
    assert "importwhitelist" in _registered_commands(monkeypatch, tmp_path)


def test_commands_named_in_bot_messages_exist(monkeypatch, tmp_path):
    """
    Scan the handler source for slash-commands the bot tells users to run, and
    verify each is registered. Catches instructions that point nowhere.
    """
    registered = _registered_commands(monkeypatch, tmp_path)
    src = pathlib.Path(__file__).resolve().parent.parent / "src"
    # Commands referenced inside user-facing string literals.
    mentioned = set()
    for path in (src / "handlers").rglob("*.py"):
        for m in re.finditer(r"/([a-z_]{3,30})\b", path.read_text(encoding="utf-8")):
            mentioned.add(m.group(1))
    # Only assert on names that look like bot commands we own.
    candidates = {c for c in mentioned if c in {
        "importwhitelist", "listwhitelist", "clearwhitelist", "import_admins",
        "setlogchannel", "setthreshold", "setthresholds", "setbands",
        "blocklist", "protect", "settings", "logs", "stats", "sweep",
        "whitelist", "unwhitelist", "setaction", "addkeyword", "removekeyword",
        "listkeywords", "ban", "unban",
    }}
    missing = candidates - registered
    assert missing == set(), f"bot text references unregistered commands: {sorted(missing)}"

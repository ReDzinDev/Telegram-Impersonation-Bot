"""
Documentation that contradicts the code is worse than no documentation (step 7).

The audit found the docs asserting, among other things, that the sweep runs every
6 hours (the default is 24), that commands work inside groups (they are DM-only),
and that the bot opens a fresh database connection per call (a pool shipped
months ago). It also found five registered commands documented nowhere and
eighteen environment variables documented nowhere — including the two that decide
whether a detection bans or merely alerts.

These tests cover the MECHANICAL half of that drift: the command list, the
environment variables, the schema tables. Those are exactly the parts that go
stale silently, and exactly the parts a test can pin. Prose still needs a human.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOC_FILES = ["README.md", "OVERVIEW.md", "DOCUMENTATION.md", "INTERNAL_DOCS.md"]


def _docs_text() -> str:
    parts = []
    for name in DOC_FILES:
        path = ROOT / name
        if path.exists():
            parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


@pytest.fixture(scope="module")
def docs() -> str:
    return _docs_text()


# ── commands ──────────────────────────────────────────────────────────────────

def test_every_advertised_command_is_documented(docs):
    from src.main import BOT_COMMANDS

    missing = [c.command for c in BOT_COMMANDS if f"/{c.command}" not in docs]
    assert missing == [], (
        "commands offered in Telegram's menu but absent from every doc: "
        f"{sorted(missing)}"
    )


# ── environment variables ─────────────────────────────────────────────────────

def _env_vars_read_by_config() -> set[str]:
    """Every variable src/config.py actually reads."""
    source = (ROOT / "src" / "config.py").read_text(encoding="utf-8")
    names = set()
    # _require("X") / _optional("X") / os.getenv("X")
    names |= set(re.findall(r'_require\(\s*"([A-Z_0-9]+)"', source))
    names |= set(re.findall(r'_optional\(\s*"([A-Z_0-9]+)"', source))
    names |= set(re.findall(r'os\.getenv\(\s*"([A-Z_0-9]+)"', source))
    # keys of the validated numeric settings table
    table = re.search(r"_NUMERIC_SETTINGS[^{]*\{(.*?)\n\}", source, re.S)
    if table:
        names |= set(re.findall(r'"([A-Z_0-9]+)":', table.group(1)))
    return names


def test_every_environment_variable_is_documented(docs):
    undocumented = sorted(v for v in _env_vars_read_by_config() if v not in docs)
    assert undocumented == [], (
        f"environment variables read by config.py but documented nowhere: "
        f"{undocumented}"
    )


def test_the_trusted_groups_variable_is_documented(docs):
    """
    Operationally the most important one: it defaults to empty, which turns
    cross-group blocklist propagation OFF. An operator who doesn't know it exists
    will assume the feature is working.
    """
    assert "BLOCKLIST_TRUSTED_GROUPS" in docs


# ── values that drifted ───────────────────────────────────────────────────────

def test_documented_sweep_interval_matches_the_code(docs):
    from src.config import SWEEP_INTERVAL_HOURS

    stale = re.findall(r"every\s+6\s*h(?:ours)?\b", docs, re.I)
    assert not stale, (
        f"docs still claim a 6-hour sweep in {len(stale)} place(s); the default "
        f"is {SWEEP_INTERVAL_HOURS}h"
    )


def test_docs_do_not_claim_commands_work_inside_groups(docs):
    """
    Commands are DM-only — _get_admin_group returns None for any non-private
    chat, silently. A troubleshooting entry telling operators to check group
    permissions sends them hunting the wrong thing.
    """
    claims = re.findall(
        r"commands? work.{0,40}(?:inside|in) a? ?group", docs, re.I
    )
    assert not claims, f"docs claim in-group command support: {claims}"


def test_docs_do_not_still_call_the_pool_a_future_improvement(docs):
    """A shipped feature listed under Limitations misleads capacity planning."""
    stale = re.findall(r"(?:fresh|new) (?:psycopg )?connection per call", docs, re.I)
    assert not stale, f"docs still describe per-call connections: {stale}"


# ── schema ────────────────────────────────────────────────────────────────────

def _tables_created_by_init_db() -> set[str]:
    source = (ROOT / "src" / "db.py").read_text(encoding="utf-8")
    return set(re.findall(r"CREATE TABLE IF NOT EXISTS\s+(\w+)", source))


def test_every_table_is_documented(docs):
    missing = sorted(t for t in _tables_created_by_init_db() if t not in docs)
    assert missing == [], f"tables created but not documented: {missing}"

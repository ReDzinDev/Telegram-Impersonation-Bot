
import os
import logging
from typing import Optional
from dotenv import load_dotenv

# No basicConfig here. A library module configuring root logging as an import
# side effect meant whichever of config/main imported first won, and it
# installed a stderr handler — which is what made every INFO line show up red
# in Railway. Logging is set up once by src.utils.logging_setup.setup_logging(),
# called from main before this module is imported.
load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"Required environment variable '{name}' is missing.")
    return value


def _optional(name: str, default: Optional[str] = None) -> Optional[str]:
    return os.getenv(name, default)


class ConfigError(Exception):
    """
    A configuration value is missing, unparseable, or out of range.

    Raised instead of letting a bare `int()` fail mid-import. The old behaviour
    was `ValueError: invalid literal for int() with base 10: 'eighty'` from
    somewhere inside the import chain, naming neither the variable nor the
    value — followed by ten Railway restarts and a permanently dead service.
    """


def _int_env(name: str, default: int, *, lo: int, hi: int) -> int:
    """
    Parse an integer env var, enforcing inclusive bounds.

    Bounds are not decoration. SWEEP_INTERVAL_HOURS=0 turns the periodic task
    into `asyncio.sleep(0)` and sweeps every group back-to-back forever, which
    is how the userbot account earns a PEER_FLOOD — and that credential is the
    expensive one to replace. A similarity threshold of 0 flags every user; one
    over 100 flags nobody and says nothing about it.
    """
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(
            f"{name}={raw!r} is not a whole number (expected {lo}-{hi}, "
            f"default {default})."
        ) from None
    if not (lo <= value <= hi):
        raise ConfigError(
            f"{name}={value} is out of range — must be between {lo} and {hi} "
            f"(default {default})."
        )
    return value


def _float_env(name: str, default: float, *, lo: float, hi: float) -> float:
    """Parse a float env var, enforcing inclusive bounds."""
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(
            f"{name}={raw!r} is not a number (expected {lo}-{hi}, "
            f"default {default})."
        ) from None
    if not (lo <= value <= hi):
        raise ConfigError(
            f"{name}={value} is out of range — must be between {lo} and {hi} "
            f"(default {default})."
        )
    return value


# name -> (default, lo, hi, parser). Bounds chosen from what the value does:
# an interval must be long enough not to hammer Telegram, a similarity score is
# a 1-100 percentage, and PFP_HASH_THRESHOLD is a Hamming distance over a
# 64-bit hash — so 85 is not "strict", it disables photo discrimination.
_NUMERIC_SETTINGS: dict[str, tuple] = {
    "NAME_SIMILARITY_THRESHOLD":      (85,   1,   100, _int_env),
    "USERNAME_SIMILARITY_THRESHOLD":  (88,   1,   100, _int_env),
    "PFP_HASH_THRESHOLD":             (10,   0,    64, _int_env),
    "DEFAULT_BAN_SCORE":              (90,   1,   100, _int_env),
    "DEFAULT_ALERT_SCORE":            (78,   1,   100, _int_env),
    "SWEEP_INTERVAL_HOURS":           (24,   1,   168, _int_env),
    "SWEEP_HARD_CAP_SECONDS":         (7200, 60, 86400, _int_env),
    "HEALTH_CHECK_INTERVAL":          (300,  30, 86400, _int_env),
    "DB_KEEPALIVE_INTERVAL":          (270,  30, 86400, _int_env),
    "NAME_CHANGE_VELOCITY_THRESHOLD": (3,    1,   100, _int_env),
    "NAME_CHANGE_WINDOW_MINUTES":     (60,   1,  1440, _int_env),
    "BIO_FETCH_MIN_INTERVAL":         (1.2,  0.0, 60.0, _float_env),
    "PFP_FETCH_MIN_INTERVAL":         (0.7,  0.0, 60.0, _float_env),
}


def load_settings() -> dict:
    """
    Parse and validate every numeric setting, reporting ALL problems at once.

    Aggregating matters: fixing configuration one crash-loop at a time is
    miserable, and on Railway each attempt costs a restart from a budget of ten.
    """
    values: dict[str, float | int] = {}
    problems: list[str] = []

    for name, (default, lo, hi, parser) in _NUMERIC_SETTINGS.items():
        try:
            values[name] = parser(name, default, lo=lo, hi=hi)
        except ConfigError as e:
            problems.append(str(e))
            values[name] = default      # keep going to collect the rest

    # Cross-field check: with alert above ban the mid band is empty, so nothing
    # ever downgrades to an alert and every match either bans or is ignored.
    if values["DEFAULT_ALERT_SCORE"] > values["DEFAULT_BAN_SCORE"]:
        problems.append(
            f"DEFAULT_ALERT_SCORE={values['DEFAULT_ALERT_SCORE']} is above "
            f"DEFAULT_BAN_SCORE={values['DEFAULT_BAN_SCORE']} — the alert band "
            "would be empty, so nothing could ever be downgraded to an alert."
        )

    if problems:
        raise ConfigError(
            "Invalid configuration:\n  - " + "\n  - ".join(problems)
        )
    return values


def _parse_group_ids(raw: Optional[str]) -> frozenset[int]:
    """Parse a comma-separated list of group IDs, ignoring junk entries."""
    if not raw:
        return frozenset()
    out = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            logging.warning(f"Ignoring non-numeric group id in config: {part!r}")
    return frozenset(out)


BOT_TOKEN = _require("BOT_TOKEN")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    pg_user = os.getenv("PGUSER", "postgres")
    pg_password = os.getenv("PGPASSWORD")
    pg_host = os.getenv("PGHOST")
    pg_port = os.getenv("PGPORT", "5432")
    pg_database = os.getenv("PGDATABASE", "railway")
    if pg_host and pg_password:
        DATABASE_URL = f"postgresql://{pg_user}:{pg_password}@{pg_host}:{pg_port}/{pg_database}"
        logging.info("DATABASE_URL constructed from individual PostgreSQL variables.")
    else:
        raise ValueError(
            "DATABASE_URL is required but missing. "
            "Set DATABASE_URL or individual vars: PGHOST, PGPASSWORD, PGUSER, PGPORT, PGDATABASE."
        )

# Global log channel (can be overridden per-group in DB)
LOG_CHANNEL_ID: Optional[str] = _optional("LOG_CHANNEL_ID")

# Pyrogram user client — needed for profile change events and full member sweeps.
# Generate PYROGRAM_SESSION once locally with: python -c "from pyrogram import Client; ..."
# See README for setup instructions.
PYROGRAM_API_ID: Optional[str] = _optional("PYROGRAM_API_ID")
PYROGRAM_API_HASH: Optional[str] = _optional("PYROGRAM_API_HASH")
PYROGRAM_SESSION: Optional[str] = _optional("PYROGRAM_SESSION")  # session string

PYROGRAM_ENABLED = bool(PYROGRAM_API_ID and PYROGRAM_API_HASH and PYROGRAM_SESSION)

# All numeric settings are parsed and range-checked in one pass, so a typo or a
# nonsense value fails at startup with every problem named at once instead of
# crash-looping on the first bad cast.
_SETTINGS = load_settings()

# Default detection thresholds (can be tuned via env, overridable per-group in DB)
NAME_SIMILARITY_THRESHOLD = _SETTINGS["NAME_SIMILARITY_THRESHOLD"]
# Usernames are more structured than display names, so they tolerate a
# stricter match before we call it impersonation.
USERNAME_SIMILARITY_THRESHOLD = _SETTINGS["USERNAME_SIMILARITY_THRESHOLD"]
# NOTE: a Hamming distance over a 64-bit hash, NOT a percentage.
PFP_HASH_THRESHOLD = _SETTINGS["PFP_HASH_THRESHOLD"]

# ── Severity score bands ────────────────────────────────────────────────────
# A flagged similarity match carries a 0-100 confidence score. Score bands turn
# that into an action without a hard binary cutoff:
#   score >= DEFAULT_BAN_SCORE   → execute the group's action_mode (ban/kick)
#   score >= DEFAULT_ALERT_SCORE → alert only (regardless of action_mode)
#   below                        → ignore
# Keyword / pfp / group-identity matches are high-confidence by construction and
# always treated as ban-band (see checker.ban_and_log). Overridable per-group.
DEFAULT_BAN_SCORE   = _SETTINGS["DEFAULT_BAN_SCORE"]
DEFAULT_ALERT_SCORE = _SETTINGS["DEFAULT_ALERT_SCORE"]

# ── Cross-group blocklist trust ─────────────────────────────────────────────
# known_bad_actors is a GLOBAL table consulted for every non-whitelisted user,
# but any admin of any enrolled group can write to it via /ban — and the bot
# auto-registers any group it is added to. Without a trust boundary, a stranger
# could enroll their own group and use it to ban arbitrary users out of every
# other group.
#
# Only bans originating from a group listed here (or from the group being
# checked itself) carry ban authority. Everything else is ADVISORY: still
# surfaced as an alert for a human, but it cannot execute a ban on its own.
#
# Operator-level on purpose — env only, so no group admin can grant themselves
# this. Empty (the default) means no group's bans propagate as actionable,
# which is the safe posture for a multi-tenant deployment.
BLOCKLIST_TRUSTED_GROUPS: frozenset[int] = _parse_group_ids(
    _optional("BLOCKLIST_TRUSTED_GROUPS")
)

# ── Background-task cadence (formerly magic numbers scattered across modules) ──
SWEEP_INTERVAL_HOURS           = _SETTINGS["SWEEP_INTERVAL_HOURS"]
SWEEP_HARD_CAP_SECONDS         = _SETTINGS["SWEEP_HARD_CAP_SECONDS"]
HEALTH_CHECK_INTERVAL          = _SETTINGS["HEALTH_CHECK_INTERVAL"]
DB_KEEPALIVE_INTERVAL          = _SETTINGS["DB_KEEPALIVE_INTERVAL"]
NAME_CHANGE_VELOCITY_THRESHOLD = _SETTINGS["NAME_CHANGE_VELOCITY_THRESHOLD"]
NAME_CHANGE_WINDOW_MINUTES     = _SETTINGS["NAME_CHANGE_WINDOW_MINUTES"]

# ── MTProto fetch pacing ────────────────────────────────────────────────────
# Minimum seconds between users.GetFullUser calls (bio fetches) and between
# profile-photo downloads, across ALL callers. These are proactive floors —
# staying under Telegram's sustained budget beats discovering it via
# FloodWait. On a flood the pacer in src.watcher.fetch ratchets the interval
# up automatically, so these only need to be roughly right.
BIO_FETCH_MIN_INTERVAL = _SETTINGS["BIO_FETCH_MIN_INTERVAL"]
PFP_FETCH_MIN_INTERVAL = _SETTINGS["PFP_FETCH_MIN_INTERVAL"]

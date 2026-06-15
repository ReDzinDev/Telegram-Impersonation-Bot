
import os
import logging
from typing import Optional
from dotenv import load_dotenv

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"Required environment variable '{name}' is missing.")
    return value


def _optional(name: str, default: Optional[str] = None) -> Optional[str]:
    return os.getenv(name, default)


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

# Default detection thresholds (can be tuned via env, overridable per-group in DB)
NAME_SIMILARITY_THRESHOLD = int(_optional("NAME_SIMILARITY_THRESHOLD", "85"))
# Usernames are more structured than display names, so they tolerate a
# stricter match before we call it impersonation.
USERNAME_SIMILARITY_THRESHOLD = int(_optional("USERNAME_SIMILARITY_THRESHOLD", "88"))
PFP_HASH_THRESHOLD = int(_optional("PFP_HASH_THRESHOLD", "10"))

# ── Severity score bands ────────────────────────────────────────────────────
# A flagged similarity match carries a 0-100 confidence score. Score bands turn
# that into an action without a hard binary cutoff:
#   score >= DEFAULT_BAN_SCORE   → execute the group's action_mode (ban/kick)
#   score >= DEFAULT_ALERT_SCORE → alert only (regardless of action_mode)
#   below                        → ignore
# Keyword / pfp / group-identity matches are high-confidence by construction and
# always treated as ban-band (see checker.ban_and_log). Overridable per-group.
DEFAULT_BAN_SCORE   = int(_optional("DEFAULT_BAN_SCORE", "90"))
DEFAULT_ALERT_SCORE = int(_optional("DEFAULT_ALERT_SCORE", "78"))

# ── Background-task cadence (formerly magic numbers scattered across modules) ──
SWEEP_INTERVAL_HOURS           = int(_optional("SWEEP_INTERVAL_HOURS", "24"))
SWEEP_HARD_CAP_SECONDS         = int(_optional("SWEEP_HARD_CAP_SECONDS", "7200"))
HEALTH_CHECK_INTERVAL          = int(_optional("HEALTH_CHECK_INTERVAL", "300"))
DB_KEEPALIVE_INTERVAL          = int(_optional("DB_KEEPALIVE_INTERVAL", "270"))
NAME_CHANGE_VELOCITY_THRESHOLD = int(_optional("NAME_CHANGE_VELOCITY_THRESHOLD", "3"))
NAME_CHANGE_WINDOW_MINUTES     = int(_optional("NAME_CHANGE_WINDOW_MINUTES", "60"))

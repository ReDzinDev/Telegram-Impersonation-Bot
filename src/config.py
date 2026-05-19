
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

# Default detection thresholds (can be tuned via env)
NAME_SIMILARITY_THRESHOLD = int(_optional("NAME_SIMILARITY_THRESHOLD", "85"))
PFP_HASH_THRESHOLD = int(_optional("PFP_HASH_THRESHOLD", "10"))

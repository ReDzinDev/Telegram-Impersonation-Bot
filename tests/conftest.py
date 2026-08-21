"""
Pytest bootstrap. Runs before any test module — and therefore before any
`import src.*` — so it is the only place that can neutralise config.py's
import-time environment read.

Two jobs:

1. Put the repo root on sys.path so plain `pytest` works, not just
   `python -m pytest` (which inserts the cwd implicitly).

2. Pin every environment variable src/config.py reads. This is deliberately a
   hard overwrite rather than setdefault: config.py calls load_dotenv(), which
   resolves .env relative to config.py's own directory, so without this the
   suite runs against the operator's live BOT_TOKEN, DATABASE_URL and
   PYROGRAM_SESSION — and inherits their production detection thresholds, which
   makes threshold-sensitive tests machine-dependent. load_dotenv() does not
   override variables already present in os.environ, so setting them here wins.

No test needs a real token or a reachable database; everything that touches the
DB is monkeypatched at the src.db boundary.
"""

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Credentials — dummy values, never read by any unit test.
os.environ["BOT_TOKEN"] = "test-bot-token"
os.environ["DATABASE_URL"] = "postgresql://test:test@localhost:5432/test"

# Empty string is falsy, so PYROGRAM_ENABLED resolves False and no real
# session string can reach the watcher under test.
os.environ["PYROGRAM_API_ID"] = ""
os.environ["PYROGRAM_API_HASH"] = ""
os.environ["PYROGRAM_SESSION"] = ""
os.environ["LOG_CHANNEL_ID"] = ""

# Individual-PG fallback vars: cleared so config.py can never synthesize a
# DATABASE_URL pointing at a real host if DATABASE_URL handling changes.
for _pg in ("PGUSER", "PGPASSWORD", "PGHOST", "PGPORT", "PGDATABASE"):
    os.environ.pop(_pg, None)

# Detection tuning — pinned to the code defaults so threshold-sensitive tests
# assert against a known baseline instead of the deployment's tuning.
os.environ["NAME_SIMILARITY_THRESHOLD"] = "85"
os.environ["USERNAME_SIMILARITY_THRESHOLD"] = "88"
os.environ["PFP_HASH_THRESHOLD"] = "10"
os.environ["DEFAULT_BAN_SCORE"] = "90"
os.environ["DEFAULT_ALERT_SCORE"] = "78"


import time
import logging
import psycopg
from psycopg.rows import dict_row
from src.config import DATABASE_URL

logger = logging.getLogger(__name__)


def get_connection(retries: int = 8, base_delay: float = 2.0):
    """
    Open a new psycopg connection with exponential-backoff retries.

    Railway Hobby databases go to sleep after inactivity and can take
    15-30 s to wake up.  Exponential backoff (2 → 4 → 8 → 16 → 30 → 30…)
    gives us up to ~2 minutes to wait for a cold-start without hammering
    the server.  connect_timeout=30 ensures a sleeping DB fails fast
    (rather than hanging) so the retry loop fires promptly.
    """
    for attempt in range(retries):
        try:
            return psycopg.connect(
                DATABASE_URL,
                row_factory=dict_row,
                connect_timeout=30,
            )
        except Exception as e:
            if attempt < retries - 1:
                delay = min(base_delay * (2 ** attempt), 30)
                logger.warning(
                    f"DB connection attempt {attempt + 1}/{retries} failed, "
                    f"retrying in {delay:.0f}s: {e}"
                )
                time.sleep(delay)
            else:
                logger.error(f"DB connection failed after {retries} attempts: {e}")
                return None


def init_db():
    conn = get_connection()
    if not conn:
        logger.error("Cannot initialize DB — no connection.")
        return

    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS groups (
                    group_id    BIGINT PRIMARY KEY,
                    title       TEXT,
                    log_channel_id BIGINT,
                    added_at    TIMESTAMPTZ DEFAULT NOW(),
                    updated_at  TIMESTAMPTZ DEFAULT NOW()
                );
            """)

            # Migration guard: add action_mode if table already existed without it
            cur.execute("""
                ALTER TABLE groups
                    ADD COLUMN IF NOT EXISTS action_mode TEXT NOT NULL DEFAULT 'ban';
            """)

            # Migration: drop the legacy check_mode column. We only support
            # the equivalent of RELAXED now (real-time Pyrogram watcher +
            # 6h auto-sweep cover what STRICT used to add).
            cur.execute("ALTER TABLE groups DROP COLUMN IF EXISTS check_mode;")

            # Per-group whitelist of protected users (admins + manual + watched VIPs)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS whitelisted_users (
                    group_id      BIGINT NOT NULL REFERENCES groups(group_id) ON DELETE CASCADE,
                    user_id       BIGINT NOT NULL,
                    username      TEXT,
                    first_name    TEXT,
                    last_name     TEXT,
                    pfp_hash      TEXT,
                    user_type     TEXT NOT NULL DEFAULT 'manual',
                    whitelisted_by BIGINT,
                    created_at    TIMESTAMPTZ DEFAULT NOW(),
                    updated_at    TIMESTAMPTZ DEFAULT NOW(),
                    PRIMARY KEY (group_id, user_id)
                );
            """)
            # Migration guard: add user_type if table already existed without it
            cur.execute("""
                ALTER TABLE whitelisted_users
                    ADD COLUMN IF NOT EXISTS user_type TEXT NOT NULL DEFAULT 'manual';
            """)
            # Migration: legacy 'watch' rows (from the removed /watch command)
            # collapse to plain 'manual'. They were functionally identical.
            cur.execute(
                "UPDATE whitelisted_users SET user_type = 'manual' WHERE user_type = 'watch';"
            )
            # is_bot: authoritative flag for listwhitelist's Bots section.
            # We backfill via the username-ends-in-'bot' heuristic for rows
            # that pre-date the column — /import_admins will overwrite with
            # the real value on its next run.
            cur.execute("""
                ALTER TABLE whitelisted_users
                    ADD COLUMN IF NOT EXISTS is_bot BOOLEAN NOT NULL DEFAULT FALSE;
            """)
            cur.execute("""
                UPDATE whitelisted_users
                   SET is_bot = TRUE
                 WHERE is_bot = FALSE
                   AND lower(username) LIKE '%bot';
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_wl_username ON whitelisted_users(group_id, username);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_wl_pfp     ON whitelisted_users(group_id, pfp_hash);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_wl_user_id ON whitelisted_users(user_id);")

            # Tracks which users have already been checked (drives RELAXED mode)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS seen_members (
                    group_id       BIGINT NOT NULL,
                    user_id        BIGINT NOT NULL,
                    first_seen_at  TIMESTAMPTZ DEFAULT NOW(),
                    last_checked_at TIMESTAMPTZ DEFAULT NOW(),
                    PRIMARY KEY (group_id, user_id)
                );
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS logs (
                    log_id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    group_id         BIGINT,
                    user_id          BIGINT NOT NULL,
                    username         TEXT,
                    full_name        TEXT,
                    target_user_id   BIGINT,
                    target_name      TEXT,
                    detection_type   TEXT NOT NULL,
                    similarity_score FLOAT,
                    action_taken     TEXT,
                    details          TEXT,
                    invite_link      TEXT,
                    trigger          TEXT DEFAULT 'join',
                    created_at       TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            # Migration guard: add invite_link if table already existed without it
            cur.execute("""
                ALTER TABLE logs
                    ADD COLUMN IF NOT EXISTS invite_link TEXT;
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_logs_group ON logs(group_id, created_at DESC);")

            # Per-group similarity threshold
            cur.execute("""
                ALTER TABLE groups
                    ADD COLUMN IF NOT EXISTS similarity_threshold INTEGER;
            """)

            # Reserved keywords / regex patterns per group
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reserved_keywords (
                    id          BIGSERIAL PRIMARY KEY,
                    group_id    BIGINT NOT NULL REFERENCES groups(group_id) ON DELETE CASCADE,
                    pattern     TEXT NOT NULL,
                    is_regex    BOOLEAN NOT NULL DEFAULT FALSE,
                    created_by  BIGINT,
                    created_at  TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE(group_id, pattern)
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_kw_group ON reserved_keywords(group_id);")

            # Name-change velocity tracking
            cur.execute("""
                CREATE TABLE IF NOT EXISTS name_change_log (
                    id         BIGSERIAL PRIMARY KEY,
                    user_id    BIGINT NOT NULL,
                    changed_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ncl_user_time ON name_change_log(user_id, changed_at DESC);")

            # Admin action audit log
            cur.execute("""
                CREATE TABLE IF NOT EXISTS admin_actions (
                    id         BIGSERIAL PRIMARY KEY,
                    group_id   BIGINT,
                    admin_id   BIGINT NOT NULL,
                    admin_name TEXT,
                    action     TEXT NOT NULL,
                    target_id  BIGINT,
                    details    TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_aa_group ON admin_actions(group_id, created_at DESC);")

            # Group identity: store the group's own PFP hash for detecting
            # users who impersonate the group itself.
            cur.execute("""
                ALTER TABLE groups
                    ADD COLUMN IF NOT EXISTS pfp_hash TEXT;
            """)

            # False-positive grace period: users cleared by an admin within
            # the window are skipped by detection without being whitelisted.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS false_positives (
                    group_id    BIGINT NOT NULL,
                    user_id     BIGINT NOT NULL,
                    cleared_by  BIGINT,
                    cleared_at  TIMESTAMPTZ DEFAULT NOW(),
                    expires_at  TIMESTAMPTZ NOT NULL,
                    PRIMARY KEY (group_id, user_id)
                );
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_fp_group ON false_positives(group_id, expires_at);"
            )

            # Per-group sweep run history — powers the per-run summary message
            # and the windowed "sweeps in the last 24h" counter in the daily digest.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sweep_runs (
                    id         BIGSERIAL PRIMARY KEY,
                    group_id   BIGINT NOT NULL,
                    iterated   INTEGER NOT NULL DEFAULT 0,
                    checked    INTEGER NOT NULL DEFAULT 0,
                    flagged    INTEGER NOT NULL DEFAULT 0,
                    errors     INTEGER NOT NULL DEFAULT 0,
                    trigger    TEXT    NOT NULL DEFAULT 'auto',
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_sweep_group ON sweep_runs(group_id, created_at DESC);"
            )

        conn.commit()
        logger.info("Database initialized.")
    except Exception as e:
        logger.error(f"DB init error: {e}")
        conn.rollback()
    finally:
        conn.close()


# ── Group helpers ──────────────────────────────────────────────────────────────

# Short-lived in-process caches so the sweep doesn't open a new DB connection
# for every member.  get_group / get_reserved_keywords are called once per
# checked member; without a cache a 1000-member sweep = 2000+ DB connections.
_group_cache:    dict[int, tuple[float, dict | None]]        = {}
_kw_cache:       dict[int, tuple[float, list[dict]]]          = {}
_fp_cache:       dict[tuple[int, int], tuple[float, bool]]    = {}
_GROUP_CACHE_TTL = 300   # 5 minutes — changes only via admin commands
_KW_CACHE_TTL    = 300
_FP_CACHE_TTL    = 300   # false-positive entries last 30 days; 5-min cache is fine


def _invalidate_group_cache(group_id: int):
    _group_cache.pop(group_id, None)


def _invalidate_kw_cache(group_id: int):
    _kw_cache.pop(group_id, None)


def upsert_group(group_id: int, title: str = None,
                 log_channel_id: int = None, pfp_hash: str = None) -> bool:
    conn = get_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            if pfp_hash is not None:
                cur.execute("""
                    INSERT INTO groups (group_id, title, log_channel_id, pfp_hash)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (group_id) DO UPDATE SET
                        title          = COALESCE(EXCLUDED.title, groups.title),
                        pfp_hash       = EXCLUDED.pfp_hash,
                        updated_at     = NOW();
                """, (group_id, title, log_channel_id, pfp_hash))
            else:
                cur.execute("""
                    INSERT INTO groups (group_id, title, log_channel_id)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (group_id) DO UPDATE SET
                        title          = COALESCE(EXCLUDED.title, groups.title),
                        updated_at     = NOW();
                """, (group_id, title, log_channel_id))
        conn.commit()
        _invalidate_group_cache(group_id)
        return True
    except Exception as e:
        logger.error(f"upsert_group error: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def get_group(group_id: int) -> dict | None:
    cached = _group_cache.get(group_id)
    if cached and time.time() - cached[0] < _GROUP_CACHE_TTL:
        return cached[1]

    conn = get_connection()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM groups WHERE group_id = %s", (group_id,))
            row = cur.fetchone()
        _group_cache[group_id] = (time.time(), row)
        return row
    except Exception as e:
        logger.error(f"get_group error: {e}")
        return None
    finally:
        conn.close()


def get_all_group_ids() -> list[int]:
    conn = get_connection()
    if not conn:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT group_id FROM groups")
            return [row["group_id"] for row in cur.fetchall()]
    except Exception as e:
        logger.error(f"get_all_group_ids error: {e}")
        return []
    finally:
        conn.close()


def set_group_log_channel(group_id: int, log_channel_id: int | None) -> bool:
    conn = get_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE groups SET log_channel_id = %s, updated_at = NOW() WHERE group_id = %s",
                (log_channel_id, group_id)
            )
        conn.commit()
        _invalidate_group_cache(group_id)
        return True
    except Exception as e:
        logger.error(f"set_group_log_channel error: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def set_group_action_mode(group_id: int, mode: str) -> bool:
    conn = get_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE groups SET action_mode = %s, updated_at = NOW() WHERE group_id = %s",
                (mode, group_id)
            )
        conn.commit()
        _invalidate_group_cache(group_id)
        return True
    except Exception as e:
        logger.error(f"set_group_action_mode error: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


# ── Whitelist helpers ──────────────────────────────────────────────────────────

_whitelist_cache: dict[int, tuple[float, list[dict]]] = {}  # group_id -> (timestamp, rows)
_WHITELIST_CACHE_TTL = 60  # seconds


def _invalidate_whitelist_cache(group_id: int):
    _whitelist_cache.pop(group_id, None)


def get_whitelist(group_id: int) -> list[dict]:
    cached = _whitelist_cache.get(group_id)
    if cached and time.time() - cached[0] < _WHITELIST_CACHE_TTL:
        return cached[1]

    conn = get_connection()
    if not conn:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM whitelisted_users WHERE group_id = %s", (group_id,))
            rows = cur.fetchall()
        _whitelist_cache[group_id] = (time.time(), rows)
        return rows
    except Exception as e:
        logger.error(f"get_whitelist error: {e}")
        return []
    finally:
        conn.close()


def is_whitelisted(group_id: int, user_id: int) -> bool:
    # Use the cache rather than a separate DB query
    return any(r["user_id"] == user_id for r in get_whitelist(group_id))


def _is_whitelisted_db(group_id: int, user_id: int) -> bool:
    """Direct DB check — used only when cache must be bypassed."""
    conn = get_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM whitelisted_users WHERE group_id = %s AND user_id = %s",
                (group_id, user_id)
            )
            return cur.fetchone() is not None
    except Exception as e:
        logger.error(f"is_whitelisted error: {e}")
        return False
    finally:
        conn.close()


def get_groups_for_user(user_id: int) -> list[int]:
    """Return all group_ids where user_id is whitelisted (used by Pyrogram watcher)."""
    conn = get_connection()
    if not conn:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT group_id FROM whitelisted_users WHERE user_id = %s",
                (user_id,)
            )
            return [row["group_id"] for row in cur.fetchall()]
    except Exception as e:
        logger.error(f"get_groups_for_user error: {e}")
        return []
    finally:
        conn.close()


def upsert_whitelisted_user(group_id: int, user_id: int, username: str,
                            first_name: str, last_name: str,
                            pfp_hash: str, whitelisted_by: int,
                            user_type: str = "manual",
                            is_bot: bool = False) -> bool:
    conn = get_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO whitelisted_users
                    (group_id, user_id, username, first_name, last_name, pfp_hash, whitelisted_by, user_type, is_bot)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (group_id, user_id) DO UPDATE SET
                    username   = EXCLUDED.username,
                    first_name = EXCLUDED.first_name,
                    last_name  = EXCLUDED.last_name,
                    pfp_hash   = COALESCE(EXCLUDED.pfp_hash, whitelisted_users.pfp_hash),
                    user_type  = EXCLUDED.user_type,
                    is_bot     = EXCLUDED.is_bot,
                    updated_at = NOW();
            """, (group_id, user_id, username, first_name, last_name, pfp_hash, whitelisted_by, user_type, is_bot))
        conn.commit()
        _invalidate_whitelist_cache(group_id)
        return True
    except Exception as e:
        logger.error(f"upsert_whitelisted_user error: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def remove_whitelisted_user(group_id: int, user_id: int) -> bool:
    conn = get_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM whitelisted_users WHERE group_id = %s AND user_id = %s",
                (group_id, user_id)
            )
            deleted = cur.rowcount > 0
        conn.commit()
        _invalidate_whitelist_cache(group_id)
        return deleted
    except Exception as e:
        logger.error(f"remove_whitelisted_user error: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


# ── Seen-member helpers (RELAXED mode) ────────────────────────────────────────

def is_seen(group_id: int, user_id: int) -> bool:
    conn = get_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM seen_members WHERE group_id = %s AND user_id = %s",
                (group_id, user_id)
            )
            return cur.fetchone() is not None
    except Exception as e:
        logger.error(f"is_seen error: {e}")
        return False
    finally:
        conn.close()


def mark_seen(group_id: int, user_id: int):
    conn = get_connection()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO seen_members (group_id, user_id)
                VALUES (%s, %s)
                ON CONFLICT (group_id, user_id) DO UPDATE SET last_checked_at = NOW();
            """, (group_id, user_id))
        conn.commit()
    except Exception as e:
        logger.error(f"mark_seen error: {e}")
        conn.rollback()
    finally:
        conn.close()


def unmark_seen(group_id: int, user_id: int):
    """Force a re-check of this user on their next message (used after profile change events)."""
    conn = get_connection()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM seen_members WHERE group_id = %s AND user_id = %s",
                (group_id, user_id)
            )
        conn.commit()
    except Exception as e:
        logger.error(f"unmark_seen error: {e}")
    finally:
        conn.close()


# ── Log helpers ────────────────────────────────────────────────────────────────

def insert_log(group_id: int, user_id: int, username: str, full_name: str,
               target_user_id: int, target_name: str, detection_type: str,
               similarity_score: float, action_taken: str, details: str,
               trigger: str = "join", invite_link: str = None):
    conn = get_connection()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO logs
                    (group_id, user_id, username, full_name, target_user_id, target_name,
                     detection_type, similarity_score, action_taken, details, trigger, invite_link)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (group_id, user_id, username, full_name, target_user_id, target_name,
                  detection_type, similarity_score, action_taken, details, trigger, invite_link))
        conn.commit()
    except Exception as e:
        logger.error(f"insert_log error: {e}")
        conn.rollback()
    finally:
        conn.close()


def get_latest_log_entry(group_id: int, user_id: int) -> dict | None:
    """Return the most recent log row for a user in a group (used by callback handlers)."""
    conn = get_connection()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM logs WHERE group_id = %s AND user_id = %s ORDER BY created_at DESC LIMIT 1",
                (group_id, user_id)
            )
            return cur.fetchone()
    except Exception as e:
        logger.error(f"get_latest_log_entry error: {e}")
        return None
    finally:
        conn.close()


def get_stats_windowed(group_id: int) -> dict:
    """
    Stats split into three windows: all-time / last 30d / last 7d.

    Returns a dict with `whitelisted` (current count) plus, for each of
    {detections, banned, sweeps}, the keys `<metric>_all`, `<metric>_30d`,
    and `<metric>_7d`. A single round-trip to the DB.
    """
    conn = get_connection()
    if not conn:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                  (SELECT COUNT(*) FROM whitelisted_users WHERE group_id = %(gid)s) AS whitelisted,

                  (SELECT COUNT(*) FROM logs WHERE group_id = %(gid)s)                                                                   AS detections_all,
                  (SELECT COUNT(*) FROM logs WHERE group_id = %(gid)s AND created_at > NOW() - INTERVAL '30 days')                       AS detections_30d,
                  (SELECT COUNT(*) FROM logs WHERE group_id = %(gid)s AND created_at > NOW() - INTERVAL '7 days')                        AS detections_7d,

                  (SELECT COUNT(*) FROM logs WHERE group_id = %(gid)s AND action_taken = 'banned')                                       AS banned_all,
                  (SELECT COUNT(*) FROM logs WHERE group_id = %(gid)s AND action_taken = 'banned' AND created_at > NOW() - INTERVAL '30 days') AS banned_30d,
                  (SELECT COUNT(*) FROM logs WHERE group_id = %(gid)s AND action_taken = 'banned' AND created_at > NOW() - INTERVAL '7 days')  AS banned_7d,

                  (SELECT COUNT(*) FROM sweep_runs WHERE group_id = %(gid)s)                                                             AS sweeps_all,
                  (SELECT COUNT(*) FROM sweep_runs WHERE group_id = %(gid)s AND created_at > NOW() - INTERVAL '30 days')                 AS sweeps_30d,
                  (SELECT COUNT(*) FROM sweep_runs WHERE group_id = %(gid)s AND created_at > NOW() - INTERVAL '7 days')                  AS sweeps_7d
            """, {"gid": group_id})
            return cur.fetchone() or {}
    except Exception as e:
        logger.error(f"get_stats_windowed error: {e}")
        return {}
    finally:
        conn.close()


def get_all_group_stats_windowed() -> list[dict]:
    """
    Per-group rollup with All / 30d / 7d windows for detections + bans,
    in a single round-trip. Used by /stats in private chat.
    """
    conn = get_connection()
    if not conn:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    g.group_id,
                    g.title,
                    g.action_mode,
                    COUNT(DISTINCT w.user_id)                                                                          AS whitelisted,
                    COUNT(DISTINCT l.log_id)                                                                           AS detections_all,
                    COUNT(DISTINCT CASE WHEN l.created_at > NOW() - INTERVAL '30 days' THEN l.log_id END)              AS detections_30d,
                    COUNT(DISTINCT CASE WHEN l.created_at > NOW() - INTERVAL '7 days'  THEN l.log_id END)              AS detections_7d,
                    COUNT(DISTINCT CASE WHEN l.action_taken = 'banned' THEN l.log_id END)                              AS banned_all,
                    COUNT(DISTINCT CASE WHEN l.action_taken = 'banned' AND l.created_at > NOW() - INTERVAL '30 days' THEN l.log_id END) AS banned_30d,
                    COUNT(DISTINCT CASE WHEN l.action_taken = 'banned' AND l.created_at > NOW() - INTERVAL '7 days'  THEN l.log_id END) AS banned_7d
                FROM groups g
                LEFT JOIN whitelisted_users w ON w.group_id = g.group_id
                LEFT JOIN logs l              ON l.group_id = g.group_id
                GROUP BY g.group_id, g.title, g.action_mode
                ORDER BY g.group_id
            """)
            return cur.fetchall()
    except Exception as e:
        logger.error(f"get_all_group_stats_windowed error: {e}")
        return []
    finally:
        conn.close()


def get_recent_activity(group_id: int, hours: int = 24) -> dict:
    """
    Activity counts within the last `hours` hours for one group.
    Used by the daily summary so it reports "what happened in the last day"
    instead of cumulative numbers.
    """
    conn = get_connection()
    if not conn:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                  (SELECT COUNT(*) FROM logs        WHERE group_id = %(gid)s AND created_at > NOW() - make_interval(hours => %(h)s))                                AS detections,
                  (SELECT COUNT(*) FROM logs        WHERE group_id = %(gid)s AND action_taken = 'banned' AND created_at > NOW() - make_interval(hours => %(h)s))    AS banned,
                  (SELECT COUNT(*) FROM logs        WHERE group_id = %(gid)s AND action_taken = 'kicked' AND created_at > NOW() - make_interval(hours => %(h)s))    AS kicked,
                  (SELECT COUNT(*) FROM logs        WHERE group_id = %(gid)s AND action_taken = 'alerted' AND created_at > NOW() - make_interval(hours => %(h)s))   AS alerted,
                  (SELECT COUNT(*) FROM sweep_runs  WHERE group_id = %(gid)s AND created_at > NOW() - make_interval(hours => %(h)s))                                AS sweeps
            """, {"gid": group_id, "h": hours})
            return cur.fetchone() or {}
    except Exception as e:
        logger.error(f"get_recent_activity error: {e}")
        return {}
    finally:
        conn.close()


# ── Sweep run history ─────────────────────────────────────────────────────────

def record_sweep_run(
    group_id: int, iterated: int, checked: int, flagged: int, errors: int,
    trigger: str = "auto",
) -> None:
    """Persist the result of one sweep_group() call so we can show
    'sweeps in the last 24h / 30d' and a per-run summary."""
    conn = get_connection()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO sweep_runs (group_id, iterated, checked, flagged, errors, trigger)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (group_id, iterated, checked, flagged, errors, trigger))
        conn.commit()
    except Exception as e:
        logger.error(f"record_sweep_run error: {e}")
        conn.rollback()
    finally:
        conn.close()


# ── Reserved keyword helpers ───────────────────────────────────────────────────

def add_reserved_keyword(group_id: int, pattern: str, is_regex: bool, created_by: int) -> bool:
    conn = get_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO reserved_keywords (group_id, pattern, is_regex, created_by)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (group_id, pattern) DO UPDATE SET is_regex = EXCLUDED.is_regex
            """, (group_id, pattern, is_regex, created_by))
        conn.commit()
        _invalidate_kw_cache(group_id)
        return True
    except Exception as e:
        logger.error(f"add_reserved_keyword error: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def remove_reserved_keyword(group_id: int, pattern: str) -> bool:
    conn = get_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM reserved_keywords WHERE group_id = %s AND pattern = %s",
                (group_id, pattern)
            )
            deleted = cur.rowcount > 0
        conn.commit()
        _invalidate_kw_cache(group_id)
        return deleted
    except Exception as e:
        logger.error(f"remove_reserved_keyword error: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def get_reserved_keywords(group_id: int) -> list[dict]:
    cached = _kw_cache.get(group_id)
    if cached and time.time() - cached[0] < _KW_CACHE_TTL:
        return cached[1]

    conn = get_connection()
    if not conn:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pattern, is_regex FROM reserved_keywords WHERE group_id = %s ORDER BY created_at",
                (group_id,)
            )
            rows = cur.fetchall()
        _kw_cache[group_id] = (time.time(), rows)
        return rows
    except Exception as e:
        logger.error(f"get_reserved_keywords error: {e}")
        return []
    finally:
        conn.close()


# ── Per-group threshold ────────────────────────────────────────────────────────

def set_group_threshold(group_id: int, threshold: int) -> bool:
    conn = get_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE groups SET similarity_threshold = %s, updated_at = NOW() WHERE group_id = %s",
                (threshold, group_id)
            )
        conn.commit()
        _invalidate_group_cache(group_id)
        return True
    except Exception as e:
        logger.error(f"set_group_threshold error: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


# ── Name-change velocity ───────────────────────────────────────────────────────

def log_name_change(user_id: int):
    conn = get_connection()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO name_change_log (user_id) VALUES (%s)",
                (user_id,)
            )
        conn.commit()
    except Exception as e:
        logger.error(f"log_name_change error: {e}")
        conn.rollback()
    finally:
        conn.close()


def count_recent_name_changes(user_id: int, window_minutes: int = 60) -> int:
    conn = get_connection()
    if not conn:
        return 0
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) AS cnt FROM name_change_log
                WHERE user_id = %s AND changed_at > NOW() - INTERVAL '%s minutes'
            """, (user_id, window_minutes))
            row = cur.fetchone()
            return row["cnt"] if row else 0
    except Exception as e:
        logger.error(f"count_recent_name_changes error: {e}")
        return 0
    finally:
        conn.close()


# ── Recent detections log ──────────────────────────────────────────────────────

def get_recent_logs(group_id: int, limit: int = 10) -> list[dict]:
    conn = get_connection()
    if not conn:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT user_id, username, full_name, target_name, detection_type,
                       similarity_score, action_taken, trigger, created_at
                FROM logs
                WHERE group_id = %s
                ORDER BY created_at DESC
                LIMIT %s
            """, (group_id, limit))
            return cur.fetchall()
    except Exception as e:
        logger.error(f"get_recent_logs error: {e}")
        return []
    finally:
        conn.close()


# ── Admin action audit log ─────────────────────────────────────────────────────

def log_admin_action(
    group_id: int | None,
    admin_id: int,
    admin_name: str,
    action: str,
    target_id: int | None = None,
    details: str | None = None,
) -> None:
    """Record a deliberate admin action (whitelist, ban, setmode, etc.)."""
    conn = get_connection()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO admin_actions (group_id, admin_id, admin_name, action, target_id, details)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (group_id, admin_id, admin_name, action, target_id, details))
        conn.commit()
    except Exception as e:
        logger.error(f"log_admin_action error: {e}")
        conn.rollback()
    finally:
        conn.close()


def get_recent_admin_actions(group_id: int, limit: int = 20) -> list[dict]:
    conn = get_connection()
    if not conn:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT admin_id, admin_name, action, target_id, details, created_at
                FROM admin_actions
                WHERE group_id = %s
                ORDER BY created_at DESC
                LIMIT %s
            """, (group_id, limit))
            return cur.fetchall()
    except Exception as e:
        logger.error(f"get_recent_admin_actions error: {e}")
        return []
    finally:
        conn.close()


# ── Bulk whitelist clear ───────────────────────────────────────────────────────

def clear_whitelist(group_id: int) -> int:
    """Remove ALL whitelisted users for a group. Returns count of rows deleted."""
    conn = get_connection()
    if not conn:
        return 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM whitelisted_users WHERE group_id = %s",
                (group_id,)
            )
            count = cur.rowcount
        conn.commit()
        _invalidate_whitelist_cache(group_id)
        return count
    except Exception as e:
        logger.error(f"clear_whitelist error: {e}")
        conn.rollback()
        return 0
    finally:
        conn.close()


# ── False-positive grace period ────────────────────────────────────────────────

def mark_false_positive(
    group_id: int, user_id: int, cleared_by: int, days: int = 30
) -> None:
    """
    Record a user as a confirmed false positive for this group.
    The bot will not re-flag this user for ``days`` days (default: 30).
    If the same user gets cleared again the window is reset.
    """
    from datetime import datetime, timedelta, timezone
    expires = datetime.now(timezone.utc) + timedelta(days=days)
    conn = get_connection()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO false_positives (group_id, user_id, cleared_by, expires_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (group_id, user_id) DO UPDATE SET
                    cleared_by = EXCLUDED.cleared_by,
                    cleared_at = NOW(),
                    expires_at = EXCLUDED.expires_at;
            """, (group_id, user_id, cleared_by, expires))
        conn.commit()
    except Exception as e:
        logger.error(f"mark_false_positive error: {e}")
        conn.rollback()
    finally:
        conn.close()


def is_false_positive(group_id: int, user_id: int) -> bool:
    """Return True if the user has an active (non-expired) false-positive record."""
    cache_key = (group_id, user_id)
    now = time.time()
    cached = _fp_cache.get(cache_key)
    if cached and now - cached[0] < _FP_CACHE_TTL:
        return cached[1]

    conn = get_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 1 FROM false_positives
                WHERE group_id = %s AND user_id = %s AND expires_at > NOW()
            """, (group_id, user_id))
            result = cur.fetchone() is not None
        _fp_cache[cache_key] = (now, result)
        return result
    except Exception as e:
        logger.error(f"is_false_positive error: {e}")
        return False
    finally:
        conn.close()

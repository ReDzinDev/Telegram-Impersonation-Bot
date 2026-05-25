
import time
import logging
import psycopg
from psycopg.rows import dict_row
from src.config import DATABASE_URL

logger = logging.getLogger(__name__)


def get_connection(retries: int = 5, delay: int = 2):
    for attempt in range(retries):
        try:
            return psycopg.connect(DATABASE_URL, row_factory=dict_row)
        except Exception as e:
            if attempt < retries - 1:
                logger.warning(f"DB connection attempt {attempt + 1}/{retries} failed, retrying in {delay}s: {e}")
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
                    check_mode  TEXT NOT NULL DEFAULT 'relaxed',
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

        conn.commit()
        logger.info("Database initialized.")
    except Exception as e:
        logger.error(f"DB init error: {e}")
        conn.rollback()
    finally:
        conn.close()


# ── Group helpers ──────────────────────────────────────────────────────────────

def upsert_group(group_id: int, title: str = None, check_mode: str = "relaxed",
                 log_channel_id: int = None) -> bool:
    conn = get_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO groups (group_id, title, check_mode, log_channel_id)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (group_id) DO UPDATE SET
                    title          = COALESCE(EXCLUDED.title, groups.title),
                    updated_at     = NOW();
            """, (group_id, title, check_mode, log_channel_id))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"upsert_group error: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def get_group(group_id: int) -> dict | None:
    conn = get_connection()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM groups WHERE group_id = %s", (group_id,))
            return cur.fetchone()
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
        return True
    except Exception as e:
        logger.error(f"set_group_log_channel error: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def set_group_check_mode(group_id: int, mode: str) -> bool:
    conn = get_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE groups SET check_mode = %s, updated_at = NOW() WHERE group_id = %s",
                (mode, group_id)
            )
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"set_group_check_mode error: {e}")
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
                            user_type: str = "manual") -> bool:
    conn = get_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO whitelisted_users
                    (group_id, user_id, username, first_name, last_name, pfp_hash, whitelisted_by, user_type)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (group_id, user_id) DO UPDATE SET
                    username   = EXCLUDED.username,
                    first_name = EXCLUDED.first_name,
                    last_name  = EXCLUDED.last_name,
                    pfp_hash   = COALESCE(EXCLUDED.pfp_hash, whitelisted_users.pfp_hash),
                    user_type  = EXCLUDED.user_type,
                    updated_at = NOW();
            """, (group_id, user_id, username, first_name, last_name, pfp_hash, whitelisted_by, user_type))
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


def get_stats(group_id: int) -> dict:
    conn = get_connection()
    if not conn:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS total FROM whitelisted_users WHERE group_id = %s",
                (group_id,)
            )
            wl_count = cur.fetchone()["total"]

            cur.execute(
                "SELECT COUNT(*) AS total FROM logs WHERE group_id = %s AND action_taken = 'banned'",
                (group_id,)
            )
            ban_count = cur.fetchone()["total"]

            cur.execute(
                "SELECT COUNT(*) AS total FROM logs WHERE group_id = %s",
                (group_id,)
            )
            detection_count = cur.fetchone()["total"]

        return {"whitelisted": wl_count, "banned": ban_count, "detections": detection_count}
    except Exception as e:
        logger.error(f"get_stats error: {e}")
        return {}
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
        return deleted
    except Exception as e:
        logger.error(f"remove_reserved_keyword error: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def get_reserved_keywords(group_id: int) -> list[dict]:
    conn = get_connection()
    if not conn:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pattern, is_regex FROM reserved_keywords WHERE group_id = %s ORDER BY created_at",
                (group_id,)
            )
            return cur.fetchall()
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


# ── All-groups stats ───────────────────────────────────────────────────────────

def get_all_group_stats() -> list[dict]:
    """Return protection and detection stats for every registered group."""
    conn = get_connection()
    if not conn:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    g.group_id,
                    g.title,
                    g.check_mode,
                    g.action_mode,
                    COUNT(DISTINCT w.user_id)                                          AS whitelisted,
                    COUNT(DISTINCT CASE WHEN l.action_taken = 'banned' THEN l.log_id END) AS banned,
                    COUNT(DISTINCT l.log_id)                                           AS detections
                FROM groups g
                LEFT JOIN whitelisted_users w ON w.group_id = g.group_id
                LEFT JOIN logs l              ON l.group_id = g.group_id
                GROUP BY g.group_id, g.title, g.check_mode, g.action_mode
                ORDER BY g.group_id
            """)
            return cur.fetchall()
    except Exception as e:
        logger.error(f"get_all_group_stats error: {e}")
        return []
    finally:
        conn.close()

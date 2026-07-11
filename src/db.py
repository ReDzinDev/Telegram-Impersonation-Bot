
import time
import logging
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from src.config import DATABASE_URL

logger = logging.getLogger(__name__)

# ── Connection pool ──────────────────────────────────────────────────────────
# Previously every DB call opened a fresh psycopg connection; a 1,000-member
# sweep meant 2,000+ TCP+TLS handshakes. A process-wide pool reuses a handful
# of live connections instead — far less overhead on Railway Hobby.
#
# Callers keep the existing contract: `conn = get_connection()` (None on
# failure) then `put_connection(conn)` in a finally block. put_connection
# returns the connection to the pool rather than closing it.

DB_POOL_MAX_SIZE = 10
_pool: ConnectionPool | None = None


def _get_pool() -> ConnectionPool:
    """
    Lazily build the process-wide pool.

    min_size=0 → we never eagerly open a connection at construction time
    (Railway Hobby Postgres may be asleep at boot, which would make eager
    opening fail). check_connection validates each borrowed connection and
    transparently replaces any the server dropped during a sleep window,
    so callers never receive a dead socket.
    """
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=DATABASE_URL,
            min_size=0,
            max_size=DB_POOL_MAX_SIZE,
            max_idle=300,
            timeout=30,
            # autocommit=True so read helpers (a bare SELECT) don't leave the
            # connection in an open transaction when returned to the pool —
            # otherwise psycopg_pool logs "rolling back returned connection".
            # Every write helper does a single statement + an explicit commit()
            # which is a harmless no-op under autocommit, so nothing is lost.
            kwargs={"row_factory": dict_row, "connect_timeout": 30, "autocommit": True},
            check=ConnectionPool.check_connection,
            open=True,
        )
    return _pool


def get_connection(retries: int = 8, base_delay: float = 2.0):
    """
    Borrow a pooled connection, with exponential-backoff retries to ride out
    Railway Hobby cold starts (the DB can take 15-30 s to wake).

    Returns None after exhausting retries — every caller already handles the
    None case. ALWAYS return the connection via put_connection() when done.
    """
    for attempt in range(retries):
        try:
            return _get_pool().getconn(timeout=30)
        except Exception as e:
            if attempt < retries - 1:
                delay = min(base_delay * (2 ** attempt), 30)
                logger.warning(
                    f"DB pool getconn attempt {attempt + 1}/{retries} failed, "
                    f"retrying in {delay:.0f}s: {e}"
                )
                time.sleep(delay)
            else:
                logger.error(f"DB pool getconn failed after {retries} attempts: {e}")
                return None


def put_connection(conn) -> None:
    """
    Return a connection to the pool. Safe to call with None. If the pool
    rejects it (e.g. the connection is broken), close the raw socket so we
    don't leak it — the pool will open a fresh one on the next borrow.
    """
    if conn is None:
        return
    try:
        _get_pool().putconn(conn)
    except Exception as e:
        logger.warning(f"put_connection failed, closing raw socket: {e}")
        try:
            conn.close()
        except Exception:
            pass


def init_db():
    conn = get_connection()
    if not conn:
        # Raise, don't return: a soft return here leaves the bot polling
        # Telegram with no schema — every DB call then fails silently and the
        # process never exits, so Railway's ON_FAILURE restart never fires
        # (a "zombie" that protects nothing). Crashing is the correct outcome.
        raise RuntimeError("Cannot initialize DB — no connection available.")

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
            # Detection-time profile snapshot: freeze the impersonator's bio and
            # own PFP hash at the moment of detection, so the record stays
            # accurate even after the scammer changes their profile.
            cur.execute("ALTER TABLE logs ADD COLUMN IF NOT EXISTS bio          TEXT;")
            cur.execute("ALTER TABLE logs ADD COLUMN IF NOT EXISTS user_pfp_hash TEXT;")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_logs_group ON logs(group_id, created_at DESC);")
            # Backs get_latest_log_entry, which runs on every alert-button press
            # (filters group_id + user_id, newest first). Without this it scans
            # all of the group's logs.
            cur.execute("CREATE INDEX IF NOT EXISTS idx_logs_group_user ON logs(group_id, user_id, created_at DESC);")

            # Per-group similarity threshold (legacy general fallback)
            cur.execute("""
                ALTER TABLE groups
                    ADD COLUMN IF NOT EXISTS similarity_threshold INTEGER;
            """)
            # Per-match-type threshold overrides. NULL → fall back to
            # similarity_threshold → global config default.
            cur.execute("ALTER TABLE groups ADD COLUMN IF NOT EXISTS username_threshold INTEGER;")
            cur.execute("ALTER TABLE groups ADD COLUMN IF NOT EXISTS name_threshold     INTEGER;")
            # Severity score bands. NULL → global config defaults
            # (DEFAULT_BAN_SCORE / DEFAULT_ALERT_SCORE).
            cur.execute("ALTER TABLE groups ADD COLUMN IF NOT EXISTS ban_score   INTEGER;")
            cur.execute("ALTER TABLE groups ADD COLUMN IF NOT EXISTS alert_score INTEGER;")
            # Cross-group blocklist participation (on by default, opt-out).
            cur.execute(
                "ALTER TABLE groups ADD COLUMN IF NOT EXISTS use_global_blocklist BOOLEAN NOT NULL DEFAULT TRUE;"
            )

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

            # Cross-group blocklist: confirmed bad actors shared across every
            # group the bot manages. Populated only by HUMAN-confirmed bans
            # (manual /ban, alert-escalation ban). A group with
            # use_global_blocklist=TRUE acts on these at join/scan time.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS known_bad_actors (
                    user_id        BIGINT PRIMARY KEY,
                    username       TEXT,
                    full_name      TEXT,
                    reason         TEXT,
                    confirmed_by   BIGINT,
                    source_group_id BIGINT,
                    ban_count      INTEGER NOT NULL DEFAULT 1,
                    first_seen_at  TIMESTAMPTZ DEFAULT NOW(),
                    last_seen_at   TIMESTAMPTZ DEFAULT NOW()
                );
            """)

        conn.commit()
        logger.info("Database initialized.")
    except Exception as e:
        logger.error(f"DB init error: {e}", exc_info=True)
        conn.rollback()
        raise  # let the process crash so Railway restarts instead of zombie-ing
    finally:
        put_connection(conn)


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
        put_connection(conn)


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
        put_connection(conn)


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
        put_connection(conn)


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
        put_connection(conn)


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
        put_connection(conn)


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
        put_connection(conn)


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
        put_connection(conn)


def get_watched_groups_for_user(user_id: int) -> list[int]:
    """
    Return group_ids where this user is a *watched* member — i.e. they've been
    seen (messaged/swept) in the group and are NOT whitelisted.

    Used by the Pyrogram watcher to decide whether a profile change is worth
    checking. Whitelisted users are excluded because they're the protected
    identities, not impersonation suspects (check_user() would skip them
    anyway), and including them would fire the name-change velocity alert for
    admins instead of scammers.
    """
    conn = get_connection()
    if not conn:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.group_id
                  FROM seen_members s
                 WHERE s.user_id = %s
                   AND NOT EXISTS (
                       SELECT 1 FROM whitelisted_users w
                        WHERE w.group_id = s.group_id
                          AND w.user_id  = s.user_id
                   )
                """,
                (user_id,),
            )
            return [row["group_id"] for row in cur.fetchall()]
    except Exception as e:
        logger.error(f"get_watched_groups_for_user error: {e}")
        return []
    finally:
        put_connection(conn)


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
        put_connection(conn)


def remove_stale_admin_whitelist(group_id: int, keep_user_ids: set[int]) -> int:
    """
    Delete admin-typed rows in this group whose user_id is NOT in keep_user_ids.

    Used by /import_admins refresh to prune entries for users who were
    once admins but have since been demoted. Manual / bot-admin entries
    are untouched — only `user_type='admin'` rows are considered.
    Returns the number of rows deleted.
    """
    conn = get_connection()
    if not conn:
        return 0
    try:
        with conn.cursor() as cur:
            if keep_user_ids:
                cur.execute(
                    """
                    DELETE FROM whitelisted_users
                     WHERE group_id  = %s
                       AND user_type = 'admin'
                       AND user_id <> ALL(%s)
                    """,
                    (group_id, list(keep_user_ids)),
                )
            else:
                # No current admins — wipe all admin-typed rows for this group
                cur.execute(
                    "DELETE FROM whitelisted_users WHERE group_id = %s AND user_type = 'admin'",
                    (group_id,),
                )
            count = cur.rowcount
        conn.commit()
        _invalidate_whitelist_cache(group_id)
        return count
    except Exception as e:
        logger.error(f"remove_stale_admin_whitelist error: {e}")
        conn.rollback()
        return 0
    finally:
        put_connection(conn)


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
        put_connection(conn)


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
        put_connection(conn)


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
        put_connection(conn)


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
        put_connection(conn)


# ── Log helpers ────────────────────────────────────────────────────────────────

def insert_log(group_id: int, user_id: int, username: str, full_name: str,
               target_user_id: int, target_name: str, detection_type: str,
               similarity_score: float, action_taken: str, details: str,
               trigger: str = "join", invite_link: str = None,
               bio: str = None, user_pfp_hash: str = None):
    conn = get_connection()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO logs
                    (group_id, user_id, username, full_name, target_user_id, target_name,
                     detection_type, similarity_score, action_taken, details, trigger,
                     invite_link, bio, user_pfp_hash)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (group_id, user_id, username, full_name, target_user_id, target_name,
                  detection_type, similarity_score, action_taken, details, trigger,
                  invite_link, bio, user_pfp_hash))
        conn.commit()
    except Exception as e:
        logger.error(f"insert_log error: {e}")
        conn.rollback()
    finally:
        put_connection(conn)


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
        put_connection(conn)


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
        put_connection(conn)


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
        put_connection(conn)


def get_recent_activity(group_id: int, hours: int = 24) -> dict:
    """
    Activity counts within the last `hours` hours for one group.
    Used by the daily summary so it reports "what happened in the last day"
    instead of cumulative numbers.

    The interval is built server-side via `now() - (hours * interval '1 hour')`
    rather than `make_interval()` — make_interval refuses parameter binding
    in older psycopg builds, which silently broke the window.
    """
    conn = get_connection()
    if not conn:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                  (SELECT COUNT(*) FROM logs       WHERE group_id = %(gid)s AND created_at > NOW() - (%(h)s * INTERVAL '1 hour'))                                AS detections,
                  (SELECT COUNT(*) FROM logs       WHERE group_id = %(gid)s AND action_taken = 'banned'  AND created_at > NOW() - (%(h)s * INTERVAL '1 hour'))   AS banned,
                  (SELECT COUNT(*) FROM logs       WHERE group_id = %(gid)s AND action_taken = 'kicked'  AND created_at > NOW() - (%(h)s * INTERVAL '1 hour'))   AS kicked,
                  (SELECT COUNT(*) FROM logs       WHERE group_id = %(gid)s AND action_taken = 'alerted' AND created_at > NOW() - (%(h)s * INTERVAL '1 hour'))   AS alerted,
                  (SELECT COUNT(*) FROM sweep_runs WHERE group_id = %(gid)s AND created_at > NOW() - (%(h)s * INTERVAL '1 hour'))                                AS sweeps
            """, {"gid": group_id, "h": hours})
            return cur.fetchone() or {}
    except Exception as e:
        logger.error(f"get_recent_activity error: {e}")
        return {}
    finally:
        put_connection(conn)


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
        put_connection(conn)


def purge_old_records(logs_days: int = 90, sweeps_days: int = 90) -> dict:
    """
    Delete rows that only matter for a bounded window, so the (small, Railway
    Hobby) Postgres disk doesn't grow without bound. Returns a per-table count
    of deleted rows. Safe to run repeatedly (idempotent).

      name_change_log  — only queried over a ~60-minute velocity window
      false_positives  — expired grace records
      logs / sweep_runs — older than the retention window
    """
    deleted = {"name_change_log": 0, "false_positives": 0, "logs": 0, "sweep_runs": 0}
    conn = get_connection()
    if not conn:
        return deleted
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM name_change_log WHERE changed_at < NOW() - INTERVAL '1 day'"
            )
            deleted["name_change_log"] = cur.rowcount or 0
            cur.execute("DELETE FROM false_positives WHERE expires_at < NOW()")
            deleted["false_positives"] = cur.rowcount or 0
            cur.execute(
                "DELETE FROM logs WHERE created_at < NOW() - (%(d)s * INTERVAL '1 day')",
                {"d": logs_days},
            )
            deleted["logs"] = cur.rowcount or 0
            cur.execute(
                "DELETE FROM sweep_runs WHERE created_at < NOW() - (%(d)s * INTERVAL '1 day')",
                {"d": sweeps_days},
            )
            deleted["sweep_runs"] = cur.rowcount or 0
        conn.commit()
        logger.info(f"Retention purge: {deleted}")
    except Exception as e:
        logger.error(f"purge_old_records error: {e}", exc_info=True)
        conn.rollback()
    finally:
        put_connection(conn)
    return deleted


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
        put_connection(conn)


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
        put_connection(conn)


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
        put_connection(conn)


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
        put_connection(conn)


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
        put_connection(conn)


def count_recent_name_changes(user_id: int, window_minutes: int = 60) -> int:
    conn = get_connection()
    if not conn:
        return 0
    try:
        with conn.cursor() as cur:
            # Bind the interval as a multiplied unit literal — psycopg can't
            # substitute a parameter *inside* a quoted INTERVAL '...' string
            # (it becomes the literal '$n minutes'), which silently threw and
            # made this whole velocity signal return 0. Same pattern as
            # get_recent_activity below.
            cur.execute("""
                SELECT COUNT(*) AS cnt FROM name_change_log
                WHERE user_id = %(uid)s
                  AND changed_at > NOW() - (%(mins)s * INTERVAL '1 minute')
            """, {"uid": user_id, "mins": window_minutes})
            row = cur.fetchone()
            return row["cnt"] if row else 0
    except Exception as e:
        logger.error(f"count_recent_name_changes error: {e}")
        return 0
    finally:
        put_connection(conn)


# ── Recent detections log ──────────────────────────────────────────────────────

def get_recent_logs(group_id: int, limit: int = 10) -> list[dict]:
    """
    Recent detections for a group.

    LEFT JOINs whitelisted_users to pick up the target's current username
    (the logs table itself doesn't store it). If the target was later
    removed from the whitelist, `target_username` will be NULL — the
    formatter falls back to just showing the stored target_name.
    """
    conn = get_connection()
    if not conn:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    l.user_id, l.username, l.full_name,
                    l.target_user_id, l.target_name,
                    wl.username AS target_username,
                    l.detection_type, l.similarity_score,
                    l.action_taken, l.details, l.trigger, l.created_at
                FROM logs l
                LEFT JOIN whitelisted_users wl
                       ON wl.group_id = l.group_id
                      AND wl.user_id  = l.target_user_id
                WHERE l.group_id = %s
                ORDER BY l.created_at DESC
                LIMIT %s
            """, (group_id, limit))
            return cur.fetchall()
    except Exception as e:
        logger.error(f"get_recent_logs error: {e}")
        return []
    finally:
        put_connection(conn)


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
        put_connection(conn)


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
        put_connection(conn)


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
        put_connection(conn)


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
        # Drop the negative cache entry so a just-cleared user isn't re-flagged
        # from a stale `is_false_positive` result (cached for _FP_CACHE_TTL).
        _fp_cache.pop((group_id, user_id), None)
    except Exception as e:
        logger.error(f"mark_false_positive error: {e}")
        conn.rollback()
    finally:
        put_connection(conn)


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
        put_connection(conn)


# ── Per-type thresholds & severity score bands ─────────────────────────────────

def set_group_thresholds(
    group_id: int, username_threshold: int | None = None,
    name_threshold: int | None = None,
) -> bool:
    """
    Set per-match-type similarity thresholds. Only the provided values are
    updated (pass None to leave one unchanged). NULL in the DB means "fall
    back to similarity_threshold, then the global config default".
    """
    sets, params = [], []
    if username_threshold is not None:
        sets.append("username_threshold = %s")
        params.append(username_threshold)
    if name_threshold is not None:
        sets.append("name_threshold = %s")
        params.append(name_threshold)
    if not sets:
        return False
    params.append(group_id)
    conn = get_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE groups SET {', '.join(sets)}, updated_at = NOW() WHERE group_id = %s",
                tuple(params),
            )
        conn.commit()
        _invalidate_group_cache(group_id)
        return True
    except Exception as e:
        logger.error(f"set_group_thresholds error: {e}")
        conn.rollback()
        return False
    finally:
        put_connection(conn)


def set_group_score_bands(group_id: int, ban_score: int, alert_score: int) -> bool:
    """Set the severity bands. ban_score >= alert_score is enforced by the caller."""
    conn = get_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE groups SET ban_score = %s, alert_score = %s, updated_at = NOW() "
                "WHERE group_id = %s",
                (ban_score, alert_score, group_id),
            )
        conn.commit()
        _invalidate_group_cache(group_id)
        return True
    except Exception as e:
        logger.error(f"set_group_score_bands error: {e}")
        conn.rollback()
        return False
    finally:
        put_connection(conn)


def set_group_blocklist(group_id: int, enabled: bool) -> bool:
    """Toggle this group's participation in the cross-group blocklist."""
    conn = get_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE groups SET use_global_blocklist = %s, updated_at = NOW() WHERE group_id = %s",
                (enabled, group_id),
            )
        conn.commit()
        _invalidate_group_cache(group_id)
        return True
    except Exception as e:
        logger.error(f"set_group_blocklist error: {e}")
        conn.rollback()
        return False
    finally:
        put_connection(conn)


# ── Cross-group blocklist (known bad actors) ───────────────────────────────────

_bad_actor_cache: dict[int, tuple[float, dict | None]] = {}
_BAD_ACTOR_CACHE_TTL = 300


def _invalidate_bad_actor_cache(user_id: int):
    _bad_actor_cache.pop(user_id, None)


def add_known_bad_actor(
    user_id: int, username: str | None, full_name: str | None,
    reason: str, confirmed_by: int | None, source_group_id: int | None,
) -> bool:
    """
    Record (or re-confirm) a confirmed bad actor in the cross-group blocklist.
    On a repeat confirmation we bump ban_count and refresh last_seen_at so the
    list doubles as a "how widespread is this scammer" signal.
    Only HUMAN-confirmed bans should call this.
    """
    conn = get_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO known_bad_actors
                    (user_id, username, full_name, reason, confirmed_by, source_group_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    username        = COALESCE(EXCLUDED.username, known_bad_actors.username),
                    full_name       = COALESCE(EXCLUDED.full_name, known_bad_actors.full_name),
                    reason          = EXCLUDED.reason,
                    confirmed_by    = EXCLUDED.confirmed_by,
                    source_group_id = EXCLUDED.source_group_id,
                    ban_count       = known_bad_actors.ban_count + 1,
                    last_seen_at    = NOW();
            """, (user_id, username, full_name, reason, confirmed_by, source_group_id))
        conn.commit()
        _invalidate_bad_actor_cache(user_id)
        return True
    except Exception as e:
        logger.error(f"add_known_bad_actor error: {e}")
        conn.rollback()
        return False
    finally:
        put_connection(conn)


def get_known_bad_actor(user_id: int) -> dict | None:
    """Return the blocklist row for a user, or None. Cached for 5 min — this
    is consulted in the detection hot path (join / message / sweep)."""
    cached = _bad_actor_cache.get(user_id)
    if cached and time.time() - cached[0] < _BAD_ACTOR_CACHE_TTL:
        return cached[1]
    conn = get_connection()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM known_bad_actors WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
        _bad_actor_cache[user_id] = (time.time(), row)
        return row
    except Exception as e:
        logger.error(f"get_known_bad_actor error: {e}")
        return None
    finally:
        put_connection(conn)


def remove_known_bad_actor(user_id: int) -> bool:
    """Remove a user from the blocklist (used when a ban is reversed as a
    false positive, so they're not re-banned cross-group)."""
    conn = get_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM known_bad_actors WHERE user_id = %s", (user_id,))
            removed = cur.rowcount > 0
        conn.commit()
        _invalidate_bad_actor_cache(user_id)
        return removed
    except Exception as e:
        logger.error(f"remove_known_bad_actor error: {e}")
        conn.rollback()
        return False
    finally:
        put_connection(conn)

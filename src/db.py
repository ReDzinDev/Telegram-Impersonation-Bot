
import psycopg
from psycopg.rows import dict_row
from src.config import DATABASE_URL, logging

logger = logging.getLogger(__name__)

def get_connection():
    """Establishes connection to the database."""
    try:
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        return conn
    except Exception as e:
        logger.error(f"Error connecting to database: {e}")
        return None

def init_db():
    """Initializes database tables."""
    conn = get_connection()
    if not conn:
        logger.error("Failed to connect to database for initialization.")
        return

    try:
        with conn.cursor() as cur:
            # Drop tables for testing if needed; omit in prod unless specifically asked
            # cur.execute("DROP TABLE IF EXISTS logs;")
            # cur.execute("DROP TABLE IF EXISTS whitelisted_users;")

            # Table for storing whitelisted users (admins, trusted)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS whitelisted_users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    pfp_hash TEXT,
                    whitelisted_by BIGINT,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            
            # Create indexes for faster searches
            cur.execute("CREATE INDEX IF NOT EXISTS idx_whitelisted_username ON whitelisted_users(username);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_whitelisted_pfp ON whitelisted_users(pfp_hash);")

            # Table for logging detections and bans
            cur.execute("""
                CREATE TABLE IF NOT EXISTS logs (
                    log_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    target_user_id BIGINT, -- Who they were impersonating
                    detection_type TEXT NOT NULL, -- 'username', 'name', 'pfp'
                    similarity_score FLOAT, 
                    action_taken TEXT, -- 'banned', 'monitor', 'failed'
                    details TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            
        conn.commit()
        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.error(f"Error initializing database: {e}")
        conn.rollback()
    finally:
        conn.close()

if __name__ == "__main__":
    init_db()

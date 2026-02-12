
import os
import logging
from dotenv import load_dotenv

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

load_dotenv()


from typing import Optional

def get_env_variable(var_name: str, default: Optional[str] = None, required: bool = True) -> str:
    value = os.getenv(var_name, default)
    if required and not value:
        raise ValueError(f"Environment variable {var_name} is required but missing.")
    return value

BOT_TOKEN = get_env_variable("BOT_TOKEN")

# Railway provides individual PostgreSQL variables instead of DATABASE_URL
# Auto-construct DATABASE_URL if not provided
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    # Try to build from Railway's individual variables
    pg_user = os.getenv("PGUSER", "postgres")
    pg_password = os.getenv("PGPASSWORD")
    pg_host = os.getenv("PGHOST")
    pg_port = os.getenv("PGPORT", "5432")
    pg_database = os.getenv("PGDATABASE", "railway")
    
    if pg_host and pg_password:
        DATABASE_URL = f"postgresql://{pg_user}:{pg_password}@{pg_host}:{pg_port}/{pg_database}"
        logging.info("DATABASE_URL constructed from individual PostgreSQL variables")
    else:
        raise ValueError("DATABASE_URL is required but missing. Please set DATABASE_URL or individual PostgreSQL variables (PGHOST, PGPASSWORD, etc.)")

LOG_CHANNEL_ID = get_env_variable("LOG_CHANNEL_ID", required=False)

# Thresholds
NAME_SIMILARITY_THRESHOLD = int(get_env_variable("NAME_SIMILARITY_THRESHOLD", "85", required=False))
PFP_HASH_THRESHOLD = int(get_env_variable("PFP_HASH_THRESHOLD", "10", required=False)) # Hamming distance

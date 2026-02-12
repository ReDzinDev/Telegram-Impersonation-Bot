
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
DATABASE_URL = get_env_variable("DATABASE_URL")
LOG_CHANNEL_ID = get_env_variable("LOG_CHANNEL_ID", required=False)

# Thresholds
NAME_SIMILARITY_THRESHOLD = int(get_env_variable("NAME_SIMILARITY_THRESHOLD", "85", required=False))
PFP_HASH_THRESHOLD = int(get_env_variable("PFP_HASH_THRESHOLD", "10", required=False)) # Hamming distance

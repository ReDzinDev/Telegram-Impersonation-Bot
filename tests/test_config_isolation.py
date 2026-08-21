"""
Guards that the test suite never runs against real deployment configuration.

src/config.py calls load_dotenv() at import time, and it resolves .env relative
to config.py's own directory — so pytest picks up the operator's live .env no
matter where it is invoked from. That means (a) real credentials are loaded into
every test run, and (b) detection tests silently inherit whatever thresholds the
operator has tuned in production, so the same test can pass on one machine and
fail on another.

tests/conftest.py pins every variable config.py reads before src is imported.
These tests fail if that pinning ever regresses.
"""

from src import config


def test_credentials_are_test_values_not_real_ones():
    assert config.BOT_TOKEN == "test-bot-token"
    assert config.DATABASE_URL == "postgresql://test:test@localhost:5432/test"


def test_pyrogram_watcher_is_disabled_so_no_real_session_is_used():
    assert config.PYROGRAM_ENABLED is False
    assert not config.PYROGRAM_SESSION


def test_detection_thresholds_use_code_defaults_not_deployment_tuning():
    assert config.NAME_SIMILARITY_THRESHOLD == 85
    assert config.USERNAME_SIMILARITY_THRESHOLD == 88
    assert config.PFP_HASH_THRESHOLD == 10

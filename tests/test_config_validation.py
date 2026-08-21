"""
Configuration must be validated at startup, with a usable error (R-5).

src/config.py had twelve bare int()/float() casts and no bounds checks. Two
distinct failure modes:

  - a typo (NAME_SIMILARITY_THRESHOLD=eighty) raised
    `ValueError: invalid literal for int() with base 10: 'eighty'` from the
    middle of an import chain, naming neither the variable nor the value, and
    then crash-looped ten times against Railway's restart budget and stayed down
  - values that parse but are nonsense were accepted silently:
    SWEEP_INTERVAL_HOURS=0 turns the periodic task into `sleep(0)` and sweeps
    every group back-to-back forever, which is how a userbot account gets
    PEER_FLOOD'd — and that credential is the expensive one to replace. A
    threshold of 0 flags every user; over 100 flags nobody, silently.
"""
import pytest

from src import config


# ── _int_env ──────────────────────────────────────────────────────────────────

def test_valid_value_is_parsed(monkeypatch):
    monkeypatch.setenv("X_TEST", "42")
    assert config._int_env("X_TEST", 10, lo=0, hi=100) == 42


def test_missing_value_uses_the_default(monkeypatch):
    monkeypatch.delenv("X_TEST", raising=False)
    assert config._int_env("X_TEST", 10, lo=0, hi=100) == 10


def test_non_numeric_value_names_the_variable_and_the_value(monkeypatch):
    monkeypatch.setenv("X_TEST", "eighty")
    with pytest.raises(config.ConfigError) as exc:
        config._int_env("X_TEST", 10, lo=0, hi=100)
    message = str(exc.value)
    assert "X_TEST" in message, "the operator can't act on an error that omits the name"
    assert "eighty" in message


def test_out_of_range_value_is_rejected_with_the_bounds(monkeypatch):
    monkeypatch.setenv("X_TEST", "500")
    with pytest.raises(config.ConfigError) as exc:
        config._int_env("X_TEST", 10, lo=0, hi=100)
    assert "500" in str(exc.value)
    assert "100" in str(exc.value)


def test_float_env_validates_the_same_way(monkeypatch):
    monkeypatch.setenv("X_TEST", "not-a-float")
    with pytest.raises(config.ConfigError):
        config._float_env("X_TEST", 1.0, lo=0.0, hi=10.0)


# ── the bounds that matter operationally ──────────────────────────────────────

@pytest.mark.parametrize("name,value,reason", [
    ("SWEEP_INTERVAL_HOURS", "0", "sleep(0) sweeps forever and gets the account limited"),
    ("HEALTH_CHECK_INTERVAL", "0", "tight get_me() loop"),
    ("DB_KEEPALIVE_INTERVAL", "0", "tight query loop"),
    ("NAME_SIMILARITY_THRESHOLD", "0", "flags every user"),
    ("NAME_SIMILARITY_THRESHOLD", "150", "flags nobody, silently"),
    ("USERNAME_SIMILARITY_THRESHOLD", "0", "flags every user"),
    ("PFP_HASH_THRESHOLD", "85", "it is a 0-64 Hamming distance, not a percentage"),
    ("DEFAULT_BAN_SCORE", "0", "bans everything"),
    ("DEFAULT_ALERT_SCORE", "500", "out of range"),
])
def test_operationally_dangerous_values_are_refused(monkeypatch, name, value, reason):
    monkeypatch.setenv(name, value)
    with pytest.raises(config.ConfigError):
        config.load_settings()


def test_alert_score_above_ban_score_is_refused(monkeypatch):
    """Inverted bands mean the mid band is empty and nothing ever alerts."""
    monkeypatch.setenv("DEFAULT_BAN_SCORE", "70")
    monkeypatch.setenv("DEFAULT_ALERT_SCORE", "90")
    with pytest.raises(config.ConfigError) as exc:
        config.load_settings()
    assert "alert" in str(exc.value).lower()


def test_every_problem_is_reported_at_once(monkeypatch):
    """Fixing config one crash-loop at a time is miserable."""
    monkeypatch.setenv("SWEEP_INTERVAL_HOURS", "0")
    monkeypatch.setenv("NAME_SIMILARITY_THRESHOLD", "nope")
    monkeypatch.setenv("PFP_HASH_THRESHOLD", "999")
    with pytest.raises(config.ConfigError) as exc:
        config.load_settings()
    message = str(exc.value)
    for name in ("SWEEP_INTERVAL_HOURS", "NAME_SIMILARITY_THRESHOLD",
                 "PFP_HASH_THRESHOLD"):
        assert name in message, f"{name} missing from the aggregated report"


def test_a_valid_environment_loads_cleanly(monkeypatch):
    monkeypatch.setenv("SWEEP_INTERVAL_HOURS", "6")
    monkeypatch.setenv("NAME_SIMILARITY_THRESHOLD", "88")
    settings = config.load_settings()
    assert settings["SWEEP_INTERVAL_HOURS"] == 6
    assert settings["NAME_SIMILARITY_THRESHOLD"] == 88

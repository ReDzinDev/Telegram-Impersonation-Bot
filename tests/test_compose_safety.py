"""
`docker compose up` must not be able to reach production (R-8).

Compose interpolates `${VAR}` from the shell environment OR from a `.env` file in
the compose directory — and a `.env` with live values sits in this one. The bot
service referenced `${BOT_TOKEN}`, `${DATABASE_URL}` and `${PYROGRAM_SESSION}`
directly, so `docker compose up` started a second bot polling the production
token (duplicate getUpdates — the Conflict _error_handler warns about), writing
to the production database, and holding the production MTProto user session.
Real bans, from a container someone started to try something out.

The bundled `postgres` service made this look safe: it exists, it is started via
depends_on, and the bot never pointed at it.

The rule these tests enforce is that every value the dev container receives is
either a literal or comes from a DEV_-prefixed variable. A production variable
name cannot be interpolated by accident, because none is mentioned.
"""
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent
COMPOSE_PATH = ROOT / "docker-compose.yml"


@pytest.fixture(scope="module")
def raw() -> str:
    return COMPOSE_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def compose(raw) -> dict:
    return yaml.safe_load(raw)


def _config_values(node) -> list[str]:
    """
    Every string VALUE in the parsed document.

    Scanning the raw text instead would flag interpolation syntax written inside
    explanatory comments — which is prose, not configuration. Parsed YAML has no
    comments in it, so this tests what compose will actually act on.
    """
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [v for item in node.values() for v in _config_values(item)]
    if isinstance(node, list):
        return [v for item in node for v in _config_values(item)]
    return []


def _config_env_names() -> set[str]:
    """Every variable src/config.py reads — i.e. every production name."""
    source = (ROOT / "src" / "config.py").read_text(encoding="utf-8")
    names = set()
    names |= set(re.findall(r'_require\(\s*"([A-Z_0-9]+)"', source))
    names |= set(re.findall(r'_optional\(\s*"([A-Z_0-9]+)"', source))
    names |= set(re.findall(r'os\.getenv\(\s*"([A-Z_0-9]+)"', source))
    table = re.search(r"_NUMERIC_SETTINGS[^{]*\{(.*?)\n\}", source, re.S)
    if table:
        names |= set(re.findall(r'"([A-Z_0-9]+)":', table.group(1)))
    return names


def test_no_production_variable_is_interpolated(compose):
    """
    The core guarantee. If a production name appears inside an interpolation,
    compose reads it from the .env sitting next to this file.
    """
    values = " ".join(_config_values(compose))
    interpolated = set(re.findall(r"\$\{([A-Z_0-9]+)", values))
    leaked = sorted(interpolated & _config_env_names())
    assert leaked == [], (
        "docker-compose.yml interpolates production variable name(s) "
        f"{leaked} — these resolve from the .env in this directory"
    )


def test_every_interpolation_is_dev_prefixed(compose):
    values = " ".join(_config_values(compose))
    interpolated = sorted(set(re.findall(r"\$\{([A-Z_0-9]+)", values)))
    assert interpolated, "expected at least the dev token to be interpolated"
    bad = [name for name in interpolated
           if not name.startswith(("DEV_", "POSTGRES_"))]
    assert bad == [], f"non-DEV interpolations: {bad}"


def test_the_dev_token_is_required_not_defaulted(compose):
    """
    `:?` makes compose refuse to start with an explanation. A `:-` default would
    silently boot a bot with an empty token, and a bare ${DEV_BOT_TOKEN} would
    boot with an empty string too.
    """
    values = " ".join(_config_values(compose))
    assert re.search(r"\$\{DEV_BOT_TOKEN:\?", values), (
        "DEV_BOT_TOKEN must use the fail-fast interpolation form so compose "
        "refuses to start rather than booting with an empty token"
    )


def test_the_database_points_at_the_bundled_postgres(compose):
    env = compose["services"]["bot"]["environment"]
    url = env["DATABASE_URL"] if isinstance(env, dict) else next(
        v.split("=", 1)[1] for v in env if v.startswith("DATABASE_URL=")
    )
    assert "@postgres:" in url, f"DATABASE_URL does not target the compose service: {url}"
    assert "${" not in url, "DATABASE_URL must be a literal, not an interpolation"


def test_the_bundled_postgres_is_actually_used(compose):
    """It existed and was started, but nothing pointed at it."""
    services = compose["services"]
    assert "postgres" in services
    assert "postgres" in str(services["bot"].get("depends_on"))


def test_postgres_has_a_healthcheck(compose):
    """
    depends_on alone only waits for the container to START. Without a
    healthcheck the bot can race an unready database on first boot.
    """
    assert "healthcheck" in compose["services"]["postgres"]


def test_the_obsolete_version_key_is_gone(compose, raw):
    assert "version" not in compose, "the top-level `version` key is obsolete in Compose v2"
    assert not re.search(r"^version:", raw, re.M)


def test_restart_policy_matches_the_deployment(compose):
    """railway.json uses ON_FAILURE; a dev container should not differ silently."""
    assert compose["services"]["bot"].get("restart") == "on-failure"


def test_a_dev_env_template_exists_and_holds_no_secrets():
    template = ROOT / ".env.dev.example"
    assert template.exists(), "developers need a template that is not the real .env"
    body = template.read_text(encoding="utf-8")
    assert "DEV_BOT_TOKEN" in body
    # A template must never carry a real-looking token (digits:base64ish).
    assert not re.search(r"\b\d{8,}:[A-Za-z0-9_-]{30,}", body), (
        "the template appears to contain a real bot token"
    )

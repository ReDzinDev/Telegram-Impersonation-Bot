"""Tests for logging_setup — Railway severity handling and PII redaction.

Drop this next to your other tests. It needs pytest and nothing else: no gateway,
no database, no network.

Both properties this covers regress silently, which is why they are worth pinning:

  * a stray basicConfig() in any module imported later puts handlers back on
    stderr, and every INFO line goes red again;
  * a new log call site interpolating a user object leaks a username into a
    third-party log store that retains it.

Adjust the import path if the module does not live at utils/logging_setup.py.
"""
from __future__ import annotations

import importlib
import io
import json
import logging
import os
import sys

import pytest

import src.utils.logging_setup as logging_setup


def _reload(*, railway: bool):
    """Reload the module with a clean root logger, in the requested mode.

    The module caches whether it has configured logging, so a reload is the
    cleanest way to test both modes in one session.
    """
    for handler in logging.getLogger().handlers[:]:
        logging.getLogger().removeHandler(handler)
    if railway:
        os.environ["RAILWAY_ENVIRONMENT"] = "production"
    else:
        os.environ.pop("RAILWAY_ENVIRONMENT", None)
    return importlib.reload(logging_setup)


def _capture(module) -> io.StringIO:
    """Install logging and redirect the single handler into a buffer."""
    module.setup_logging()
    buffer = io.StringIO()
    logging.getLogger().handlers[0].stream = buffer
    return buffer


def _entries(buffer) -> list[dict]:
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


# ── Redaction ────────────────────────────────────────────────────────────────

class TestRedact:
    @pytest.fixture
    def ls(self):
        return _reload(railway=False)

    def test_drops_name_before_id(self, ls):
        """The f"{member} ({member.id})" idiom — how most leaks actually look."""
        out = ls.redact("left holding a claimed code: someuser (Nick) (807676696873926666).")
        assert "someuser" not in out
        assert "Nick" not in out
        # The numeric ID is kept on purpose: pseudonymous and needed for support.
        assert "807676696873926666" in out

    def test_drops_email(self, ls):
        assert "a@b.com" not in ls.redact("application from a@b.com received")

    def test_drops_legacy_handle(self, ls):
        assert "olduser#4821" not in ls.redact("banned olduser#4821 for spam")

    @pytest.mark.parametrize("line", [
        "Applied migration 013_add_index.sql",
        "Listening on 0.0.0.0:8000",
        "Nominations rejected: count=2 actor_id=1 target_id=2",
        "Wallet verify failed for 0x5f7a4c11be...: timeout",
    ])
    def test_leaves_ordinary_lines_alone(self, ls, line):
        assert ls.redact(line) == line

    def test_filter_runs_on_records_not_just_direct_calls(self, ls):
        buffer = _capture(ls)
        logging.getLogger("app").info("code held by someuser (807676696873926666).")
        assert "someuser" not in buffer.getvalue()

    def test_backstop_only_documented_limitation(self, ls):
        """A bare username with no adjacent ID is NOT detectable by pattern.

        This asserts the known gap rather than pretending it doesn't exist: the
        real fix is at the call site. If this ever starts passing, the filter got
        smarter and the assertion should be revisited.
        """
        assert "whitewolf.21" in ls.redact("Application submitted for whitewolf.21")


# ── Railway mode ─────────────────────────────────────────────────────────────

class TestRailwayMode:
    @pytest.fixture
    def ls(self):
        return _reload(railway=True)

    def test_levels_map_to_railway_vocabulary(self, ls):
        buffer = _capture(ls)
        log = logging.getLogger("app")
        log.debug("d")      # filtered out at INFO
        log.info("i")
        log.warning("w")
        log.error("e")
        log.critical("c")   # Railway has no "critical" — must become error
        assert [e["level"] for e in _entries(buffer)] == ["info", "warn", "error", "error"]

    def test_one_json_object_per_line(self, ls):
        """Railway parses per line: a multi-line payload arrives unparseable."""
        buffer = _capture(ls)
        logging.getLogger("app").info("first\nsecond")
        assert len(buffer.getvalue().strip().splitlines()) == 1

    def test_extra_fields_become_queryable_attributes(self, ls):
        buffer = _capture(ls)
        logging.getLogger("app.wallet").info("linked", extra={"chain": "evm", "rows": 3})
        entry = _entries(buffer)[0]
        assert entry["chain"] == "evm"
        assert entry["rows"] == 3
        assert entry["logger"] == "app.wallet"

    def test_exception_is_serialized(self, ls):
        buffer = _capture(ls)
        try:
            raise ValueError("boom")
        except ValueError:
            logging.getLogger("app").exception("failed")
        entry = _entries(buffer)[0]
        assert entry["level"] == "error"
        assert "ValueError: boom" in entry["error"]

    def test_unserializable_extra_does_not_lose_the_line(self, ls):
        """default=str means a surprising object costs a field, not the record."""
        buffer = _capture(ls)
        logging.getLogger("app").info("odd", extra={"obj": object()})
        assert len(_entries(buffer)) == 1


# ── Handler wiring ───────────────────────────────────────────────────────────

class TestHandlerWiring:
    def test_single_handler_on_stdout(self):
        """The root cause: a stderr handler makes Railway label INFO as error."""
        ls = _reload(railway=True)
        ls.setup_logging()
        handlers = logging.getLogger().handlers
        assert len(handlers) == 1
        assert handlers[0].stream is sys.stdout

    def test_idempotent(self):
        """Two entry points in one process both call this; lines must not double."""
        ls = _reload(railway=True)
        for _ in range(3):
            ls.setup_logging()
        assert len(logging.getLogger().handlers) == 1

    def test_replaces_a_prior_basicconfig_handler(self):
        """A library calling basicConfig() first must not leave stderr behind."""
        _reload(railway=True)
        logging.basicConfig()  # installs a stderr handler
        importlib.reload(logging_setup).setup_logging()
        handlers = logging.getLogger().handlers
        assert len(handlers) == 1
        assert handlers[0].stream is sys.stdout

    def test_uvicorn_loggers_propagate_to_our_handler(self):
        ls = _reload(railway=True)
        ls.setup_logging()
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            logger = logging.getLogger(name)
            assert logger.propagate, f"{name} must propagate"
            assert logger.handlers == [], f"{name} kept its own handler"

    def test_quiet_pins_named_loggers(self):
        ls = _reload(railway=True)
        ls.setup_logging(quiet=("httpx", "noisy.lib"))
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("noisy.lib").level == logging.WARNING


# ── Local mode ───────────────────────────────────────────────────────────────

class TestLocalMode:
    def test_human_readable_off_railway(self):
        """JSON in a dev terminal is why people revert this change."""
        ls = _reload(railway=False)
        buffer = _capture(ls)
        logging.getLogger("app").info("plain line")
        out = buffer.getvalue().strip()
        assert not out.startswith("{")
        assert "[INFO] app: plain line" in out

    def test_emoji_does_not_raise_on_a_legacy_console(self):
        """Windows consoles default to a codepage that cannot encode emoji; the
        handler must degrade rather than raise inside emit()."""
        ls = _reload(railway=False)
        buffer = _capture(ls)
        logging.getLogger("app").info("gift 🎁 check")
        assert buffer.getvalue().strip()


# ── project integration ───────────────────────────────────────────────────────
#
# The failure mode the Railway fix keeps running into is a library reinstalling a
# stderr handler AFTER setup_logging() has run — at which point INFO lines go
# back to being red and nothing in setup_logging's own tests notices.

def test_importing_main_leaves_exactly_one_stdout_handler():
    """
    src.main calls setup_logging() before its own src imports, then pulls in PTB
    and Pyrogram. If either installs a root handler, this catches it.
    """
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent("""
        import logging, sys
        import src.main            # noqa: F401  (import side effects are the point)
        root = logging.getLogger()
        streams = [getattr(h, "stream", None) for h in root.handlers]
        print("HANDLERS", len(root.handlers),
              sum(s is sys.stdout for s in streams),
              sum(s is sys.stderr for s in streams))
    """)
    env = {
        **os.environ,
        "BOT_TOKEN": "test-bot-token",
        "DATABASE_URL": "postgresql://test:test@localhost:5432/test",
        "PYROGRAM_API_ID": "", "PYROGRAM_API_HASH": "", "PYROGRAM_SESSION": "",
        "LOG_CHANNEL_ID": "",
    }
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, env=env, cwd=str(
            __import__("pathlib").Path(__file__).resolve().parent.parent
        ),
    )
    assert proc.returncode == 0, f"import failed:\n{proc.stderr}"
    # The import itself logs (config messages, the blocklist warning), so pick
    # out our marker line rather than assuming stdout starts with the numbers.
    marker = next(
        line for line in proc.stdout.splitlines() if line.startswith("HANDLERS")
    )
    total, on_stdout, on_stderr = map(int, marker.split()[1:4])
    assert total == 1, f"{total} root handlers after importing src.main"
    assert on_stdout == 1, "the handler is not on stdout"
    assert on_stderr == 0, "a stderr handler survived — INFO will render as error"


def test_config_module_does_not_configure_root_logging_on_import():
    """
    src.config used to call basicConfig at import time, so whichever of
    config/main imported first won — and it installed a stderr handler.
    """
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent("""
        import logging
        import src.config         # noqa: F401
        print(len(logging.getLogger().handlers))
    """)
    env = {
        **os.environ,
        "BOT_TOKEN": "test-bot-token",
        "DATABASE_URL": "postgresql://test:test@localhost:5432/test",
    }
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, env=env, cwd=str(
            __import__("pathlib").Path(__file__).resolve().parent.parent
        ),
    )
    assert proc.returncode == 0, proc.stderr
    assert int(proc.stdout.strip().splitlines()[-1]) == 0, (
        "importing src.config installed a root handler as a side effect"
    )

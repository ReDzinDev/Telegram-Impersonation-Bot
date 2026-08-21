"""
logging_setup.py — one logging configuration for a Python service on Railway.

Drop this in (utils/logging_setup.py is a natural home) and call setup_logging()
once per entry point, as early as you can.

Why this exists
---------------
Railway derives a log line's severity from the *stream* it arrives on, not from
its text: stdout becomes ``level: info``, stderr becomes ``level: error``. The one
exception is a JSON line carrying an explicit ``level``, which Railway parses and
trusts instead.

Python's default is exactly wrong for this. ``logging.basicConfig(...)`` with no
``handlers=`` writes to **stderr**, so every INFO line lands in the error bucket,
red, indistinguishable from a real failure.

Routing to stdout alone is only half a fix: then nothing is red, and genuine
WARNING/ERROR lines get labelled ``info`` and hide — usually worse, because now
you cannot find failures. So on Railway this emits single-line JSON with a real
level, which also makes ``@level:error`` and ``@logger:name`` filtering work in
the log explorer. Off Railway it stays human-readable, because JSON in a dev
terminal is miserable enough that people revert the whole change.

Personal data
-------------
Railway retains log lines, which makes it a third-party store of whatever you put
in them. ``redact()`` strips what is mechanically recognisable as personal, but it
is a backstop, not the fix: a bare username with no adjacent numeric ID cannot be
detected by pattern. Log ``user_id=<int>`` at the call site instead of the object.
Numeric platform IDs are kept deliberately — pseudonymous, needed for support, and
resolvable only by someone who already has access to the platform.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from collections.abc import Iterable
from datetime import datetime, timezone

# ── Redaction ────────────────────────────────────────────────────────────────

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")

# Legacy Discord-style handle, e.g. "someuser#4821".
_LEGACY_HANDLE = re.compile(r"\b[\w.]{2,32}#\d{4}\b")

# A name — optionally followed by a parenthesised nickname — sitting just before a
# parenthesised snowflake/ID. This is the f"{member} ({member.id})" idiom, which is
# how most username leaks actually look:
#     "left holding a claimed code: someuser (Nick) (807676696873926666)."
# Keep the ID, drop the name.
_NAME_BEFORE_ID = re.compile(r"[^\s:,()]+(?:\s+\([^)]*\))?\s+\((\d{15,20})\)")


def redact(message: str) -> str:
    """Strip personal data from a log message, preserving numeric IDs.

    Deliberately conservative: it removes what is mechanically recognisable and
    leaves everything else alone. Something non-personal in the same shape as a
    username (a guild or channel name before its ID) is also reduced to its ID —
    harmless over-redaction, and worth it to guarantee usernames never ship.
    """
    message = _EMAIL.sub("<email>", message)
    message = _LEGACY_HANDLE.sub("<user>", message)
    message = _NAME_BEFORE_ID.sub(r"id:\1", message)
    return message


class _RedactFilter(logging.Filter):
    """Applies redact() to every record before it reaches a formatter."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            # A broken format string is the caller's bug. Let the formatter
            # surface it rather than swallowing the record here.
            return True
        cleaned = redact(rendered)
        if cleaned != rendered:
            # Collapse msg+args into the redacted string: re-interpolating later
            # would reintroduce exactly what was just removed.
            record.msg = cleaned
            record.args = ()
        return True


# ── Formatters ───────────────────────────────────────────────────────────────

# Railway understands debug / info / warn / error only.
_RAILWAY_LEVEL = {
    logging.DEBUG: "debug",
    logging.INFO: "info",
    logging.WARNING: "warn",
    logging.ERROR: "error",
    logging.CRITICAL: "error",
}

# LogRecord's own attributes. Anything else on the record arrived via
# `logger.info(..., extra={...})` and becomes a queryable Railway attribute.
_RESERVED = frozenset({
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "message", "module",
    "msecs", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "taskName", "thread", "threadName",
})


class RailwayJsonFormatter(logging.Formatter):
    """One-line JSON per record, in the shape Railway parses.

    Must stay single-line: Railway treats each line as one entry, so a
    pretty-printed object arrives as N unparseable fragments.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "level": _RAILWAY_LEVEL.get(record.levelno, "info"),
            "message": record.getMessage(),
            "logger": record.name,
            "ts": datetime.fromtimestamp(
                record.created, timezone.utc
            ).isoformat(timespec="milliseconds"),
        }
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        # default=str so an unexpected object never costs the whole line.
        return json.dumps(payload, default=str, ensure_ascii=False)


# ── Setup ────────────────────────────────────────────────────────────────────

_configured = False


def on_railway() -> bool:
    """True when running on Railway, which sets RAILWAY_ENVIRONMENT itself."""
    return bool(os.getenv("RAILWAY_ENVIRONMENT"))


def setup_logging(
    level: int = logging.INFO,
    quiet: Iterable[str] = (),
) -> None:
    """Install a single stdout handler on the root logger.

    ``quiet`` names loggers to pin at WARNING. It is a parameter rather than a
    constant because each service silences a different set of libraries, and this
    module is meant to be copied between projects unchanged.

    Idempotent: services often have two entry points in one process (a bot plus a
    web server in a thread), and both will call this. A second call must be a
    no-op or every line doubles.
    """
    global _configured
    if _configured:
        return

    # Windows consoles default to a legacy codepage, so a single emoji in a log
    # message raises UnicodeEncodeError inside the handler. Railway is UTF-8 and
    # unaffected; this keeps local development from tripping over it. Only the
    # error policy changes — the platform encoding is left alone.
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except (ValueError, OSError):
            pass

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_RedactFilter())
    if on_railway():
        handler.setFormatter(RailwayJsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))

    root = logging.getLogger()
    # Drop anything a library (or an earlier basicConfig) already installed, so
    # no stderr handler survives to keep painting INFO lines red.
    for existing in root.handlers[:]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    for name in quiet:
        logging.getLogger(name).setLevel(logging.WARNING)

    # uvicorn ships its own dictConfig that installs a *stderr* handler for
    # everything except the access log. Clearing propagate=False and its handlers
    # routes it through ours instead, so API lines get real levels too. A no-op in
    # services that don't use uvicorn, kept so this file stays identical across
    # projects. NOTE: also pass log_config=None to uvicorn.Config / uvicorn.run,
    # or uvicorn re-applies its own config after this and undoes the fix.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lib = logging.getLogger(name)
        lib.handlers.clear()
        lib.propagate = True

    _configured = True

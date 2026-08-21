"""
User-controlled text must be escaped before it goes into an HTML send (A-5).

notify._alert_operator interpolated the group title and the raw exception string
into a parse_mode="HTML" message. Group titles are attacker-controlled
(upsert_group stores chat.title verbatim), so a tenant could:

  - rename their group to markup and have the operator's "log channel
    unreachable" alert render attacker-authored content as if the bot wrote it
  - or rename it to anything containing a bare '<' or '&', which makes the send
    RAISE. That exception is swallowed, so the tenant permanently suppresses the
    only signal the operator has that a log channel died.
"""
import asyncio

import pytest

from src.utils import notify


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None, **kw):
        self.sent.append(text)
        # Telegram rejects malformed entities; approximate that so a test can
        # tell "escaped" from "merely didn't crash locally".
        if parse_mode == "HTML":
            import re
            stripped = re.sub(r"</?(b|i|u|s|code|pre|a)(\s[^>]*)?>", "", text)
            if "<" in stripped or ">" in stripped:
                raise ValueError("Can't parse entities: unsupported start tag")
        return object()


OPERATOR_CHANNEL = "-1009999999999"
TENANT_GROUP = -1001111111111


@pytest.fixture
def operator_channel(monkeypatch):
    monkeypatch.setattr(notify, "LOG_CHANNEL_ID", OPERATOR_CHANNEL)
    notify._failures.clear()
    notify._alerted.clear()
    yield
    notify._failures.clear()
    notify._alerted.clear()


def _run_alert(monkeypatch, title, exc=RuntimeError("boom")):
    monkeypatch.setattr(notify, "get_all_group_ids", lambda: [TENANT_GROUP])
    monkeypatch.setattr(notify, "get_group",
                        lambda gid: {"title": title, "log_channel_id": -1002222222222})
    bot = _FakeBot()
    asyncio.run(notify._alert_operator(bot, -1002222222222, exc))
    return bot


def test_markup_in_a_group_title_is_escaped(monkeypatch, operator_channel):
    bot = _run_alert(monkeypatch, "<b>Log channel restored, ignore this</b>")
    assert bot.sent, "operator alert was not sent at all"
    body = bot.sent[0]
    assert "&lt;b&gt;" in body
    assert "<b>Log channel restored" not in body


def test_a_bare_angle_bracket_in_a_title_does_not_suppress_the_alert(
        monkeypatch, operator_channel):
    """This is the self-silencing case: the send used to raise and be swallowed."""
    bot = _run_alert(monkeypatch, "Crypto < Group & Friends")
    assert bot.sent, "a tenant's group name suppressed the operator alert"
    assert "&amp;" in bot.sent[0]


def test_exception_text_is_escaped(monkeypatch, operator_channel):
    bot = _run_alert(monkeypatch, "Normal Group",
                     exc=ValueError("bad <tag> in payload & more"))
    assert bot.sent
    assert "&lt;tag&gt;" in bot.sent[0]


# ── every group title bound for an HTML send must be escaped ──────────────────
#
# A-5 fixed notify.py and missed two more sites (sweep._post_sweep_summary and
# summary.run_daily_summary), both of which interpolate a group title into text
# sent through send_log_message — which sends HTML. Group titles are
# attacker-controlled: upsert_group stores chat.title verbatim.
#
# This scans for the pattern instead of naming the files, so the next place
# someone reads a title fails here rather than in production.

def test_no_group_title_is_interpolated_into_html_unescaped():
    """
    Group titles are attacker-controlled (upsert_group stores chat.title
    verbatim), so any title rendered into an HTML message must be escaped.

    Two subtleties this test has to respect:

    - a title read for fuzzy COMPARISON (checker's group-identity stage) must
      NOT be escaped; escaping would corrupt the match. So the rule is about
      rendering, not reading.
    - the codebase escapes in both places — at the assignment
      (`title = html.escape(...)`) and at the interpolation. Both are correct, so
      this walks the file in order and resolves each interpolation against the
      nearest preceding assignment of that name.
    """
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "src"
    html_tag = re.compile(r"</?(?:b|i|u|s|code|pre|a)[ >]")
    assign = re.compile(r"^\s*(\w*title\w*)\s*=\s*(.*)$")
    interp = re.compile(r"\{([^{}]*title[^{}]*)\}")

    offenders = []
    html_lines_seen = 0

    for path in sorted(src.rglob("*.py")):
        escaped_at_assignment: dict[str, bool] = {}
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = assign.match(line)
            if match:
                escaped_at_assignment[match.group(1)] = "html.escape" in match.group(2)
            if not html_tag.search(line):
                continue
            html_lines_seen += 1
            for expr in interp.findall(line):
                if "html.escape" in expr or "_html.escape" in expr:
                    continue          # escaped right here
                name = expr.strip()
                if escaped_at_assignment.get(name):
                    continue          # escaped when it was assigned
                offenders.append(f"{path.relative_to(src.parent)}:{lineno}")

    # Positive control. The first version of this test had its  escapes mangled
    # into literal backspace bytes, so the pattern matched nothing and the test
    # passed while two real offenders sat in the tree. A scanning test that can
    # silently match nothing is worse than no test.
    assert html_lines_seen > 20, (
        f"the scanner only matched {html_lines_seen} HTML lines in src/ - the "
        "pattern is probably broken, so a green result here means nothing"
    )

    assert offenders == [], (
        "group titles rendered into HTML without escaping - a tenant can rename "
        "their group to markup and spoof or break the message: "
        + "; ".join(offenders)
    )

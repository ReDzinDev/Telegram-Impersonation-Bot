# Anti-Impersonator Bot

Automatically detects and removes users who impersonate admins, VIPs, or the group itself inside Telegram groups. Monitors joins, messages, real-time profile changes, and scheduled full-group sweeps.

---

## What It Catches

| Check | Signal |
|---|---|
| Username similarity | Fuzzy match vs. protected usernames (e.g. `@j0hn_admin`) |
| Homoglyph username/name | Mixed-script lookalike characters (Cyrillic `а` for Latin `a`) |
| Display name similarity | Fuzzy match vs. protected display names |
| Profile photo | Perceptual hash match — tiebreaker for weak name matches |
| Reserved keywords | Any name/username/bio containing words like "Admin", "Support", custom patterns or regex |
| **Group identity** | Name or logo matching the group itself — catches impersonators of the group brand |

Detection is a pipeline — stops at first hit. Configurable fuzzy threshold per group (default 85/100).

---

## Detection Triggers

| Trigger | When | Pyrogram required |
|---|---|---|
| Join | Every new member | No |
| Message | First message per user (Relaxed) or every 5 min (Strict) | No |
| Profile change | Real-time rename / photo swap | **Yes** |
| Sweep | Full scan every 6 hours + on-demand `/sweep` | **Yes** |

---

## Actions & Modes

**Action** (per group): `ban` · `kick` · `alert`  
**Scan mode** (per group): `relaxed` (check once on first message) · `strict` (re-check every 5 min)

When a ban or kick fires, the log channel alert shows three inline buttons:
- **✅ Unban + Whitelist** — reverse and protect permanently
- **🔓 Unban only** — reverse with a 30-day grace period (no whitelist entry)
- **🗑 Dismiss** — keep the ban, remove buttons

---

## Key Commands

| Command | What it does |
|---|---|
| `/import_admins` | Whitelist all current admins (human + bots like Rose/Combot) and store the group's own logo for brand protection |
| `/watch` | Protect a non-admin VIP (reply or ID) |
| `/whitelist` / `/unwhitelist` | Add or remove any user (reply or ID) |
| `/sweep` | Run a full member scan immediately |
| `/check` | Manually check a user and preview the action that would be taken |
| `/setmode strict\|relaxed` | Set scan mode |
| `/setaction ban\|kick\|alert` | Set detection action |
| `/setthreshold 85` | Fuzzy sensitivity 50–100 (default 85) |
| `/addkeyword admin` | Reserve a word; prefix `r:` for regex |
| `/setlogchannel -100…` | Per-group log channel |
| `/stats` | Stats for this group (or all groups in private DM) |
| `/logs` / `/auditlog` | Recent detections / admin actions |
| `/clearwhitelist confirm` | ⚠️ Wipe the entire whitelist |

All commands work from a **private DM** with the bot — select a group via the picker, then manage it without posting in the group chat.

---

## Setup

```
1. Add the bot to your group as admin (Ban members permission)
2. DM the bot → Select Group → pick your group
3. /import_admins  — populates whitelist + stores group logo
4. /addkeyword admin  — add words only real admins use
5. /setlogchannel -1001234567890  — point to a private log channel
```

For real-time profile-change detection and sweeps, set the three Pyrogram env vars and add the watcher account to the group as a member.

---

## Environment Variables

| Variable | Required | Notes |
|---|---|---|
| `BOT_TOKEN` | **Yes** | From @BotFather |
| `DATABASE_URL` | **Yes** | PostgreSQL |
| `LOG_CHANNEL_ID` | Recommended | Global fallback log channel |
| `PYROGRAM_API_ID` | Optional | From my.telegram.org |
| `PYROGRAM_API_HASH` | Optional | From my.telegram.org |
| `PYROGRAM_SESSION` | Optional | Base64 session string — see `gen_session.py` |

---

## Generating a Pyrogram Session String

```python
# gen_session.py — run once, then delete
import asyncio, os
from pyrogram import Client

async def main():
    async with Client("s", api_id=int(os.getenv("PYROGRAM_API_ID")),
                           api_hash=os.getenv("PYROGRAM_API_HASH")) as app:
        print(await app.export_session_string())

asyncio.run(main())
```

Paste the output as `PYROGRAM_SESSION`. Delete the script and `s.session` afterwards.

---

## Stack

`python-telegram-bot` v21 · `pyrogram` v2 · `psycopg` v3 · `rapidfuzz` · `imagehash` · `confusable_homoglyphs` · PostgreSQL

For full architecture, database schema, detection internals, and operational details → see [`INTERNAL_DOCS.md`](INTERNAL_DOCS.md).

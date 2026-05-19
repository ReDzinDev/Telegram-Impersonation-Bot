# Anti-Impersonator Bot

A Telegram bot that detects and bans users impersonating admins or protected VIPs in your groups.

## How it works

The bot runs two components side by side:

- **PTB bot** — handles commands, monitors new joins, scans messages
- **Pyrogram userbot** — monitors real-time profile changes (name, username, PFP) and runs full member sweeps. The Bot API has no equivalent for these — MTProto is required.

Detection checks (in order):
1. Username similarity (fuzzy match, case-insensitive)
2. Homoglyph username (mixed-script lookalike characters)
3. Homoglyph display name
4. Display name similarity (fuzzy match)
5. Profile picture hash (perceptual hash, Hamming distance)

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | ✅ | From [@BotFather](https://t.me/BotFather) |
| `DATABASE_URL` | ✅ | PostgreSQL connection string |
| `PYROGRAM_API_ID` | ✅ | From [my.telegram.org](https://my.telegram.org) |
| `PYROGRAM_API_HASH` | ✅ | From [my.telegram.org](https://my.telegram.org) |
| `PYROGRAM_SESSION` | ✅ | Session string — see setup below |
| `LOG_CHANNEL_ID` | ☑️ | Global fallback log channel (e.g. `-1001234567890`) |
| `NAME_SIMILARITY_THRESHOLD` | ☑️ | Fuzzy match threshold, default `85` |
| `PFP_HASH_THRESHOLD` | ☑️ | PFP Hamming distance threshold, default `10` |

---

## First-time setup

### 1. Disable bot privacy mode

By default Telegram bots only see messages starting with `/`. You need to turn this off so the bot can scan all messages.

1. Open [@BotFather](https://t.me/BotFather)
2. `/mybots` → select your bot → **Bot Settings** → **Group Privacy** → **Turn off**

Alternatively, making the bot a group admin also bypasses privacy mode (which you need to do anyway for ban permissions).

### 2. Get Pyrogram credentials

Log into [my.telegram.org](https://my.telegram.org) **as the dedicated watcher account**, go to **API development tools**, create an app, and copy the **API ID** and **API Hash**.

### 3. Generate the session string

Create `gen_session.py` in the project root:

```python
import asyncio, os
from dotenv import load_dotenv
from pyrogram import Client

load_dotenv()

async def main():
    async with Client("session",
                      api_id=int(os.getenv("PYROGRAM_API_ID")),
                      api_hash=os.getenv("PYROGRAM_API_HASH")) as app:
        print(await app.export_session_string())

asyncio.run(main())
```

Run it, enter the phone number and OTP when prompted, then paste the printed string as `PYROGRAM_SESSION`. Delete `gen_session.py` and `session.session` afterwards.

### 4. Add the bots to your groups

- **The bot** must be a group **admin** with "Ban users" permission
- **The watcher account** must be a group **member** (no admin rights needed)

### 5. Deploy to Railway

```bash
git push
```

Railway rebuilds automatically. Once running, go into each group and run `/import_admins`.

### 6. Set a log channel (optional)

Create a channel, add the bot as admin, get the ID from [@userinfobot](https://t.me/userinfobot), then either:
- Set `LOG_CHANNEL_ID` in Railway env vars (global), or
- Run `/setlogchannel -1001234567890` in the group (per-group override)

### 7. Migrate the database (upgrading from v1)

The schema changed significantly. Connect to your PostgreSQL instance and run:

```sql
DROP TABLE IF EXISTS logs;
DROP TABLE IF EXISTS whitelisted_users;
DROP TABLE IF EXISTS seen_members;
DROP TABLE IF EXISTS groups;
```

The bot recreates everything on startup. Re-run `/import_admins` afterwards.

---

## Commands

All group commands are **admin-only**.

| Command | Description |
|---|---|
| `/import_admins` | Whitelist all current group admins |
| `/whitelist` | Whitelist a user (reply to their message) |
| `/unwhitelist` | Remove a user from the whitelist (reply or ID) |
| `/watch` | Protect a non-admin VIP's identity (reply or ID) |
| `/listwhitelist` | Show all protected users |
| `/exportwhitelist` | Download the whitelist as a CSV file |
| `/check` | Manually check a user (reply to their message) |
| `/ban` | Manually ban a user (reply or ID) |
| `/unban` | Unban a user by ID |
| `/sweep` | Run a full member scan (requires Pyrogram) |
| `/setmode strict\|relaxed` | Set the message scan mode for this group |
| `/setaction ban\|kick\|alert` | Set what happens when an impersonator is detected |
| `/setlogchannel` | Set a per-group log channel |
| `/stats` | Show detection and ban counts |

### Scan modes

- **relaxed** (default) — each user is checked once on their first message
- **strict** — re-checks every sender, at most once per 5 minutes

### Action modes

- **ban** (default) — permanently ban the impersonator
- **kick** — remove the user without a permanent ban (they can rejoin)
- **alert** — notify only; no action taken, useful for review-before-ban workflows

---

## Log channel alerts

When an impersonator is detected, a detailed alert is posted to the log channel including match type, similarity score, invite link used to join, and the action taken.

Alerts include inline buttons for quick moderation without leaving the log channel:

- **✅ Unban + Whitelist** — immediately unbans the user and adds them to the whitelist (for false positives)
- **🗑 Dismiss** — removes the buttons without taking action

A **daily summary** is automatically posted to the log channel at midnight UTC with detection and ban counts across all monitored groups.

---

## Automatic behaviours

- **New admin promoted** — automatically whitelisted without needing to re-run `/import_admins`
- **Startup sweep** — a full member scan runs 30 seconds after the bot starts, then every 6 hours
- **PFP auto-refresh** — whitelisted users' profile photo hashes are updated after every sweep so stored fingerprints never go stale
- **Profile change monitoring** — if an existing member changes their name, username, or photo to impersonate someone, the Pyrogram watcher catches it in real time

---

## Local development

```bash
pip install python-telegram-bot "psycopg[binary]" imagehash pillow rapidfuzz python-dotenv confusable_homoglyphs
# fill in .env
python run.py
```

Pyrogram features (sweeps, profile-change detection) are disabled when `PYROGRAM_SESSION` is not set. The bot will warn about this on startup but otherwise runs normally.

Uses Docker + PostgreSQL for a full local stack:

```bash
docker compose up
```

---

## Project structure

```
src/
├── main.py                 # Entry point — runs PTB + Pyrogram together
├── config.py               # Environment variable loading
├── db.py                   # Schema + all DB helpers
├── handlers/
│   ├── commands.py         # All bot commands
│   ├── member_join.py      # New member join detection + admin promotion whitelist
│   └── messages.py         # Per-message scan (STRICT/RELAXED)
├── utils/
│   ├── checker.py          # Core detection logic (shared by all triggers)
│   ├── detector.py         # Fuzzy matching + homoglyph detection
│   └── image.py            # Perceptual hash comparison
└── watcher/
    ├── client.py           # Pyrogram session setup
    ├── events.py           # Profile change event handlers
    ├── sweep.py            # Full member sweep + PFP refresh
    ├── health.py           # Pyrogram connection health check
    └── summary.py          # Daily digest to log channel
```

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

### 1. Get Pyrogram credentials

Log into [my.telegram.org](https://my.telegram.org) **as the dedicated watcher account**, go to **API development tools**, create an app, and copy the **API ID** and **API Hash**.

### 2. Generate the session string

Run this once locally (with `.env` populated):

```bash
pip install pyrogram tgcrypto python-dotenv
python -c "
from pyrogram import Client
import asyncio, os
from dotenv import load_dotenv
load_dotenv()
async def main():
    async with Client('session', api_id=os.getenv('PYROGRAM_API_ID'),
                      api_hash=os.getenv('PYROGRAM_API_HASH')) as app:
        print(await app.export_session_string())
asyncio.run(main())
"
```

Enter the phone number and OTP when prompted. Paste the printed string as `PYROGRAM_SESSION`.

### 3. Add the bots to your groups

- **The bot** must be a group **admin** with "Ban users" permission
- **The watcher account** (phone number) must be a group **member** (does not need admin rights)

### 4. Deploy to Railway

```bash
git push
```

Railway rebuilds automatically. Once running, go into each group and run `/import_admins`.

### 5. Set a log channel (optional)

Create a channel, add the bot as admin, get the ID from [@userinfobot](https://t.me/userinfobot), then either:
- Set `LOG_CHANNEL_ID` in Railway env vars (global), or
- Run `/setlogchannel -1001234567890` in the group (per-group override)

### 6. Migrate the database (upgrading from v1)

The schema changed significantly. Connect to your Railway PostgreSQL and run:

```sql
DROP TABLE IF EXISTS logs;
DROP TABLE IF EXISTS whitelisted_users;
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
| `/check` | Manually check a user (reply to their message) |
| `/ban` | Manually ban a user (reply or ID) |
| `/unban` | Unban a user by ID |
| `/sweep` | Run a full member scan (requires Pyrogram) |
| `/setmode strict\|relaxed` | Set the message scan mode for this group |
| `/setlogchannel` | Set a per-group log channel |
| `/stats` | Show detection and ban counts |

### Scan modes

- **relaxed** (default) — each user is checked once on their first message
- **strict** — re-checks every sender, at most once per 5 minutes

---

## Local development

```bash
pip install -r requirements.txt
# fill in .env
python run.py
```

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
│   ├── member_join.py      # New member join detection
│   └── messages.py         # Per-message scan (STRICT/RELAXED)
├── utils/
│   ├── checker.py          # Core detection logic (shared by all triggers)
│   ├── detector.py         # Fuzzy matching + homoglyph detection
│   └── image.py            # Perceptual hash comparison
└── watcher/
    ├── client.py           # Pyrogram session setup
    ├── events.py           # Profile change event handlers
    ├── sweep.py            # Full member sweep + PFP refresh
    └── health.py           # Pyrogram connection health check
```

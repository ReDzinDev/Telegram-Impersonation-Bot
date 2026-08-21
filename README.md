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
| Sweep | Full scan every `SWEEP_INTERVAL_HOURS` (default **24h**) + on-demand `/sweep` | **Yes** |

---

## Actions & Modes

**Action** (per group): `ban` · `kick` · `alert`

**Severity bands sit above the action.** A similarity match carries a 0-100
confidence score, and the score decides whether the action runs at all:

| Score | Outcome |
|---|---|
| ≥ `ban_score` (default 90) | Run the group's action (`ban`/`kick`) |
| ≥ `alert_score` (default 78) | Alert only, even in `ban` mode |
| below | Ignored — nothing logged |

So a group set to `ban` will still only *alert* on a mid-confidence match. That
is usually the explanation for "why wasn't this user banned?". Tune with
`/setbands`. Keyword, photo and group-identity matches are treated as full
confidence by construction and always land in the ban band.

**Cross-group blocklist.** A manual `/ban` can propagate to other groups the bot
protects — but only from groups listed in `BLOCKLIST_TRUSTED_GROUPS`. An entry
from anywhere else is *advisory*: it raises an alert but never bans on its own,
because any admin of any group the bot has been added to could otherwise get
arbitrary users banned everywhere. Groups opt out with `/blocklist off`.

Each user is scanned **once** — on their first message in the group — and re-checked thereafter via the Pyrogram profile-change watcher and the periodic auto-sweep
(every `SWEEP_INTERVAL_HOURS`, default 24h).

When a detection fires, the log channel alert shows inline buttons. After a ban/kick: **Unban + Whitelist** · **Unban only (30-day grace)** · **Dismiss**. In alert-only mode: **Ban** · **Kick** · **Whitelist** · **Ignore (30d)** · **Dismiss**.

---

## Key Commands

| Command | What it does |
|---|---|
| `/import_admins` | Whitelist all current admins (human + bots like Rose/Combot) and store the group's own logo for brand protection |
| `/whitelist` / `/unwhitelist` | Add or remove any user (reply or ID). Falls back to the Pyrogram userbot for users not yet in the chat. |
| `/listwhitelist` | Show whitelist (Admins / Bots / Manual sections) + CSV export attached |
| `/sweep` | Run a full member scan immediately |
| `/setaction ban\|kick\|alert` | Set detection action |
| `/setthreshold 85` | Fuzzy sensitivity 50–100 (default 85) |
| `/addkeyword admin, *mod*, r:official.*ceo` | Add keywords — commas, `*` wildcards, and `r:` regex all supported |
| `/setlogchannel` | Pick a per-group log channel via the channel picker |
| `/stats` | Stats with All-time / 30d / 7d breakdown |
| `/logs` | Recent detections + admin actions in one reply |
| `/clearwhitelist confirm` | ⚠️ Wipe the entire whitelist (posts a CSV backup first) |
| `/importwhitelist` | Restore a whitelist — reply to a CSV with the command, or just send the CSV |
| `/settings` | Show every setting for the selected group in one reply |
| `/setthresholds username=88 name=85` | Per-match-type thresholds, overriding `/setthreshold` |
| `/setbands 90 78` | Severity bands: ban at/above 90, alert at/above 78, ignore below |
| `/blocklist on\|off` | Opt this group in or out of the cross-group blocklist |
| `/protect "Some Name"` | Protect an external identity by name (optionally with a photo) |

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

**Required**

| Variable | Notes |
|---|---|
| `BOT_TOKEN` | From @BotFather |
| `DATABASE_URL` | PostgreSQL connection string. On Railway you may instead set `PGHOST`, `PGPASSWORD`, `PGUSER`, `PGPORT`, `PGDATABASE` and the URL is assembled from them. |

**Recommended**

| Variable | Default | Notes |
|---|---|---|
| `LOG_CHANNEL_ID` | — | Global fallback log channel. Per-group channels set with `/setlogchannel` take precedence. Retention (`purge_old_records`) currently runs from the daily-summary task, which only starts when this is set. |
| `BLOCKLIST_TRUSTED_GROUPS` | *(empty)* | Comma-separated group IDs whose manual bans may propagate to other groups. **Empty means propagation is off** — pre-existing blocklist entries degrade to alert-only. Set this to your own group IDs to enable it. |

**Watcher (MTProto)** — needed for real-time profile-change detection and `/sweep`

| Variable | Notes |
|---|---|
| `PYROGRAM_API_ID` | From my.telegram.org |
| `PYROGRAM_API_HASH` | From my.telegram.org |
| `PYROGRAM_SESSION` | Session string — see below. All three must be set, or the watcher stays disabled (logged loudly at startup). |

**Detection tuning** — global defaults; each is overridable per group by command

| Variable | Default | Range | Notes |
|---|---|---|---|
| `NAME_SIMILARITY_THRESHOLD` | 85 | 1-100 | Display-name fuzzy threshold |
| `USERNAME_SIMILARITY_THRESHOLD` | 88 | 1-100 | Usernames are more structured, so stricter |
| `PFP_HASH_THRESHOLD` | 10 | 0-64 | **A Hamming distance, not a percentage.** Higher is more permissive; 85 would disable photo discrimination entirely |
| `DEFAULT_BAN_SCORE` | 90 | 1-100 | At/above this, the action runs |
| `DEFAULT_ALERT_SCORE` | 78 | 1-100 | At/above this, alert only. Must not exceed the ban score |

**Cadence and pacing**

| Variable | Default | Range | Notes |
|---|---|---|---|
| `SWEEP_INTERVAL_HOURS` | 24 | 1-168 | Between automatic full sweeps |
| `SWEEP_HARD_CAP_SECONDS` | 7200 | 60-86400 | Per-group time budget. A capped run records where it stopped and resumes there next time |
| `HEALTH_CHECK_INTERVAL` | 300 | 30-86400 | MTProto session probe |
| `DB_KEEPALIVE_INTERVAL` | 270 | 30-86400 | Keeps Railway Hobby Postgres awake |
| `NAME_CHANGE_VELOCITY_THRESHOLD` | 3 | 1-100 | Renames within the window before it's suspicious |
| `NAME_CHANGE_WINDOW_MINUTES` | 60 | 1-1440 | Window for the above |
| `BIO_FETCH_MIN_INTERVAL` | 1.2 | 0-60 | Seconds between `users.GetFullUser` calls, across all callers |
| `PFP_FETCH_MIN_INTERVAL` | 0.7 | 0-60 | Seconds between profile-photo downloads |

Every numeric value is range-checked at startup. A typo or an out-of-range value
fails immediately, naming every problem at once, rather than crash-looping.

`RAILWAY_ENVIRONMENT` is set by the platform, not by you; when present the bot
emits single-line JSON logs so Railway shows real severities and `@level:error`
filtering works.

---

## Generating a Pyrogram Session String

Save this as `gen_session.py` in the project root and run it once. **It is
interactive** — Telegram will ask for the watcher account's phone number and the
login code it texts you.

```python
# gen_session.py — run once, then delete.
import asyncio
import os

from dotenv import load_dotenv          # without this, the getenv calls return None
from pyrogram import Client

load_dotenv()

async def main():
    async with Client(
        "gen",
        api_id=int(os.environ["PYROGRAM_API_ID"]),
        api_hash=os.environ["PYROGRAM_API_HASH"],
    ) as app:
        print(await app.export_session_string())

asyncio.run(main())
```

Paste the output as `PYROGRAM_SESSION`, then delete both `gen_session.py` and the
`gen.session` file it leaves behind — that file is a live credential for the
account.

The watcher account must also be a **member of each group** you want swept;
`get_chat_members` fails with `PEER_ID_INVALID` otherwise.

---

## Stack

`python-telegram-bot` v21 · `pyrogram` v2 · `psycopg` v3 · `rapidfuzz` · `imagehash` · `confusable_homoglyphs` · PostgreSQL

For full architecture, database schema, detection internals, and operational
details → see [`DOCUMENTATION.md`](DOCUMENTATION.md), which is the canonical
reference. [`INTERNAL_DOCS.md`](INTERNAL_DOCS.md) covers similar ground and is
kept for its internals detail.

# Anti-Impersonator Bot — Complete Documentation

This is the canonical reference for everything the bot does and how it works. For a short user-facing pitch see [`OVERVIEW.md`](OVERVIEW.md); for a quick-start setup, see [`README.md`](README.md).

---

## Table of Contents

1. [What the Bot Is](#1-what-the-bot-is)
2. [The Problem It Solves](#2-the-problem-it-solves)
3. [Architecture at a Glance](#3-architecture-at-a-glance)
4. [Detection Pipeline](#4-detection-pipeline)
5. [Detection Triggers](#5-detection-triggers)
6. [Action Modes (Ban / Kick / Alert)](#6-action-modes)
7. [The Whitelist](#7-the-whitelist)
8. [False Positives and the Grace Window](#8-false-positives-and-the-grace-window)
9. [Alert Buttons in the Log Channel](#9-alert-buttons-in-the-log-channel)
10. [Background Tasks](#10-background-tasks)
11. [Reporting](#11-reporting)
12. [Full Command Reference](#12-full-command-reference)
13. [Keyword Syntax](#13-keyword-syntax)
14. [Per-Group Configuration](#14-per-group-configuration)
15. [Private-Chat (DM) Workflow](#15-private-chat-dm-workflow)
16. [First-Time Setup](#16-first-time-setup)
17. [Database Schema](#17-database-schema)
18. [Caches](#18-caches)
19. [Environment Variables](#19-environment-variables)
20. [Schema Migrations](#20-schema-migrations)
21. [Limitations](#21-limitations)
22. [Troubleshooting](#22-troubleshooting)
23. [Tech Stack](#23-tech-stack)
24. [File Layout](#24-file-layout)

---

## 1. What the Bot Is

A Telegram bot that automatically detects and removes users impersonating admins, VIPs, or the group itself inside Telegram groups.

It watches four independent paths in parallel — joins, first messages, real-time profile changes, and full-group sweeps — and takes action (ban, kick, or alert) the moment a match is found. Configuration is fully per-group: every group has its own whitelist, keyword list, sensitivity, action mode, and log channel.

---

## 2. The Problem It Solves

Scammers routinely join active Telegram communities (crypto, trading, NFT, support groups) with a display name and profile photo nearly identical to a real admin's — or even copying the group's own logo. They then DM members pretending to be that admin or "official support" and drain wallets or phish credentials.

Telegram's built-in protections cover spam and basic abuse but offer **nothing** for identity impersonation: real-time profile changes can't be detected via the Bot API, and the Bot API can't enumerate all members of a supergroup. This bot fills both gaps by running a second client (a Pyrogram MTProto userbot) alongside the Bot API.

---

## 3. Architecture at a Glance

The bot is a single Python process that runs **two Telegram clients concurrently** via `asyncio.gather`:

| Client | Library | What it does |
| --- | --- | --- |
| Bot API client | `python-telegram-bot` (PTB) | Commands, join events, message scanning, banning, the inline-button workflow for log-channel alerts |
| MTProto userbot | `pyrogram` | Real-time profile-change events (`UpdateUserName` / `UpdateUserPhoto`), full group-member enumeration for sweeps |

State lives in **PostgreSQL** (psycopg v3). Hot reads (whitelist, group config, keywords, false-positive grace) are cached in-process; every cache is invalidated immediately on the relevant admin write.

The process is deployed on **Railway** with Docker (see `Dockerfile`, `start.sh`, `railway.json`). A built-in keep-alive task pings Postgres every 270 seconds so Railway's Hobby plan doesn't put the DB to sleep between bursts of activity.

---

## 4. Detection Pipeline

Every non-whitelisted user passes through a seven-stage pipeline. The pipeline **short-circuits** at the first hit and dispatches the configured action immediately.

| # | Stage | What it checks | Notes |
| --- | --- | --- | --- |
| 0 | **Keywords** | Display name, username, and bio against the group's reserved keyword/regex list | Fastest path — pure string ops, no fuzzy scoring |
| 1 | **Username similarity** | Fuzzy match (`rapidfuzz.fuzz.ratio`) of the user's username vs. every whitelisted username | Threshold defaults to 85/100, configurable per group |
| 2 | **Homoglyph username** | Unicode lookalike characters in the username (e.g. Cyrillic `а` for Latin `a`) | Only flags if the username **also** fuzzy-matches a whitelisted one — otherwise too noisy |
| 3 | **Homoglyph name** | Same idea, against display names | Skipped if either side is a single-word name (too noisy) |
| 4 | **Display name similarity** | `rapidfuzz.fuzz.token_sort_ratio` of full name vs. every whitelisted name | Single-word weak matches escalate to stage 5 |
| 5 | **Profile photo hash** | `imagehash` perceptual hash vs. stored PFP hashes | Tiebreaker only — never flags standalone, and only fired after a weak name match |
| 6 | **Group identity** | Name and PFP vs. the group's own stored title + logo hash | Catches scammers cloning the group itself (e.g. joining "Crypto Group" with the group's logo as PFP) |

The `match_type` recorded in the log can be: `keyword`, `username`, `homoglyph_username`, `homoglyph_name`, `name`, `pfp`, `group_name`, `group_pfp`, or `manual` / `manual_escalation` for human-triggered actions.

**Group sentinel users** (`GroupAnonymousBot`, the "Channel Bot" linked-channel poster) are hard-skipped — they appear with `is_bot=True` but defensive code in `_SKIP_USER_IDS` in `src/utils/checker.py` guarantees they're never flagged.

The PFP stage uses `compute_pfp_hash_bytes()` (perceptual `phash`) — small image edits and resaves still match. Hamming distance ≤ `PFP_HASH_THRESHOLD` counts as a hit.

A separate **name-change velocity** signal lives in `name_change_log`: if a user renames 3+ times in 60 minutes the watcher logs it as a flag. This does not auto-ban (no specific target is known at that point) — it just notifies the log channel.

---

## 5. Detection Triggers

The same pipeline runs from four entry points:

| Trigger | When it fires | Needs Pyrogram |
| --- | --- | --- |
| **Join** | Every time a non-bot user joins (via `ChatMemberHandler`) | No |
| **First message** | The first time a user posts in the group. After that, a `seen_members` row prevents re-scanning. | No |
| **Profile change** | Instantly when a tracked group member renames or changes their photo (`UpdateUserName` / `UpdateUserPhoto`) | **Yes** |
| **Sweep** | Full member scan every 6 hours, plus on-demand `/sweep` | **Yes** |

> **Pyrogram is optional but strongly recommended.** Without it, joins and first-message scans still work, but renames after the first message won't be caught until a manual `/sweep`, and `/sweep` itself is unavailable. To enable, set `PYROGRAM_API_ID`, `PYROGRAM_API_HASH`, and `PYROGRAM_SESSION` (generated once locally — see `README.md`).

### Scanning model

There is no "strict / relaxed" toggle. Each user is checked **once per group**, on their first message, and `seen_members` prevents re-scanning via the message handler. After that, profile changes are caught by:

- The **Pyrogram watcher** (`src/watcher/events.py`) — instantly, via raw MTProto updates. It also calls `unmark_seen()` so the next message re-validates.
- The **6-hour auto-sweep** (`src/watcher/sweep.py`) — catches anything the watcher missed, refreshes whitelist PFP hashes, and records a row in `sweep_runs` for `/stats` and the daily digest.

The legacy STRICT mode (re-check every 5 min) was removed once Pyrogram + sweeps proved sufficient.

---

## 6. Action Modes

Set per group with `/setaction`. Default is `ban`.

| Mode | What happens |
| --- | --- |
| `ban` | The user is permanently banned. They cannot rejoin. |
| `kick` | The user is removed (ban + immediate unban). They can rejoin. |
| `alert` | No action is taken automatically. The log channel is notified with action buttons so an admin can decide. |

Alert mode is the recommended way to **trial the bot in a new group** before committing to auto-bans — every detection comes with `🚫 Ban` / `👢 Kick` / `✅ Whitelist` / `🔕 Ignore (30d)` / `🗑 Dismiss` buttons.

---

## 7. The Whitelist

A group with an empty whitelist will never flag anyone via similarity matching (stages 1–6); the keyword stage still fires. The whitelist is the comparison set every detection is measured against.

| Type | How added | Notes |
| --- | --- | --- |
| **Admin (human)** | `/import_admins`, or automatically when a user is promoted to admin | Stores name, username, and PFP hash |
| **Admin (bot)** | `/import_admins` | Rose, Combot, Guardian, etc. — protects their usernames from being copied. PFP not fetched for bots. The Anti-Impersonator Bot itself is always excluded. |
| **Manual** | `/whitelist` (reply or user ID) | Any specific user. The user does **not** need to be in the chat yet — `/whitelist <id>` falls back to the Pyrogram userbot for proactive whitelisting (this absorbs what the legacy `/watch` command did). |
| **CSV import** | Send a `.csv` to the bot in DM | Bulk import. Column format matches the CSV `/listwhitelist` emits. |

Each whitelist row stores: `user_id`, `username`, `first_name`, `last_name`, `pfp_hash`, `user_type` (`admin` / `manual`), `is_bot`, `whitelisted_by`, `created_at`, `updated_at`.

### Bot identification (`is_bot`)

`is_bot` is set authoritatively from `User.is_bot` (or Pyrogram's equivalent) at write time. It's what `/listwhitelist` uses to split the **Admins / Bots / Manual** sections. Old rows that pre-date this column are backfilled on the startup migration via the legacy `username LIKE '%bot'` heuristic — `/import_admins` overwrites with the real value on its next run.

### Profile photo hashes stay fresh

After every sweep, `refresh_whitelist_pfps()` re-downloads and re-hashes the current profile photo for every whitelisted user. So legitimate photo changes by real admins propagate automatically — no manual re-import.

### Group identity protection

When the bot joins a group, or `/import_admins` runs, the group's own profile photo hash is stored alongside its title in the `groups` table. Stage 6 of detection uses these to catch scammers cloning the group itself — e.g. a user joining "Crypto Group" with the group's own banner as their PFP.

---

## 8. False Positives and the Grace Window

A confirmed false positive can be marked via the "🔓 Unban only" / "🔕 Ignore (30d)" buttons on a detection alert (or programmatically via `mark_false_positive(group_id, user_id, days=30)`).

While the grace window is active:

- `is_false_positive()` returns `True` early in the pipeline — the user is skipped before any matching runs.
- The user is **not** added to the whitelist (so a real impersonator who happens to clear once doesn't get permanent immunity).
- If the user is cleared again before the window expires, the 30-day timer resets.
- After expiry, normal detection resumes.

For users who should be permanently trusted (an admin onboarding, a known contributor), use **"✅ Unban + Whitelist"** instead — that one *does* add them to the whitelist.

---

## 9. Alert Buttons in the Log Channel

Every detection sent to the log channel ends with inline action buttons. The set depends on what the bot already did:

### After a `ban` or `kick`

| Button | Effect |
| --- | --- |
| **✅ Unban + Whitelist** | Unbans the user and adds them to the whitelist (`user_type='manual'`). Logged to `admin_actions`. |
| **🔓 Unban only** | Unbans and sets a 30-day grace window. No whitelist entry. Logged to `admin_actions`. |
| **🗑 Dismiss** | Removes the buttons from the message. The action stands. |

### After an `alert` (no automatic action)

| Button | Effect |
| --- | --- |
| **🚫 Ban** | Escalate to a permanent ban now. Logged to both `logs` (with `trigger='alert_escalation'`) and `admin_actions`. |
| **👢 Kick** | Escalate to a kick. Logged the same way. |
| **✅ Whitelist** | Add the user to the whitelist. Logged to `admin_actions`. |
| **🔕 Ignore (30d)** | Set a 30-day grace window. Logged. |
| **🗑 Dismiss** | Remove the buttons. |

Callback data format: `<action>|<group_id>|<user_id>`. Handlers live in `handle_detection_callback()` in `src/handlers/commands.py`. Registered actions: `unban_wl`, `unban_fp`, `dismiss`, `ban_now`, `kick_now`.

---

## 10. Background Tasks

All background tasks are started in `src/main.py` after the PTB updater is polling.

| Task | Schedule | Behavior |
| --- | --- | --- |
| **DB keep-alive** | Every 270 s | Runs `SELECT 1` to keep Railway Hobby Postgres awake. Always on. |
| **Daily summary** | Midnight UTC | Posts a **last-24h** activity digest (detections / bans / kicks / alerts / sweeps) per group and a grand total to the global log channel. Has a startup-grace: if booting less than an hour before midnight, the very next midnight is skipped so a fresh deploy doesn't dump a near-empty digest. |
| **Full sweep** *(Pyrogram only)* | Every 6 h | Iterates every member of every configured group via MTProto, runs the detection pipeline, and posts a per-run summary to that group's log channel. First sweep is delayed by a full interval — the bot does **not** sweep on startup. |
| **PFP refresh** *(Pyrogram only)* | After each sweep | Re-downloads and re-hashes the current PFP of every whitelisted user in the swept group. |
| **Health check** *(Pyrogram only)* | Every 5 min | Pings the Pyrogram session; auto-reconnects if it has dropped. |

### Sweep details

- A `_sweep_locks` dict prevents two concurrent sweeps on the same group.
- A 2-hour hard cap stops runaway sweeps on very large groups.
- Lazy PFP loading: photos are only fetched when there's a weak name match that needs PFP confirmation, not for every member.
- Per-member yield via `await asyncio.sleep(0)` keeps other handlers responsive during a sweep.
- Each completed sweep is recorded in `sweep_runs` (`group_id`, `iterated`, `checked`, `flagged`, `errors`, `trigger='auto'|'manual'`, `created_at`).
- `_post_sweep_summary()` writes a short report to the group's per-group log channel (or the global fallback).

### `/sweep` (manual)

Behaves identically to the auto-sweep but:

- Reports progress live in the chat message (`🔍 Sweeping… 150 seen · 120 checked · 2 flagged`).
- Records the run with `trigger='manual'`.
- Sends the summary as a reply to the admin instead of to the log channel.

---

## 11. Reporting

Two surfaces, fed by the same windowed queries:

### `/stats`

**In a group chat:** windowed breakdown for that group.
```
Action mode: ban
Similarity threshold: 85 (default)
🛡 Protected users: 14

All time    — 🚨 detections: 132 · 🚫 bans: 119 · 🧹 sweeps: 412
Last 30 days — 🚨 detections: 8   · 🚫 bans: 7   · 🧹 sweeps: 120
Last 7 days  — 🚨 detections: 1   · 🚫 bans: 1   · 🧹 sweeps: 28
```

**In private DM:** same windowed breakdown but per registered group, plus grand totals — one DB round-trip via `get_all_group_stats_windowed()`.

### `/logs`

Merged view of both detection history and admin actions (replaces the old `/logs` + `/auditlog` pair). Usage: `/logs [limit=10]` (max 50). The limit is applied **per section** — `/logs 5` returns up to 5 detections + 5 admin actions.

Both sections sort newest-first and include timestamps. Detections show: who, who they impersonated, match type, action taken. Admin actions show: who ran what, on which target, plus any free-text detail.

### Daily summary

Posted to the global log channel at midnight UTC. Shows the **last 24h** of activity:

```
📋 Daily Summary — last 24h (2026-05-28 UTC)

Across all groups: 🚨 14 detections · 🚫 11 bans · 👢 2 kicks · 🔕 1 alerts · 🧹 24 sweeps

By group:
• Crypto Trading Group — 🚨 8 · 🚫 7 · 👢 0 · 🔕 1 · 🧹 4
• NFT Collectors      — 🚨 6 · 🚫 4 · 👢 2 · 🔕 0 · 🧹 4
```

Groups with zero activity in the window are omitted to keep the digest tight.

---

## 12. Full Command Reference

All commands work both inside a group (apply to that group) and in a private DM with the bot (apply to your **active group** — see [§15 Private-Chat Workflow](#15-private-chat-dm-workflow)). Commands that mutate state require the caller to be a group admin.

### Whitelist Management

| Command | Description |
| --- | --- |
| `/import_admins` | Whitelist every current group admin (human + bot) with name, username, PFP hash. Stores the group's own profile photo for group-identity detection. Safe to re-run. |
| `/whitelist` | Reply to a message, or `/whitelist 123456`. Falls back to the Pyrogram userbot when the user isn't in the chat yet. |
| `/unwhitelist` | Reply to a message, or `/unwhitelist 123456`. |
| `/listwhitelist` | Inline list split into **Admins / Bots / Manual** sections + CSV attached. |
| `/importwhitelist` | DM the bot a `.csv` with columns `user_id, username, first_name, last_name, user_type, is_bot, created_at`. Re-runnable. |
| `/clearwhitelist confirm` | ⚠️ Remove ALL protected users for the active group. Shows a count + warning first; requires the literal word `confirm`. |

### Detection & Moderation

| Command | Description |
| --- | --- |
| `/ban` | Manual ban — reply or `/ban 123456`. Logged to `logs` and `admin_actions`. |
| `/unban 123456` | Unban a user by ID. |
| `/sweep` | Run a full member scan immediately. Requires Pyrogram. Shows live progress; auto-sweeps run every 6 h in the background. |

### Configuration

| Command | Description |
| --- | --- |
| `/setaction ban\|kick\|alert` | What happens when an impersonator is detected. Default `ban`. |
| `/setthreshold 85` | Fuzzy-match sensitivity, 50–100. Default 85. Lower = more detections (more false positives). |
| `/setlogchannel` | Pick the per-group log channel. In DM, opens a chat-picker button. In a group, accepts `/setlogchannel -100…` or `/setlogchannel clear` to fall back to the global env. |
| `/addkeyword admin, *mod*, r:official.*ceo` | Add one or more keywords. Comma-separated; supports `*` wildcards and `r:` regex. See [§13 Keyword Syntax](#13-keyword-syntax). |
| `/removekeyword admin` | Remove a single keyword by exact pattern. |
| `/listkeywords` | List all reserved keywords for the active group. |

### Reporting

| Command | Description |
| --- | --- |
| `/stats` | Windowed breakdown — see [§11 Reporting](#11-reporting). |
| `/logs [N]` | Last N detections **and** admin actions in one reply. Default 10, max 50, applied per section. |

### Removed in the latest refactor

`/watch`, `/check`, `/setmode`, `/exportwhitelist`, `/auditlog` were removed. Their behavior is covered by:

- `/watch` → folded into `/whitelist` (Pyrogram fallback handles the not-yet-in-chat case).
- `/check` → no replacement. The reporting it offered wasn't actionable; if you suspect a user, just look at their profile yourself.
- `/setmode` → no replacement. RELAXED is the only model; Pyrogram + sweep cover what STRICT added.
- `/exportwhitelist` → CSV is now attached to every `/listwhitelist` reply.
- `/auditlog` → merged into `/logs`.

---

## 13. Keyword Syntax

`/addkeyword` accepts a single command with multiple entries separated by commas. Each entry is one of:

| Form | Meaning |
| --- | --- |
| `admin` | Substring match — `admin` appears anywhere in name/username/bio. Default behavior; case-insensitive. |
| `admin*` | Starts-with `admin`. |
| `*admin` | Ends-with `admin`. |
| `*admin*` | Explicit "contains" — same as bare `admin`. |
| `r:official.*ceo` | Python regex (prefix with `r:`). Compiled with `re.IGNORECASE` and matched via `re.search`. Bad regex is rejected at add time. |

Examples:

```
/addkeyword admin, support, *mod*
/addkeyword admin*, r:official.*team
```

Each entry is processed independently — invalid entries (bad regex, DB error) are reported back as skipped while valid entries still get added.

Matching scans the user's display name, username, **and** Telegram bio (when Pyrogram is enabled — the Bot API doesn't expose bios). The first matching pattern short-circuits the pipeline at stage 0.

---

## 14. Per-Group Configuration

Every group's configuration is independent. Stored in the `groups` table:

| Setting | Default | Configured via |
| --- | --- | --- |
| `action_mode` | `ban` | `/setaction` |
| `similarity_threshold` | `85` (global default) | `/setthreshold` |
| `log_channel_id` | global `LOG_CHANNEL_ID` env | `/setlogchannel` |
| `pfp_hash` | set automatically by `/import_admins` | (not user-editable) |
| `title` | set automatically | (not user-editable) |

### Log channel resolution order

For any given alert, the bot picks the channel via:

1. The group's `log_channel_id` (if set via `/setlogchannel`).
2. The global `LOG_CHANNEL_ID` environment variable.
3. No channel → no notification (the action still happens silently).

---

## 15. Private-Chat (DM) Workflow

Every command works from a direct message with the bot — no need to type commands inside the group itself.

1. DM the bot → tap **Select Group** (a `KeyboardButtonRequestChat` picker).
2. Pick the group from the system chat-share dialog.
3. The bot auto-runs `/import_admins` on first selection.
4. All subsequent commands apply to that group until you switch.

The active group is stored in `user_data["active_group_id"]` and persisted across restarts via `PicklePersistence` (file `bot_persistence`).

**Tap "Switch Group"** to change. Each admin's active group is independent of others.

`/stats` in DM shows the per-group rollup across **every** registered group (see [§11 Reporting](#11-reporting)).

---

## 16. First-Time Setup

```
1. Add the bot to your group as admin
   → Required permission: "Ban members"

2. DM the bot → tap "Select Group" → pick your group
   → /import_admins runs automatically on first selection

3. /import_admins  (re-run if you add admins later)
   → Whitelists every admin (human + admin bots) with name + username + PFP
   → Stores the group's profile photo for group-identity detection

4. /whitelist  (reply or ID)
   → Protect any non-admin VIPs (founders, staff, influencers).
     Works for users not yet in the chat when Pyrogram is configured.

5. /addkeyword admin, support, *mod*, r:official.*team
   → Words only real admins should use in their name/username

6. /setaction ban   (default; or kick / alert)

7. /setlogchannel
   → DM and tap the channel picker; pick a private channel where
     the bot is admin. Highly recommended.

8. Done. The bot now runs autonomously:
   → Real-time profile-change monitoring (with Pyrogram)
   → 6-hour auto-sweeps
   → Daily midnight UTC summary digest
```

### Optional: enable Pyrogram for full functionality

Set `PYROGRAM_API_ID`, `PYROGRAM_API_HASH`, and `PYROGRAM_SESSION` (see [§19 Environment Variables](#19-environment-variables)). Generate the session string locally once — there's a helper script in the project root (see `README.md`). The Pyrogram session account must be a member of every group the bot watches; otherwise `pyro.get_chat_members()` raises `ChatAdminRequired` for that group.

---

## 17. Database Schema

PostgreSQL, accessed via psycopg v3 with `dict_row` row factory. All tables are created or migrated by `init_db()` on startup.

| Table | Purpose | Key columns |
| --- | --- | --- |
| `groups` | Per-group config | `group_id PK`, `title`, `action_mode`, `similarity_threshold`, `log_channel_id`, `pfp_hash` |
| `whitelisted_users` | Protected identities | `(group_id, user_id) PK`, `username`, `first_name`, `last_name`, `pfp_hash`, `user_type`, `is_bot`, `whitelisted_by` |
| `seen_members` | Who has been first-message-scanned | `(group_id, user_id) PK`, `first_seen_at`, `last_checked_at` |
| `logs` | Detection history | `log_id PK`, `group_id`, `user_id`, `username`, `full_name`, `target_user_id`, `target_name`, `detection_type`, `similarity_score`, `action_taken`, `details`, `invite_link`, `trigger`, `created_at` |
| `reserved_keywords` | Per-group keyword/regex patterns | `(group_id, pattern) UNIQUE`, `is_regex` |
| `name_change_log` | Rename velocity tracking | `user_id`, `changed_at` |
| `admin_actions` | Audit trail | `group_id`, `admin_id`, `admin_name`, `action`, `target_id`, `details`, `created_at` |
| `false_positives` | 30-day grace windows | `(group_id, user_id) PK`, `cleared_by`, `cleared_at`, `expires_at` |
| `sweep_runs` | Per-sweep results | `id PK`, `group_id`, `iterated`, `checked`, `flagged`, `errors`, `trigger`, `created_at` |

### `user_type` values

- `admin` — added via `/import_admins` or auto-promotion handler.
- `manual` — added via `/whitelist`, CSV import, or "Unban + Whitelist" button. The legacy `watch` value is rewritten to `manual` on startup migration.

### `action_taken` values in `logs`

- `banned` / `kicked` — the bot took the configured action.
- `alerted` — the bot was in alert mode and only notified.
- `ban_failed: <error>` — the ban call raised an exception (logged with full detail).
- `manual` — written by `/ban`.

### `trigger` values in `logs`

- `join` — fired from the join handler.
- `message` — fired from the first-message handler.
- `sweep` — fired from `sweep_group()`.
- `profile_change` — fired from the Pyrogram event watcher.
- `manual` — written by `/ban`.
- `alert_escalation` — written by the `ban_now` / `kick_now` callback handlers.

---

## 18. Caches

All caches live in `src/db.py` and are invalidated by their respective writer functions.

| Cache | TTL | What it holds |
| --- | --- | --- |
| Whitelist | 60 s | Full per-group whitelist (drives `is_whitelisted()` too — no separate query) |
| Group config | 5 min | `groups` row — action mode, threshold, log channel, group PFP hash |
| Reserved keywords | 5 min | Per-group keyword/regex list |
| False-positive grace | 5 min | `(group_id, user_id) → bool` |
| Admin status | 5 min | `(user_id, group_id) → is_admin` from `getChatMember`. Lives in `src/handlers/commands.py`. |
| Pyrogram entity cache | (Pyrogram-managed) | Warmed up at startup by iterating `get_dialogs()` — without this, `get_chat_members` fails with `PEER_ID_INVALID` for never-touched groups. |

Note: `get_connection()` opens a fresh psycopg connection per call. The keep-alive task ensures the DB stays warm; a connection pool is a noted potential future improvement.

---

## 19. Environment Variables

Set in Railway's "Variables" tab (or `.env` for local dev).

| Variable | Required | Description |
| --- | --- | --- |
| `BOT_TOKEN` | **Yes** | Telegram bot token from `@BotFather`. |
| `DATABASE_URL` | **Yes** | PostgreSQL connection string. Railway provides one automatically when you add the Postgres add-on. |
| `LOG_CHANNEL_ID` | Recommended | Global fallback log channel ID. Used when no per-group channel is set via `/setlogchannel`. Format: `-100…`. |
| `PYROGRAM_API_ID` | Optional* | From `my.telegram.org`. |
| `PYROGRAM_API_HASH` | Optional* | From `my.telegram.org`. |
| `PYROGRAM_SESSION` | Optional* | Pyrogram session string (generated locally once — see `README.md`). |

\* Without all three Pyrogram vars set, the bot still works via join + first-message triggers, but `/sweep`, the 6 h auto-sweep, and real-time profile-change monitoring are disabled.

`PYROGRAM_ENABLED` (in `src/config.py`) is the canonical flag — it's `True` iff all three Pyrogram vars are present and non-empty.

---

## 20. Schema Migrations

All migrations run inside `init_db()` on every boot and are idempotent. Currently active migrations:

- `ALTER TABLE groups ADD COLUMN IF NOT EXISTS action_mode TEXT NOT NULL DEFAULT 'ban';`
- `ALTER TABLE groups DROP COLUMN IF EXISTS check_mode;` *(dropped — legacy STRICT mode is gone)*
- `ALTER TABLE groups ADD COLUMN IF NOT EXISTS similarity_threshold INTEGER;`
- `ALTER TABLE groups ADD COLUMN IF NOT EXISTS pfp_hash TEXT;`
- `ALTER TABLE whitelisted_users ADD COLUMN IF NOT EXISTS user_type TEXT NOT NULL DEFAULT 'manual';`
- `UPDATE whitelisted_users SET user_type = 'manual' WHERE user_type = 'watch';` *(legacy /watch rows folded into manual)*
- `ALTER TABLE whitelisted_users ADD COLUMN IF NOT EXISTS is_bot BOOLEAN NOT NULL DEFAULT FALSE;`
- `UPDATE whitelisted_users SET is_bot = TRUE WHERE is_bot = FALSE AND lower(username) LIKE '%bot';` *(one-time backfill heuristic — overwritten with the real value on next `/import_admins`)*
- `ALTER TABLE logs ADD COLUMN IF NOT EXISTS invite_link TEXT;`
- `CREATE TABLE IF NOT EXISTS sweep_runs (…);`
- `CREATE TABLE IF NOT EXISTS false_positives (…);`
- `CREATE TABLE IF NOT EXISTS reserved_keywords (…);`
- `CREATE TABLE IF NOT EXISTS name_change_log (…);`
- `CREATE TABLE IF NOT EXISTS admin_actions (…);`
- Various `CREATE INDEX IF NOT EXISTS` statements for hot query paths.

There is no separate migration tool (Alembic, etc.) — the bot is small enough that idempotent DDL on every boot is the simplest reliable approach.

---

## 21. Limitations

- **No message-content scanning.** The bot only checks sender identity, never message text. Privacy-safe by design.
- **No global whitelist.** Each group maintains its own. There's no way to share a whitelist across groups.
- **Bio scanning requires Pyrogram.** The Bot API doesn't expose user bios — only the MTProto client can read them.
- **PFP is a tiebreaker, not a primary signal.** A matching photo alone never triggers a ban (except for group-identity matches, which also require a weak name match).
- **Seen-cache is persistent.** A user who passed their initial check won't be re-scanned via messages until either the Pyrogram watcher resets their `seen` flag (profile change) or the 6 h sweep runs.
- **Sweep requires the Pyrogram session to be a member of the target group.** The MTProto API enumerates members from a participant's perspective.
- **False-positive grace expires.** After 30 days, detection resumes. Re-clear them or whitelist them properly if needed.
- **Per-call DB connections.** Each DB-touching function opens a fresh connection. Fine at current load; future versions may add a connection pool.
- **PicklePersistence is local-disk.** If you run multiple bot instances behind a load balancer, group-picker state will desync. Single-instance deployment is assumed.

---

## 22. Troubleshooting

| Symptom | Likely cause / fix |
| --- | --- |
| Bot doesn't respond to commands | Check it has admin rights in the group. Check `BOT_TOKEN`. Check Railway logs for the startup `🟢 Anti-Impersonator Bot started` message in the log channel. |
| `/sweep` says "Sweep requires the Pyrogram watcher" | `PYROGRAM_API_ID` / `PYROGRAM_API_HASH` / `PYROGRAM_SESSION` not all set, OR the session string is invalid. Re-generate locally. |
| Sweep finds 0 members and errors | The Pyrogram session account isn't a member of that group. Add it. |
| `PEER_ID_INVALID` on first sweep | Pyrogram entity cache cold. The bot warms it up at startup via `get_dialogs()` — wait for that to finish, then retry. |
| Real admins keep getting flagged on first message | `/import_admins` hasn't been run, or threshold is too low. The bot has a safety net: if a flagged user is *currently* a group admin, it silently auto-whitelists them instead of banning. So you'd see them appear in `/listwhitelist` automatically. |
| Daily summary posts at the wrong time | The schedule is hard-coded to midnight UTC. Not currently configurable. |
| Bot ignores a known scammer | They may be on the false-positive grace list (`/logs` will show the admin who cleared them, and when). To force a re-check now, set `mark_false_positive` to a past date by re-running detection, or unwhitelist them. |
| Daily digest is empty after a deploy | Expected if the deploy was within an hour of midnight UTC — the startup-grace logic skips the imminent midnight to avoid a near-empty post. The next one fires 24h later. |
| Auto-sweep summaries don't appear | The group has no log channel set (per-group or global). Set one with `/setlogchannel`. |
| `db keep-alive: could not connect` warnings | Railway Hobby Postgres took longer than 30 s to wake up. The exponential-backoff retry should recover; persistent warnings suggest a real connectivity issue. |
| Bot's actions in `logs` show as `ban_failed: …` | The bot lacks "Ban members" permission in that group, or Telegram rate-limited the call. Fix permissions; failed bans are recorded but not retried. |

---

## 23. Tech Stack

| Library | Version | Role |
| --- | --- | --- |
| `python-telegram-bot` | v21+ | Bot API client, command handlers, message handlers, `CallbackQueryHandler`, persistence |
| `pyrogram` | v2+ | MTProto userbot — profile-change events, member enumeration |
| `psycopg` | v3 | PostgreSQL driver (synchronous; one connection per call) |
| `rapidfuzz` | latest | Fuzzy string similarity (`fuzz.ratio`, `fuzz.token_sort_ratio`) |
| `Pillow` + `imagehash` | latest | Perceptual profile-photo hashing (`phash`) |
| `confusable_homoglyphs` | latest | Unicode lookalike detection |

Python 3.11+. Deployed on Railway via Docker; see `Dockerfile`, `start.sh`, `railway.json`.

---

## 24. File Layout

```
.
├── DOCUMENTATION.md          ← this file
├── OVERVIEW.md               ← short user-facing pitch
├── README.md                 ← quick-start setup
├── INTERNAL_DOCS.md          ← technical notes (kept for historical context)
├── Dockerfile
├── docker-compose.yml
├── railway.json
├── start.sh
├── requirements.txt
├── run.py                    ← entry point: asyncio.run(main())
└── src/
    ├── main.py               ← wires PTB + Pyrogram, registers handlers, starts bg tasks
    ├── config.py             ← env-var loading + PYROGRAM_ENABLED computation
    ├── db.py                 ← all DB access + caches + migrations
    ├── handlers/
    │   ├── commands.py       ← every /command, plus handle_detection_callback
    │   ├── messages.py       ← first-message scan
    │   └── member_join.py    ← join + promotion + bot-added-to-group
    ├── utils/
    │   ├── checker.py        ← shared detection pipeline + ban_and_log
    │   ├── detector.py       ← fuzzy/homoglyph/keyword primitives
    │   └── image.py          ← perceptual PFP hashing
    └── watcher/
        ├── client.py         ← Pyrogram client factory
        ├── events.py         ← raw MTProto update handlers
        ├── sweep.py          ← sweep_group + run_periodic_sweeps + _post_sweep_summary
        ├── health.py         ← Pyrogram session health pings
        └── summary.py        ← midnight UTC daily digest
```

---

*Last updated: 2026-05-28. Tracks the codebase as of commit `6454a66` ("Streamline command surface, windowed stats, and alert-mode escalation") on `origin/main`.*

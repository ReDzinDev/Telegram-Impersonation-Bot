# Anti-Impersonator Bot — Internal Documentation

---

## What It Is

A Telegram bot that automatically detects and removes users who impersonate admins, VIPs, the group itself, or other protected identities inside Telegram groups. It monitors joins, messages, profile changes, and runs scheduled full-group sweeps — then bans, kicks, or alerts based on how each group is configured.

---

## The Problem It Solves

Scammers routinely join crypto, trading, and community Telegram groups with a display name and profile photo nearly identical to a real admin's — or even copying the group's own logo. They DM members pretending to be that admin or official support, then drain wallets or phish credentials.

Telegram's built-in tools offer no automated protection against this. The bot fills that gap.

---

## How Detection Works

Every non-whitelisted user goes through a 7-stage pipeline. It stops at the first hit and takes action immediately.

| Stage | What It Checks | Notes |
| --- | --- | --- |
| **0 — Keywords** | Display name, username, bio vs. reserved words/regex | Fastest path. Catches "Admin", "Support", "Official", custom patterns |
| **1 — Username similarity** | Fuzzy match vs. all whitelisted usernames | RapidFuzz scoring, configurable threshold (default 85/100) |
| **2 — Homoglyph username** | Unicode lookalike characters (e.g. Cyrillic `а` for Latin `a`) | Only flags if it *also* fuzzy-matches a protected username |
| **3 — Homoglyph name** | Same, for display names | Single-word names are excluded (too noisy) |
| **4 — Display name similarity** | Fuzzy match vs. all whitelisted display names | Weak single-word matches escalate to stage 5 |
| **5 — Profile photo hash** | Perceptual hash comparison vs. whitelisted user photos | Tiebreaker only for weak name matches — never flags standalone |
| **6 — Group identity** | Name + photo similarity vs. the group's own stored identity | Catches users impersonating the group itself (cloning logo/name) |

**Match types logged:** `keyword`, `username`, `homoglyph_username`, `homoglyph_name`, `name`, `pfp`, `group_name`, `group_pfp`

A separate **name-change velocity alert** fires if a user renames themselves 3+ times within 60 minutes — a common evasion tactic. It notifies the log channel but does not auto-ban (no specific impersonation target is known at that point).

---

## Detection Triggers

The bot catches impersonators through four independent paths:

| Trigger | When | Requires Pyrogram |
| --- | --- | --- |
| **Join** | Every time a non-bot user joins | No |
| **Message** | On a user's **first message** in the group | No |
| **Profile change** | Instantly when a group member renames or changes photo | **Yes** |
| **Sweep** | Full member scan every 6 hours, plus on-demand `/sweep` | **Yes** |

> **Pyrogram** is an optional MTProto user-session client. Without it, the bot still works via join and message triggers, but cannot receive real-time profile-change events or enumerate all group members for sweeps.

---

## Scanning Model

There is no "strict / relaxed" toggle anymore. Each user is checked **once per group**, on their first message, then never re-scanned via the message handler. Coverage of profile changes after that point comes from two background paths:

- The **Pyrogram watcher** catches rename / photo-change events instantly.
- The **6-hour auto-sweep** catches anything the watcher missed (and refreshes whitelist PFP hashes).

The legacy STRICT mode (re-check every 5 min) was redundant once Pyrogram + sweeps were in place and has been removed.

---

## Detection Actions

Controlled per group with `/setaction`.

| Mode | What Happens |
| --- | --- |
| **ban** (default) | User is permanently banned from the group |
| **kick** | User is removed but not banned — they can rejoin |
| **alert** | Detection is logged and the log channel is notified; no action taken |

Alert mode is useful for monitoring a new group before committing to auto-bans.

---

## The Whitelist (Protected Identities)

The bot compares every suspicious user against the group's whitelist. A group with an empty whitelist will never flag anyone.

### Who Gets Protected

| Type | How Added | Notes |
| --- | --- | --- |
| **Admin (human)** | `/import_admins` or automatic on promotion | Includes name, username, and PFP hash |
| **Admin (bot)** | `/import_admins` | Rose, Combot, and other admin bots are included — protects their usernames from being copied. PFP not fetched for bots. |
| **Manual** | `/whitelist` (reply or user ID) | Any specific user. If the user isn't in the chat yet, the Bot API lookup falls back to the Pyrogram userbot — this absorbs what the old `/watch` command did. |
| **CSV import** | Send `.csv` in private chat | Bulk import; same column format as the CSV emitted by `/listwhitelist` |

Each entry stores: user ID, username, display name, profile photo hash, who added them, when, and type.

### Admin Bot Protection

`/import_admins` now whitelists **all admin bots** in the group (e.g. Rose, Combot, Guardian) except the Anti-Impersonator Bot itself. Their usernames and display names are added to the protected identity list, so copycats like `@R0seBot` or a user named "RoseBot Admin" can be caught.

### Profile Photo Hashes

After every sweep, the bot re-downloads and re-hashes the current profile photo for every whitelisted user. This keeps stored hashes fresh even when protected users legitimately change their own photo.

### Group Identity Storage

When the bot joins a group or `/import_admins` runs, the group's own profile photo hash and name are stored. Stage 6 of detection uses these to catch users who clone the group's logo or name — e.g. a scammer joining "Crypto Group" with the group's own banner as their profile picture.

---

## Handling False Positives (and Alert-Mode Escalation)

Every detection alert in the log channel comes with action buttons. The set differs by what the bot already did:

**Ban / Kick mode** — bot already removed the user:

| Button | What It Does |
| --- | --- |
| **✅ Unban + Whitelist** | Unbans and adds the user to the whitelist. Use when the person is legitimately trusted. |
| **🔓 Unban only** | Unbans with a **30-day grace period** (no whitelist add). Won't be re-flagged during the window. |
| **🗑 Dismiss** | Just removes the buttons. The ban/kick stands. |

**Alert mode** — bot only notified, took no action:

| Button | What It Does |
| --- | --- |
| **🚫 Ban** | Escalate to a permanent ban now. |
| **👢 Kick** | Escalate to a kick (user can rejoin). |
| **✅ Whitelist** | Mark the user as trusted. |
| **🔕 Ignore (30d)** | 30-day grace window — won't re-fire. |
| **🗑 Dismiss** | Acknowledge without doing anything. |

The 30-day grace resets if the same user is cleared again before it expires.

---

## Admin Action Audit Trail

Every mutating command is recorded in the `admin_actions` table with who ran it, when, and on which target. Recent entries are shown alongside detection history in `/logs`.

Tracked actions: `whitelist`, `unwhitelist`, `ban`, `kicked`, `banned`, `import_admins`, `setaction`, `setthreshold`, `importwhitelist`, `clearwhitelist`.

---

## Log Channel Alerts

When a detection fires, the bot posts a structured alert to the configured log channel:

```
🚨 Impersonation Detected

Group ID: -1001234567890
User: John Smíth (@johnsmith99) | ID: 987654321
Impersonating: John Smith (ID: 111222333)
Method: name
Match: John Smith
Score: 94.3
Trigger: join
Invite link: https://t.me/+abc123
Action: banned
```

For group-identity matches, `Impersonating` shows `[Group] GroupName` instead of a user.

### Log Channel Priority

The bot resolves the log channel in this order:
1. Per-group channel set via `/setlogchannel`
2. Global `LOG_CHANNEL_ID` environment variable
3. No logging (silent)

---

## Per-Group Configuration Reference

| Setting | Command | Default | Options / Notes |
| --- | --- | --- | --- |
| Detection action | `/setaction` | `ban` | `ban` · `kick` · `alert` |
| Fuzzy threshold | `/setthreshold 85` | `85` | 50–100. Lower = more sensitive |
| Log channel | `/setlogchannel` | Global env | DM the bot to get a channel picker; `/setlogchannel clear` removes override |
| Reserved keywords | `/addkeyword admin` | None | Supports comma lists, `*` wildcards, and `r:regex` (see below) |

### Keyword syntax

`/addkeyword` accepts multiple entries in one command, separated by commas. Each entry is one of:

| Form | Matches |
| --- | --- |
| `admin` | Substring — `admin` appears anywhere in name/username/bio |
| `admin*` | Starts-with `admin` |
| `*admin` | Ends-with `admin` |
| `*admin*` | Explicit "contains" — same as bare `admin` |
| `r:official.*ceo` | Python regex (`re.IGNORECASE`) |

Examples:
```
/addkeyword admin, support, *mod*
/addkeyword admin*, r:official.*team
```

---

## Full Command Reference

### Whitelist Management

| Command | Description |
| --- | --- |
| `/import_admins` | Whitelist all current group admins — human and bot alike — with name, username, and PFP hash. Also stores the group's own profile photo for group-identity detection. |
| `/whitelist` | Whitelist a user — reply to their message, or `/whitelist 123456`. Falls back to the Pyrogram userbot when the user isn't in the chat yet (covers what the old `/watch` did). |
| `/unwhitelist` | Remove from whitelist — reply or `/unwhitelist 123456` |
| `/listwhitelist` | Show all protected users sectioned by **Admins / Bots / Manual**, and attach a CSV export of the whitelist in the same reply |
| `/importwhitelist` | Send a CSV file in the bot's DM to bulk-add users |
| `/clearwhitelist confirm` | ⚠️ Remove ALL protected users — shows count warning first, requires `confirm` |

### Detection & Moderation

| Command | Description |
| --- | --- |
| `/sweep` | Run a full member scan immediately (Pyrogram required). Shows live progress; auto-sweeps also post a summary to the log channel every 6 h. |
| `/ban` | Manually ban a user — reply or `/ban 123456` |
| `/unban 123456` | Unban a user by ID |

### Configuration

| Command | Description |
| --- | --- |
| `/setaction ban\|kick\|alert` | Set what happens when an impersonator is detected |
| `/setthreshold 85` | Set fuzzy-match sensitivity (50–100, default 85) |
| `/setlogchannel` | Pick the per-group log channel (or `/setlogchannel clear` / `/setlogchannel -100…`) |
| `/addkeyword admin, *mod*, r:official.*ceo` | Add one or more keywords (wildcards + regex supported — see Keyword syntax) |
| `/removekeyword admin` | Remove a reserved keyword |
| `/listkeywords` | List all reserved keywords for this group |

### Reporting

| Command | Description |
| --- | --- |
| `/stats` | In a group: all-time / 30d / 7d breakdown of detections, bans, sweeps. In private DM: per-group rollup across every registered group. |
| `/logs 20` | Recent detections AND admin actions in one reply (replaces the old `/logs` + `/auditlog` pair) |

---

## Private Chat (DM) Workflow

All commands work from a direct message with the bot — no need to use them inside the group.

1. DM the bot → tap **Select Group** (or **Switch Group** to change)
2. Pick the group from the chat picker
3. All subsequent commands apply to that group until you switch

This lets group admins manage everything privately without posting commands in the group chat. `/stats` in DM shows a breakdown of **all registered groups** at once.

---

## First-Time Setup (Step by Step)

```
1. Add the bot to your group as admin
   → Must have "Ban members" permission

2. DM the bot → tap "Select Group" → pick your group
   → The bot auto-runs /import_admins on first selection

3. /import_admins (re-run if needed)
   → Whitelists all current admins (human + bots) with name + username + PFP hash
   → Stores the group's profile photo for group-identity detection

4. /whitelist <reply or ID>
   → Protect any non-admin VIPs (founders, staff, influencers).
     Works even for users who haven't joined the group yet
     when the Pyrogram userbot is configured.

5. /addkeyword admin, support, *mod*, r:official.*team
   → Add words only real admins should have in their name.
     Supports comma lists, * wildcards, and r:regex.

6. /setaction ban
   → Default is already ban; confirm or switch to kick / alert

7. /setlogchannel -1001234567890
   → Recommended: point to a private channel for detection alerts

8. Done — the bot monitors joins and messages automatically
   → Enable Pyrogram env vars for real-time profile-change detection + sweeps
```

---

## Architecture

The bot runs two clients simultaneously:

| Client | Role |
| --- | --- |
| **python-telegram-bot (PTB)** | Handles all commands, member-join events, and message scanning via the Telegram Bot API |
| **Pyrogram (MTProto)** | User-session client; receives raw profile-change events and enumerates all group members for sweeps — both impossible via the Bot API |

State is stored in **PostgreSQL**. Frequently-read data is cached in memory:

| Cache | TTL | What |
| --- | --- | --- |
| Whitelist | 60 s | Per-group list of protected users |
| Group config | 5 min | Action, threshold, log channel, group PFP hash |
| Reserved keywords | 5 min | Per-group keyword/regex list |
| Admin status | 5 min | Per-(user, group) admin check result — eliminates repeated `getChatMember` API calls |
| False-positive grace | 5 min | Per-(user, group) clearance status |

All caches are invalidated immediately on the relevant admin write (e.g. `/setaction` invalidates the group config cache).

### Background Tasks (Pyrogram only)

| Task | Schedule |
| --- | --- |
| **Full sweep** | Every 6 hours (first sweep is delayed — no startup sweep). Each run is recorded in `sweep_runs` and posts a short summary to the group's log channel. |
| **PFP hash refresh** | After every sweep (keeps stored hashes current) |
| **Health check** | Every 5 minutes; auto-reconnects if the Pyrogram session drops |
| **Daily summary** | Midnight UTC; posts a **last-24h** activity digest (detections / bans / kicks / alerts / sweeps) to the log channel. Use `/stats` for cumulative + windowed numbers. |

### Sweep Behaviour

- Sweep is **non-blocking**: shows live progress in the status message (`🔍 Sweeping… 150 seen · 120 checked · 2 flagged`)
- Uses **lazy PFP loading**: profile photos are only downloaded when there's a weak name match that needs confirmation, not for every member
- The sweep lock prevents two concurrent sweeps on the same group
- A 2-hour hard cap stops runaway sweeps on very large groups

---

## Database Schema

| Table | Purpose |
| --- | --- |
| `groups` | Per-group config: action, threshold, log channel, **group PFP hash** |
| `whitelisted_users` | Protected identities with name, username, PFP hash, type (`admin` / `manual`) |
| `seen_members` | Tracks who has been checked (one entry per user per group) |
| `logs` | Detection history: who, what, score, action, trigger, invite link |
| `reserved_keywords` | Per-group keyword/regex patterns |
| `name_change_log` | Timestamps for rename velocity tracking |
| `admin_actions` | Audit trail of every admin command |
| `false_positives` | 30-day grace windows for manually-cleared users |
| **`sweep_runs`** | One row per sweep_group() call — feeds /stats sweep counts and the daily digest |

A startup migration drops the legacy `check_mode` column on `groups` and rewrites any `user_type='watch'` rows to `'manual'` (the two were behaviourally identical).

---

## Environment Variables

| Variable | Required | Description |
| --- | --- | --- |
| `BOT_TOKEN` | **Yes** | Telegram bot token from @BotFather |
| `DATABASE_URL` | **Yes** | PostgreSQL connection string |
| `LOG_CHANNEL_ID` | Recommended | Global fallback log channel ID |
| `PYROGRAM_API_ID` | Optional* | From my.telegram.org |
| `PYROGRAM_API_HASH` | Optional* | From my.telegram.org |
| `PYROGRAM_SESSION` | Optional* | Base64-encoded Pyrogram session string |

*Without Pyrogram vars, the bot still works but profile-change monitoring and full sweeps are disabled.

---

## Known Limitations

- **No message content scanning** — the bot only checks sender identity, never message text (privacy-safe)
- **No global whitelist** — each group maintains its own independent whitelist
- **Bio scanning only on profile changes** — not during sweeps (MTProto `GetFullUser` is too expensive per member at scale)
- **PFP is a tiebreaker, not a primary signal** — a matching photo alone never triggers a ban (except for group-identity matches where a weak name match is also present)
- **Seen-cache is persistent** — a user who passed their initial check won't be re-scanned by messages until the Pyrogram watcher resets the seen flag (profile change) or the 6 h sweep runs
- **Sweep requires the Pyrogram session to be a member of the group** — the session account must be in the group to enumerate members
- **False-positive grace expires** — after 30 days, detection resumes for cleared users. Re-clear them or whitelist them if needed.

---

## Tech Stack

| Library | Version | Role |
| --- | --- | --- |
| `python-telegram-bot` | v21+ | Bot API client, handlers, persistence |
| `pyrogram` | v2+ | MTProto client |
| `psycopg` | v3 | PostgreSQL (synchronous, connection-per-call) |
| `rapidfuzz` | latest | Fuzzy string similarity |
| `imagehash` + `Pillow` | latest | Perceptual profile photo hashing |
| `confusable_homoglyphs` | latest | Unicode homoglyph detection |

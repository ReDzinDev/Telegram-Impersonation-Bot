# Anti-Impersonator Bot — What It Does

---

## The Problem

In any active Telegram community, scammers regularly join the group pretending to be an admin or official team member. They copy the name, profile photo, and even the group's own logo — then privately message members offering "support", fake investments, or account recovery. By the time anyone notices, wallets have been drained.

Telegram has no built-in protection for this. The bot fills that gap automatically.

---

## What the Bot Does

The bot watches your group 24/7 and acts the moment it detects a fake:

- **Someone joins with a name too similar to an admin** → instantly banned
- **Someone copies an admin's profile photo** → caught and removed
- **Someone uses your group's logo as their avatar** → flagged as brand impersonation
- **Someone renames themselves mid-conversation to look like staff** → detected in real time
- **Someone uses forbidden words like "Admin", "Support", "Official" in their name** → blocked on entry

No manual review needed. The bot handles it before the scammer can send a single message.

---

## How It Protects Your Group

| Scenario | What Happens |
|---|---|
| Fake admin joins | Banned before they can contact anyone |
| Existing member renames to look like staff | Caught immediately via profile monitoring |
| Scammer copies the group's logo | Flagged as group brand impersonation |
| "Support" account slides into DMs | Blocked at the door |
| False positive (real user wrongly flagged) | Admin clicks one button to reverse it |

---

## What You Control

Every group has its own settings. You decide:

- **Ban, kick, or just alert** — auto-ban is default; alert mode lets you review first
- **How strict the name matching is** — tune sensitivity from broad to narrow
- **Which words are off-limits** — reserve "Admin", "CEO", "Official", or any custom pattern
- **Where alerts go** — a private log channel only your team sees

---

## The Log Channel

Every detection sends a notification to your private log channel with full context: who was caught, what they were imitating, how confident the match was, and what action was taken.

Your team can react directly from that message:

- **Unban + Whitelist** — it was a false alarm; clear them permanently
- **Unban (30-day grace)** — unban without adding to the protected list
- **Dismiss** — confirmed scammer; keep the ban

---

## Zero Ongoing Work

Once set up, the bot runs itself:

- **Monitors every new member** that joins
- **Scans all existing members** every 6 hours automatically
- **Catches renames and photo changes** the moment they happen
- **Sends a daily summary** of detections and bans to your log channel
- **Auto-protects newly promoted admins** the moment they're given admin rights — no manual update needed

---

## Multi-Group Support

One bot instance covers unlimited groups. Each group has completely independent settings, whitelists, and log channels. Manage everything from a private conversation with the bot — no need to post commands in the group chat.

---

## Quick Start

1. Add the bot to your group as admin
2. Message the bot privately → select your group
3. Run `/import_admins` — the bot learns who your real admins are
4. Add any forbidden words (`/addkeyword Admin`)
5. Point it at a private log channel
6. Done

Full technical documentation: [`INTERNAL_DOCS.md`](INTERNAL_DOCS.md)

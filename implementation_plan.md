# Implementation Plan - Anti-Impersonation Bot

## Overview
Develop a Python-based Telegram bot to detect user impersonation in real-time. This involves user monitoring, fuzzy string matching for names, and perceptual hashing for profile pictures.

## Step 1: Initialize Project
- [ ] Create Python virtual environment and `requirements.txt`.
- [ ] Set up `.env` for `BOT_TOKEN`, `DB_URL` and `LOG_CHANNEL_ID`.
- [ ] Create initial project structure (`src/`, `handlers/`, `utils/`).
- [ ] Implement `src/config.py` using `pydantic-settings` or simple env var loading.
- [ ] Setup PostgreSQL connection using `psycopg` (v3).

## Step 2: Database Schema
- [ ] Create `init_db.sql` script following `postgresql` skill guidelines.
- [ ] Define `whitelisted_users` table with proper indexes on `username` and `pfp_hash`.
- [ ] Define `logs` table for tracking detections.
- [ ] Implement database migration/initialization logic in `src/database.py`.

## Step 3: Core Logic - Whitelist Management
- [ ] Implement image hashing utility (`src/utils/image_utils.py`):
    - Download user PFP -> Convert to Grayscale -> Resize -> Compute Average/dHash -> Store Hex.
- [ ] Create admin command `/import_admins`:
    - Fetch chat administrators.
    - Extract `username` and `full_name`.
    - Download and hash current PFP (if exists).
    - Insert/Upsert into `whitelisted_users`.
- [ ] Create manual command `/whitelist <reply>`:
    - Add replied user to whitelist.

## Step 4: Core Logic - Impersonation Detection
- [ ] Implement string matching utility (`src/utils/detector.py`):
    - Use `rapidfuzz` (faster than `fuzzywuzzy`) for Levenshtein distance on names/usernames.
    - Define thresholds (e.g., >85% similarity).
- [ ] Implement PFP matching utility:
    - Compare new user's PFP hash with all hashes in DB using Hamming distance.
    - Define threshold (e.g., <5 changes).
- [ ] Create `chat_member` update handler (`src/handlers/member_join.py`):
    - Trigger on new chat members.
    - Run name check against ALL whitelisted users.
    - Run PFP check against ALL whitelisted users.
    - Ban user if match found and log action.
    - Send alert to log channel.

## Step 5: Integration & Testing
- [ ] Combine handlers into main bot instance.
- [ ] Test `/import_admins` in a test group.
- [ ] Test join detection by attempting to join with a similar name/pfp (using alt account if possible, or simulate event).
- [ ] Verify database logging.

## Step 6: Polish
- [ ] Add `/stats` command.
- [ ] Add `/config` to adjust thresholds dynamically (optional).
- [ ] Final code review and cleanup.

## User Commands
- `/start` - Check bot status.
- `/import_admins` - Automatically whitelist all current admins.
- `/whitelist` - Whitelist a specific user.
- `/unwhitelist` - Remove a user from whitelist.
- `/check_user` - Manually run check on a user (reply).

# Task: Anti-Impersonation Telegram Bot

## Context
The user wants a powered Anti-Impersonation Telegram bot written in Python using PostgreSQL. The bot needs to detect and ban users who impersonate admins or whitelisted users via username, nickname, or profile picture.

## Requirements

### Core Features
1.  **Impersonation Detection**:
    - Monitor `chat_member` updates for new users.
    - Compare new user's `username`, `first_name`, `last_name` against a whitelist using fuzzy matching.
    - Compare new user's profile picture against whitelisted users' profile pictures using perceptual hashing.
    - **Action**: Ban detected impersonators immediately and log the event.
2.  **Whitelist Management**:
    - Store whitelisted users (Admins + Manual additions) in PostgreSQL.
    - Store `user_id`, `username`, `display_name`, and `pfp_hash` for each whitelisted user.
    - Command `/import_admins`: Fetch all current group admins and add them to the whitelist.
    - Command `/whitelist <user_id|reply>`: Manually whitelist a user.
3.  **Logging**:
    - Log actions (bans, detections) to a specified database table and optionally a log channel.

### Tech Stack
-   **Language**: Python
-   **Database**: PostgreSQL
-   **Libraries**:
    -   `python-telegram-bot` (Bot Framework)
    -   `psycopg` (PostgreSQL driver)
    -   `imagehash` & `Pillow` (Image processing)
    -   `rapidfuzz` or `Levenshtein` (String similarity)

### Capabilities (from Skills)
-   Use `telegram-bot-builder` patterns for structure and commands.
-   Use `postgresql` best practices for schema design (indexes, types).

## Architecture

### Database Schema
-   **`whitelisted_users`**:
    -   `user_id` (BIGINT, PK)
    -   `username` (TEXT)
    -   `display_name` (TEXT)
    -   `pfp_hash` (TEXT) - Hex string of the image hash
    -   `updated_at` (TIMESTAMPTZ)
-   **`logs`**:
    -   `log_id` (BIGINT GENERATED ALWAYS AS IDENTITY, PK)
    -   `user_id` (BIGINT) - The offender
    -   `matched_with_id` (BIGINT) - Who they impersonated
    -   `match_type` (TEXT) - 'username', 'name', 'pfp'
    -   `score` (FLOAT) - Similarity score
    -   `action_taken` (TEXT) - 'banned', 'monitor'
    -   `created_at` (TIMESTAMPTZ)

### Project Structure
```
anti_impersonator/
├── src/
│   ├── main.py
│   ├── config.py
│   ├── database.py
│   ├── services/
│   │   ├── detector.py      # Core logic for similarity
│   │   ├── image_utils.py   # Hashing logic
│   ├── handlers/
│   │   ├── admin.py
│   │   ├── member_join.py
│   └── utils/
├── .env
├── requirements.txt
```

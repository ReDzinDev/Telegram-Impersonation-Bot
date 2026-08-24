# BotFather profile assets

Everything here is the public-facing text and imagery for the bot's Telegram
profile. Character counts are checked against BotFather's limits by
`tests/test_branding.py`, so edit the files rather than pasting ad-hoc text.

| File | BotFather field | Limit | Current |
|---|---|---|---|
| `botfather_name.txt` | Name | 64 | 14 |
| `botfather_about.txt` | About (shown on the profile / before you press Start) | 120 | 104 |
| `botfather_description.txt` | Description (shown in the empty chat) | 512 | 496 |
| `description_picture.png` | Description picture (shown above the description) | 640x360 | 640x360 |
| `../PRIVACY.md` | Privacy Policy (a URL) | — | — |

## How to apply them

Open [@BotFather](https://t.me/BotFather) and run these, picking the bot when
asked. Paste the file contents exactly.

```
/setname
/setabouttext
/setdescription
```

The description picture and the privacy policy have no slash command — they live
in the menus:

- **Picture:** `/mybots` -> your bot -> *Edit Bot* -> *Edit Description Picture*,
  then upload `description_picture.png`.
- **Privacy policy:** `/mybots` -> your bot -> *Bot Settings* -> *Privacy Policy*,
  then send this URL:

  ```
  https://github.com/ReDzinDev/Telegram-Impersonation-Bot/blob/main/PRIVACY.md
  ```

  Fill in the operator name, contact and date placeholders in `PRIVACY.md` and
  push before sending the URL. The repo is public, so the link resolves for
  users; if it is ever made private, host the policy somewhere else first.

## Two settings that are not cosmetic

- **Group privacy mode must be disabled.** `/setprivacy` -> *Disable*. The bot
  triggers a profile check when a member posts for the first time
  (`main.py` registers a `MessageHandler` on ordinary group text), and privacy
  mode withholds those updates. The bot never inspects the message text — see
  `PRIVACY.md`.
- **Command list:** `/setcommands`, using the block the bot prints at startup
  (`BOT_COMMANDS` in `src/main.py` is the single source of truth).

## Wording notes

The copy deliberately avoids promising detection rates or calling the bot
"AI-powered". It names the three things it compares (name, @handle, photo), the
evasions it folds (Cyrillic lookalikes, small caps, spacing, l33t), and the fact
that the admin chooses between ban, kick and alert-only. Claims here should stay
things the test suite actually pins.

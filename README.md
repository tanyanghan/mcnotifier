# mcnotifier

A Telegram bot that monitors a Minecraft server and sends notifications when players join or leave. Also responds to commands for player status and stats.

## Files

| File | Description |
|------|-------------|
| `bot.py` | Main bot — log watcher, Telegram handlers, stats |
| `requirements.txt` | Python dependencies |
| `.env.example` | Template for required environment variables |
| `.gitignore` | Excludes secrets, runtime state, and virtualenv |

## Setup

1. **Install dependencies**
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **Configure environment**
   ```bash
   cp .env.example .env
   ```
   Edit `.env` and fill in:
   - `BOT_TOKEN` — from [@BotFather](https://t.me/BotFather)
   - `MINECRAFT_DIR` — absolute path to the Minecraft server directory (e.g. `/home/user/Minecraft`)

3. **Run**
   ```bash
   python bot.py
   ```

## First-time authorisation

1. Send any private message to the bot — the first sender becomes the **admin**.
2. Add the bot to your group chat, then send `/chat_id` in the group to get its ID.
3. In a private message to the bot, send `/authorize <chat_id>` to whitelist the group.

The bot will now send join/leave notifications to all authorised chats and respond to commands there.

## Commands

| Command | Description |
|---------|-------------|
| `/status` | Show currently online players |
| `/list` | List all players found in the stats directory |
| `/stats [player]` | Full stats for one or all players |
| `/playtime` | Playtime leaderboard |
| `/chat_id` | Show the current chat's ID |
| `/authorize <chat_id>` | *(Admin)* Whitelist a group chat |
| `/revoke <chat_id>` | *(Admin)* Remove a group from the whitelist |
| `/listchats` | *(Admin)* List all authorised chat IDs |

## Runtime state

The bot writes the following at runtime (all excluded from git):

- `auth.json` — admin user ID and authorised chat list
- `player_names.json` — UUID → username mappings learned from server logs
- `logs/log_<YYYYMMDD_HHMMSS>.txt` — a new log file is created each time the bot starts

Delete `auth.json` and `player_names.json` to reset the bot to a fresh state.

## Logging

Each run produces a timestamped log file under `logs/`. Logged events include:

- Player join/leave notifications (player name, online/offline, number of chats notified)
- All command requests (command name and Telegram username of requester)
- Admin actions (claim, authorize, revoke)
- Player registry updates (new UUID→name mappings and renames)
- Minecraft log file rotation detection
- Errors and exceptions

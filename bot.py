import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
import telebot
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# ---------------------------------------------------------------------------
# 1. Config
# ---------------------------------------------------------------------------
load_dotenv(Path(__file__).parent / ".env")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
MINECRAFT_DIR = os.environ.get("MINECRAFT_DIR")

missing = [k for k, v in {"BOT_TOKEN": BOT_TOKEN, "MINECRAFT_DIR": MINECRAFT_DIR}.items() if not v]
if missing:
    raise EnvironmentError(f"Missing required environment variables: {', '.join(missing)}")

LOG_PATH = Path(MINECRAFT_DIR) / "logs" / "latest.log"
STATS_DIR = Path(MINECRAFT_DIR) / "world" / "stats"

# ---------------------------------------------------------------------------
# Logging setup (configured in main, used everywhere via module-level logger)
# ---------------------------------------------------------------------------
logger = logging.getLogger("mcnotifier")


def setup_logging(logs_dir: Path) -> None:
    logs_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = logs_dir / f"log_{timestamp}.txt"

    fmt = logging.Formatter("%(asctime)s  %(name)-12s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)

    logger.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

    telebot_logger = logging.getLogger("TeleBot")
    telebot_logger.addHandler(file_handler)

    logger.info("Logging started — writing to %s", log_file)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _tg_user(message) -> str:
    """Return a readable identifier for the Telegram sender."""
    u = message.from_user
    if u is None:
        return "unknown"
    return f"@{u.username}" if u.username else (u.full_name or str(u.id))


# ---------------------------------------------------------------------------
# 2. Player Name Registry
# ---------------------------------------------------------------------------
_NAMES_PATH = Path(__file__).parent / "player_names.json"
_names_lock = threading.Lock()


def load_player_names(path: Path) -> dict:
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            logger.exception("Failed to load player_names.json")
    return {}


def _save_player_names(names: dict, path: Path) -> None:
    with open(path, "w") as f:
        json.dump(names, f, indent=2)


def register_player(uuid: str, name: str, names: dict, path: Path) -> None:
    if names.get(uuid) == name:
        return
    with _names_lock:
        old = names.get(uuid)
        names[uuid] = name
        _save_player_names(names, path)
    if old:
        logger.info("Player registry: UUID %s renamed %s -> %s", uuid, old, name)
    else:
        logger.info("Player registry: registered %s (%s)", name, uuid)


# ---------------------------------------------------------------------------
# 3. Online Players State
# ---------------------------------------------------------------------------
_online_players: set = set()
_online_lock = threading.Lock()


def player_join(name: str) -> None:
    with _online_lock:
        _online_players.add(name)


def player_leave(name: str) -> None:
    with _online_lock:
        _online_players.discard(name)


def get_online_players() -> list:
    with _online_lock:
        return sorted(_online_players)


# ---------------------------------------------------------------------------
# 4. Log Parsing
# ---------------------------------------------------------------------------
RE_JOIN = re.compile(r'^\[[\d:]+\] \[Server thread/INFO\]: (\w+) joined the game')
RE_LEAVE = re.compile(r'^\[[\d:]+\] \[Server thread/INFO\]: (\w+) left the game')
RE_UUID = re.compile(r'^\[[\d:]+\] \[User Authenticator #\d+/INFO\]: UUID of player (\w+) is ([0-9a-f-]+)')


_pending_uuids: dict = {}  # name -> uuid, populated by UUID line, consumed by join line


def parse_line(line: str, names: dict) -> tuple:
    """Return (event_type, player_name) or (None, None)."""
    line = line.strip()

    m = RE_UUID.match(line)
    if m:
        name, uuid = m.group(1), m.group(2)
        _pending_uuids[name] = uuid
        register_player(uuid, name, names, _NAMES_PATH)
        return None, None

    m = RE_JOIN.match(line)
    if m:
        name = m.group(1)
        uuid = _pending_uuids.pop(name, None)
        if uuid:
            register_player(uuid, name, names, _NAMES_PATH)
        player_join(name)
        return "join", name

    m = RE_LEAVE.match(line)
    if m:
        name = m.group(1)
        player_leave(name)
        return "leave", name

    return None, None


# ---------------------------------------------------------------------------
# 5. LogWatcher
# ---------------------------------------------------------------------------
class LogWatcher(FileSystemEventHandler):
    def __init__(self, log_path: Path, names: dict, notify_cb):
        self._path = log_path
        self._names = names
        self._notify = notify_cb
        self._pos = 0
        self._inode = None
        self._lock = threading.Lock()
        self._seek_to_end()

    def _seek_to_end(self) -> None:
        try:
            stat = self._path.stat()
            self._inode = stat.st_ino
            self._pos = stat.st_size
        except FileNotFoundError:
            logger.warning("Log file not found at startup: %s (server may be offline)", self._path)

    def _check_rotation(self) -> bool:
        try:
            inode = self._path.stat().st_ino
            if inode != self._inode:
                self._inode = inode
                self._pos = 0
                logger.info("Log file rotation detected, resetting position")
                return True
        except FileNotFoundError:
            pass
        return False

    def on_modified(self, event):
        if not str(event.src_path).endswith("latest.log"):
            return
        with self._lock:
            self._check_rotation()
            try:
                with open(self._path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(self._pos)
                    new_data = f.read()
                    self._pos = f.tell()
                for line in new_data.splitlines():
                    event_type, name = parse_line(line, self._names)
                    if event_type and name:
                        self._notify(event_type, name)
            except FileNotFoundError:
                pass
            except Exception:
                logger.exception("Error reading Minecraft log")


# ---------------------------------------------------------------------------
# 6. Notification Callback
# ---------------------------------------------------------------------------
def make_notify_callback(bot: telebot.TeleBot, auth: dict):
    _last_event: dict = {}
    _cooldown = 3

    def notify(event_type: str, name: str) -> None:
        key = f"{name}-{event_type}"
        now = time.time()
        if now - _last_event.get(key, 0) < _cooldown:
            return
        _last_event[key] = now

        online = get_online_players()
        count = len(online)
        names_str = ", ".join(online) if online else "none"

        verb = "joined the game" if event_type == "join" else "left the game"
        status = "online" if event_type == "join" else "offline"
        msg = f"{name} {verb}\nPlayers online: {count} ({names_str})"

        chat_ids = auth.get("authorized_chat_ids", [])
        logger.info("Notification: player %s %s — sending to %d chat(s)", name, status, len(chat_ids))
        for chat_id in chat_ids:
            try:
                bot.send_message(chat_id, msg)
            except Exception:
                logger.exception("Failed to send notification to chat %s", chat_id)

    return notify


# ---------------------------------------------------------------------------
# 7. Stats Logic
# ---------------------------------------------------------------------------
def _ticks_to_hours(ticks: int) -> float:
    return round(ticks / 20 / 3600, 2)


def _cm_to_km(cm: int) -> float:
    return round(cm / 100000, 2)


def read_player_stats(stats_dir: Path, names: dict) -> list:
    if not stats_dir.exists():
        return []
    result = []
    for stat_file in stats_dir.glob("*.json"):
        try:
            with open(stat_file) as f:
                data = json.load(f)
        except Exception:
            logger.exception("Failed to read stats file %s", stat_file)
            continue
        uuid = stat_file.stem
        name = names.get(uuid, uuid)
        stats = data.get("stats", {})
        custom = stats.get("minecraft:custom", {})
        mined = stats.get("minecraft:mined", {})
        killed = stats.get("minecraft:killed", {})

        distance_cm = (
            custom.get("minecraft:walk_one_cm", 0)
            + custom.get("minecraft:sprint_one_cm", 0)
            + custom.get("minecraft:swim_one_cm", 0)
            + custom.get("minecraft:fly_one_cm", 0)
        )
        diamonds = (
            mined.get("minecraft:diamond_ore", 0)
            + mined.get("minecraft:deepslate_diamond_ore", 0)
        )

        result.append({
            "name": name,
            "time_played_hours": _ticks_to_hours(custom.get("minecraft:play_time", 0)),
            "deaths": custom.get("minecraft:deaths", 0),
            "diamonds_mined": diamonds,
            "ancient_debris_mined": mined.get("minecraft:ancient_debris", 0),
            "distance_travelled_km": _cm_to_km(distance_cm),
            "villager_trades": custom.get("minecraft:traded_with_villager", 0),
            "total_mobs_killed": sum(killed.values()) if killed else 0,
        })
    return result


def _format_stats(p: dict) -> str:
    return (
        f"Player: {p['name']}\n"
        f"  Time played: {p['time_played_hours']}h\n"
        f"  Deaths: {p['deaths']}\n"
        f"  Diamonds mined: {p['diamonds_mined']}\n"
        f"  Ancient debris mined: {p['ancient_debris_mined']}\n"
        f"  Distance travelled: {p['distance_travelled_km']} km\n"
        f"  Villager trades: {p['villager_trades']}\n"
        f"  Mobs killed: {p['total_mobs_killed']}"
    )


# ---------------------------------------------------------------------------
# 8. Authorization System
# ---------------------------------------------------------------------------
_AUTH_PATH = Path(__file__).parent / "auth.json"
_auth_lock = threading.Lock()


def load_auth(path: Path) -> dict:
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            logger.exception("Failed to load auth.json")
    return {"admin_user_id": None, "authorized_chat_ids": []}


def save_auth(auth: dict, path: Path) -> None:
    with open(path, "w") as f:
        json.dump(auth, f, indent=2)


def is_admin(user_id: int, auth: dict) -> bool:
    return auth.get("admin_user_id") == user_id


def is_authorized(chat_id: int, auth: dict) -> bool:
    if auth.get("admin_user_id") is not None and chat_id == auth["admin_user_id"]:
        return True
    return chat_id in auth.get("authorized_chat_ids", [])


def _guard(message, auth: dict) -> bool:
    """Return True if the message should be processed."""
    chat_type = message.chat.type
    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else None

    if chat_type == "private":
        admin_id = auth.get("admin_user_id")
        if admin_id is None:
            return True  # unclaimed — allow for admin claim
        return user_id == admin_id

    if chat_type in ("group", "supergroup"):
        return chat_id in auth.get("authorized_chat_ids", [])

    return False


# ---------------------------------------------------------------------------
# 9. Bot Commands
# ---------------------------------------------------------------------------
def register_handlers(bot: telebot.TeleBot, auth: dict, names: dict) -> None:

    def guard(message) -> bool:
        return _guard(message, auth)

    # --- Admin claim (private chat, unclaimed) ---
    @bot.message_handler(func=lambda m: (
        m.chat.type == "private"
        and auth.get("admin_user_id") is None
    ))
    def claim_admin(message):
        with _auth_lock:
            if auth.get("admin_user_id") is not None:
                return
            auth["admin_user_id"] = message.from_user.id
            save_auth(auth, _AUTH_PATH)
        logger.info("Admin claimed by %s (id=%s)", _tg_user(message), message.from_user.id)
        bot.reply_to(message, "You are now the admin.")

    # --- /start, /help ---
    @bot.message_handler(commands=["start", "help"])
    def cmd_help(message):
        if not guard(message):
            return
        logger.info("Help: requested by %s", _tg_user(message))
        lines = [
            "Available commands:",
            "/status — show online players",
            "/list — list all known players",
            "/stats [player] — player statistics",
            "/playtime — playtime leaderboard",
            "/chat_id — show this chat's ID",
        ]
        if message.chat.type == "private" and is_admin(message.from_user.id, auth):
            lines += [
                "/authorize <chat_id> — whitelist a chat",
                "/revoke <chat_id> — remove a chat from whitelist",
                "/listchats — list authorized chats",
            ]
        bot.reply_to(message, "\n".join(lines))

    # --- /status ---
    @bot.message_handler(commands=["status"])
    def cmd_status(message):
        if not guard(message):
            return
        logger.info("Status: requested by %s", _tg_user(message))
        online = get_online_players()
        if online:
            bot.reply_to(message, f"Players online: {len(online)} ({', '.join(online)})")
        else:
            bot.reply_to(message, "No players currently online.")

    # --- /stats ---
    @bot.message_handler(commands=["stats"])
    def cmd_stats(message):
        if not guard(message):
            return
        args = message.text.split(maxsplit=1)
        target = args[1].strip().lower() if len(args) > 1 else None
        logger.info("Stats: requested by %s (player=%s)", _tg_user(message), target or "all")

        all_stats = read_player_stats(STATS_DIR, names)
        if not all_stats:
            bot.reply_to(message, "Stats directory not found or empty.")
            return

        if target:
            matches = [p for p in all_stats if p["name"].lower() == target]
            if not matches:
                bot.reply_to(message, f"No player found matching '{target}'.")
                return
            bot.reply_to(message, _format_stats(matches[0]))
        else:
            lines = [_format_stats(p) for p in sorted(all_stats, key=lambda p: p["name"].lower())]
            bot.reply_to(message, "\n\n".join(lines))

    # --- /playtime ---
    @bot.message_handler(commands=["playtime"])
    def cmd_playtime(message):
        if not guard(message):
            return
        logger.info("Playtime: requested by %s", _tg_user(message))
        all_stats = read_player_stats(STATS_DIR, names)
        if not all_stats:
            bot.reply_to(message, "Stats directory not found or empty.")
            return
        ranked = sorted(all_stats, key=lambda p: p["time_played_hours"], reverse=True)
        lines = [f"{i+1}. {p['name']} — {p['time_played_hours']}h" for i, p in enumerate(ranked)]
        bot.reply_to(message, "Playtime leaderboard:\n" + "\n".join(lines))

    # --- /list ---
    @bot.message_handler(commands=["list"])
    def cmd_list(message):
        if not guard(message):
            return
        logger.info("List: requested by %s", _tg_user(message))
        if not STATS_DIR.exists():
            bot.reply_to(message, "Stats directory not found.")
            return
        entries = sorted(
            names.get(f.stem, f.stem)
            for f in STATS_DIR.glob("*.json")
        )
        if not entries:
            bot.reply_to(message, "No players found in stats directory.")
            return
        bot.reply_to(message, "Known players:\n" + "\n".join(entries))

    # --- /chat_id ---
    @bot.message_handler(commands=["chat_id"])
    def cmd_chat_id(message):
        logger.info("ChatID: requested by %s (chat=%s)", _tg_user(message), message.chat.id)
        bot.reply_to(message, f"Chat ID: {message.chat.id}")

    # --- /authorize ---
    @bot.message_handler(commands=["authorize"])
    def cmd_authorize(message):
        if message.chat.type != "private":
            return
        if not is_admin(message.from_user.id, auth):
            return
        args = message.text.split(maxsplit=1)
        if len(args) > 1:
            try:
                target_id = int(args[1].strip())
            except ValueError:
                bot.reply_to(message, "Invalid chat ID.")
                return
        else:
            bot.reply_to(message, "Usage: /authorize <chat_id>")
            return
        with _auth_lock:
            if target_id not in auth["authorized_chat_ids"]:
                auth["authorized_chat_ids"].append(target_id)
                save_auth(auth, _AUTH_PATH)
        logger.info("Authorize: chat %s added by %s", target_id, _tg_user(message))
        bot.reply_to(message, f"Chat {target_id} is now authorized.")

    # --- /revoke ---
    @bot.message_handler(commands=["revoke"])
    def cmd_revoke(message):
        if message.chat.type != "private":
            return
        if not is_admin(message.from_user.id, auth):
            return
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            bot.reply_to(message, "Usage: /revoke <chat_id>")
            return
        try:
            target_id = int(args[1].strip())
        except ValueError:
            bot.reply_to(message, "Invalid chat ID.")
            return
        with _auth_lock:
            if target_id in auth["authorized_chat_ids"]:
                auth["authorized_chat_ids"].remove(target_id)
                save_auth(auth, _AUTH_PATH)
                logger.info("Revoke: chat %s removed by %s", target_id, _tg_user(message))
                bot.reply_to(message, f"Chat {target_id} has been revoked.")
            else:
                bot.reply_to(message, f"Chat {target_id} was not authorized.")

    # --- /listchats ---
    @bot.message_handler(commands=["listchats"])
    def cmd_listchats(message):
        if message.chat.type != "private":
            return
        if not is_admin(message.from_user.id, auth):
            return
        logger.info("ListChats: requested by %s", _tg_user(message))
        ids = auth.get("authorized_chat_ids", [])
        if ids:
            bot.reply_to(message, "Authorized chats:\n" + "\n".join(str(i) for i in ids))
        else:
            bot.reply_to(message, "No authorized chats.")


# ---------------------------------------------------------------------------
# 10. Main
# ---------------------------------------------------------------------------
def main():
    setup_logging(Path(__file__).parent / "logs")

    auth = load_auth(_AUTH_PATH)
    names = load_player_names(_NAMES_PATH)

    bot = telebot.TeleBot(BOT_TOKEN)
    register_handlers(bot, auth, names)

    notify = make_notify_callback(bot, auth)
    watcher = LogWatcher(LOG_PATH, names, notify)
    observer = Observer()
    observer.schedule(watcher, path=str(LOG_PATH.parent), recursive=False)
    observer.start()
    logger.info("Watching %s for join/leave events", LOG_PATH)

    class _NetworkErrorFilter(logging.Filter):
        _TRANSIENT = (
            ("Network is unreachable", "network unreachable"),
            ("NewConnectionError",     "network unreachable"),
            ("Max retries exceeded",   "network unreachable"),
            ("Read timed out",         "read timed out"),
            ("read operation timed out", "read timed out"),
            ("handshake operation timed out", "SSL handshake timed out"),
            ("Bad Gateway",            "Telegram returned 502 Bad Gateway"),
            ("Connection reset by peer", "connection reset by peer"),
        )

        def filter(self, record):
            msg = record.getMessage()
            for phrase, description in self._TRANSIENT:
                if phrase in msg:
                    # Only log warning for the exception line, not the traceback
                    if "Exception traceback" not in msg:
                        logger.warning("Polling: %s, retrying...", description)
                    return False  # suppress from TeleBot logger
            return True

    logging.getLogger("TeleBot").addFilter(_NetworkErrorFilter())

    try:
        bot.infinity_polling(timeout=30, long_polling_timeout=20)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()
        logger.info("Bot stopped")


if __name__ == "__main__":
    main()

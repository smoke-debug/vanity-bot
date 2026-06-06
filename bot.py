import os
import re
import json
import asyncio
import logging
import shutil
import atexit
import signal
import zipfile
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple, Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

# =========================
# BASIC SETTINGS
# =========================
TOKEN = os.getenv("DISCORD_TOKEN")
DEFAULT_PREFIX = os.getenv("BOT_PREFIX", "!")

# Safer defaults for Discord / Cloudflare.
DEFAULT_DELAY_SECONDS = 8
DEFAULT_BATCH_SIZE = 5
DEFAULT_BATCH_COOLDOWN_SECONDS = 60
DEFAULT_LIST_COOLDOWN_SECONDS = 90
DEFAULT_AUTO_MINUTES = 60
MAX_RETRIES = 3
MAX_CODES_PER_LIST = 1500
BLOCK_COOLDOWN_SECONDS = 600

API_BASE = "https://discord.com/api/v10/invites"

BASE_DIR = Path(__file__).resolve().parent


def resolve_data_dir() -> Path:
    """Choose the safest data folder.

    Priority:
    1. DATA_DIR env var if you set it.
    2. /data when Railway Volume is mounted.
    3. Local ./data folder as fallback.

    Lists and countdowns are saved to this folder. For Railway redeploys, attach
    a Volume and set DATA_DIR=/data so these files survive updates.
    """
    env_dir = os.getenv("DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser()

    railway_volume = Path("/data")
    try:
        if railway_volume.exists() and os.access(str(railway_volume), os.W_OK):
            return railway_volume
    except Exception:
        pass

    return BASE_DIR / "data"


DATA_DIR = resolve_data_dir()
# Kept as unavailable_vanities for compatibility with your existing hosted files.
# The contents are now NOT-TAKEN / AVAILABLE words only.
UNAVAILABLE_DIR = DATA_DIR / "unavailable_vanities"
CONFIG_FILE = DATA_DIR / "vanity_config.json"
ACTIVE_INVALID_FILE = DATA_DIR / "invalid_vanities.json"
EXPIRED_INVALID_FILE = DATA_DIR / "expired_invalid_vanities.json"
BACKFILL_STATE_FILE = DATA_DIR / "backfill_scan_state.json"
BACKFILL_EVENTS_FILE = DATA_DIR / "backfill_transition_events.json"
VANITY_STATUS_FILE = DATA_DIR / "vanity_statuses.json"
EVENT_LOG_FILE = DATA_DIR / "bot_events.log"
BACKUP_DIR = DATA_DIR / "backups"
COUNTDOWN_DAYS = 30
TRACKED_LENGTHS = range(1, 33)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("vanity_checker")

intents = discord.Intents.default()
intents.guilds = True
intents.message_content = True

config = {
    "prefix": DEFAULT_PREFIX,
    "auto_enabled": False,
    "auto_minutes": DEFAULT_AUTO_MINUTES,
    "delay_seconds": DEFAULT_DELAY_SECONDS,
    "batch_size": DEFAULT_BATCH_SIZE,
    "batch_cooldown_seconds": DEFAULT_BATCH_COOLDOWN_SECONDS,
    "list_cooldown_seconds": DEFAULT_LIST_COOLDOWN_SECONDS,
    "invalid_alert_channel_id": None,
    # Countdown filters. Use !setcountdownlengths to change these.
    # This prevents bad backfill data like discord.gg/a from polluting the tracker.
    "min_countdown_length": int(os.getenv("MIN_COUNTDOWN_LENGTH", "2")),
    "max_countdown_length": int(os.getenv("MAX_COUNTDOWN_LENGTH", "32")),
    # When true, !topcountdowns verifies candidates against Discord before showing them.
    "topcountdowns_live_verify": True,
    "lists": {}
}


def get_prefix(bot_obj, message):
    return config.get("prefix", DEFAULT_PREFIX)


bot = commands.Bot(command_prefix=get_prefix, intents=intents, help_command=None)
_slash_commands_synced = False

# Stores not-taken / available words, even though older command/file names say unavailable.
unavailable_cache = defaultdict(set)

# Active countdowns start when a vanity goes from taken/on-server (200) to not-taken/available (404).
# Expired countdowns are moved out of the active tracker after 30 days.
# If the vanity becomes taken/on-server again, it is removed from active/expired tracking.
active_invalid_vanities: dict[str, dict] = {}
expired_invalid_vanities: dict[str, dict] = {}

# Stores channel scan cursors and matched transition events so backfill commands
# can continue from unscanned messages instead of rescanning the same history.
backfill_scan_state: dict[str, dict] = {}
backfill_transition_events: dict[str, dict] = {}

# Persistent last-known status per vanity. This prevents first scans, lost TXT
# files, or redeploys from creating fake fresh countdowns for every currently
# available vanity. A countdown only starts when the previous saved status was
# taken/on-server and the newest check is available.
vanity_statuses: dict[str, dict] = {}

check_state = {
    "running": False,
    "stop_requested": False,
    "current": 0,
    "total": 0,
    "mode": None,
}

# =========================
# FILE / CONFIG HELPERS
# =========================

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def format_time(iso_time: Optional[str]) -> str:
    if not iso_time:
        return "Unknown"
    try:
        dt = datetime.fromisoformat(iso_time)
        unix = int(dt.timestamp())
        return f"<t:{unix}:F> • <t:{unix}:R>"
    except Exception:
        return "Unknown"


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UNAVAILABLE_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)


def backup_existing_file(path: Path, label: str, keep: int = 25) -> None:
    """Keep safety backups so list/config data is not lost from normal saves."""
    try:
        if not path.exists():
            return
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_path = BACKUP_DIR / f"{label}_{timestamp}.json"
        shutil.copy2(path, backup_path)
        backups = sorted(BACKUP_DIR.glob(f"{label}_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in backups[keep:]:
            try:
                old.unlink()
            except Exception:
                pass
    except Exception as e:
        logger.warning("Could not create backup for %s: %s", path.name, e)


def write_event_log(event: str, details: Optional[dict] = None) -> None:
    """Append one JSON line to data/bot_events.log for auditing saves and transitions."""
    try:
        ensure_dirs()
        payload = {
            "at": now_iso(),
            "event": str(event),
            "details": details or {},
        }
        with open(EVENT_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception as e:
        logger.warning("Could not write event log: %s", e)


def latest_backup_for(label: str) -> Optional[Path]:
    try:
        backups = sorted(BACKUP_DIR.glob(f"{label}_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        return backups[0] if backups else None
    except Exception:
        return None


def restore_latest_backup(path: Path, label: str) -> bool:
    backup = latest_backup_for(label)
    if not backup:
        return False
    try:
        shutil.copy2(backup, path)
        write_event_log("backup_restored", {"file": path.name, "backup": backup.name})
        return True
    except Exception as e:
        logger.warning("Could not restore backup for %s from %s: %s", path.name, backup, e)
        return False


def atomic_write_json(path: Path, data, *, label: Optional[str] = None, backup: bool = True) -> None:
    """Crash-safe JSON write with fsync + replace. Existing file is backed up first."""
    ensure_dirs()
    label = label or path.stem
    if backup:
        backup_existing_file(path, label)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    tmp_path.replace(path)


def atomic_write_text(path: Path, text: str) -> None:
    ensure_dirs()
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    tmp_path.replace(path)


def save_all_data(reason: str = "manual") -> None:
    """Save every persistent store. Safe to call from shutdown/autosave."""
    try:
        save_config()
        save_invalid_tracker()
        save_backfill_progress()
        save_vanity_statuses()
        for length in TRACKED_LENGTHS:
            rewrite_unavailable_file(length)
        write_event_log("all_data_saved", {
            "reason": reason,
            "data_dir": str(DATA_DIR),
            "lists": len(config.get("lists", {})),
            "active_countdowns": len(active_invalid_vanities),
            "expired_countdowns": len(expired_invalid_vanities),
        })
    except Exception as e:
        logger.warning("save_all_data failed: %s", e)


def normalize_list_record(data: dict) -> dict:
    """Keeps old configs working while adding separate available/taken channels."""
    if not isinstance(data, dict):
        data = {}

    # Old versions used claim_channel_id for available/not-taken results.
    if "available_channel_id" not in data and data.get("claim_channel_id"):
        data["available_channel_id"] = data.get("claim_channel_id")

    # Old versions only had log_channel_id. Use logs as taken channel until you run setchannels.
    if "taken_channel_id" not in data:
        data["taken_channel_id"] = data.get("taken_channel_id") or data.get("taken_log_channel_id") or data.get("log_channel_id")

    # Keep old key for compatibility in list displays.
    if "claim_channel_id" not in data and data.get("available_channel_id"):
        data["claim_channel_id"] = data.get("available_channel_id")

    data.setdefault("log_channel_id", data.get("summary_channel_id"))
    data.setdefault("summary_channel_id", data.get("log_channel_id"))
    data.setdefault("ping_role_id", None)
    data.setdefault("words", [])
    data.setdefault("created_at", now_iso())
    data.setdefault("updated_at", data.get("created_at"))
    return data


def save_config() -> None:
    # This is called immediately after addlist/addwords/setchannels/etc.
    # It writes atomically so lists survive normal bot restarts/crashes.
    atomic_write_json(CONFIG_FILE, config, label="vanity_config", backup=True)
    write_event_log("config_saved", {"lists": len(config.get("lists", {})), "file": CONFIG_FILE.name})


def load_config() -> None:
    ensure_dirs()
    if not CONFIG_FILE.exists():
        # If a previous save exists in backups, restore it instead of starting blank.
        if restore_latest_backup(CONFIG_FILE, "vanity_config"):
            write_event_log("config_loaded_from_backup", {"file": CONFIG_FILE.name})
        else:
            save_config()
            write_event_log("config_created", {"file": CONFIG_FILE.name})
            return

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
    except Exception as e:
        logger.warning("Config failed to load: %s", e)
        if restore_latest_backup(CONFIG_FILE, "vanity_config"):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                write_event_log("config_restored_after_load_failure", {"file": CONFIG_FILE.name})
            except Exception as restore_error:
                logger.warning("Restored config still failed to load: %s", restore_error)
                return
        else:
            return

    config["prefix"] = str(loaded.get("prefix", DEFAULT_PREFIX))[:5] or DEFAULT_PREFIX
    config["auto_enabled"] = bool(loaded.get("auto_enabled", False))
    config["auto_minutes"] = int(loaded.get("auto_minutes", DEFAULT_AUTO_MINUTES))
    config["delay_seconds"] = int(loaded.get("delay_seconds", DEFAULT_DELAY_SECONDS))
    config["batch_size"] = int(loaded.get("batch_size", DEFAULT_BATCH_SIZE))
    config["batch_cooldown_seconds"] = int(loaded.get("batch_cooldown_seconds", DEFAULT_BATCH_COOLDOWN_SECONDS))
    config["list_cooldown_seconds"] = int(loaded.get("list_cooldown_seconds", DEFAULT_LIST_COOLDOWN_SECONDS))
    config["invalid_alert_channel_id"] = loaded.get("invalid_alert_channel_id")
    try:
        config["min_countdown_length"] = max(1, min(32, int(loaded.get("min_countdown_length", config.get("min_countdown_length", 2)))))
    except Exception:
        config["min_countdown_length"] = 2
    try:
        config["max_countdown_length"] = max(config["min_countdown_length"], min(32, int(loaded.get("max_countdown_length", config.get("max_countdown_length", 32)))))
    except Exception:
        config["max_countdown_length"] = 32
    config["topcountdowns_live_verify"] = bool(loaded.get("topcountdowns_live_verify", config.get("topcountdowns_live_verify", True)))

    loaded_lists = loaded.get("lists", {}) if isinstance(loaded.get("lists", {}), dict) else {}
    config["lists"] = {clean_code(name): normalize_list_record(data) for name, data in loaded_lists.items() if clean_code(name)}
    write_event_log("config_loaded", {"lists": len(config["lists"]), "data_dir": str(DATA_DIR)})


def unavailable_file(length: int) -> Path:
    return UNAVAILABLE_DIR / f"unavailable_{length}_letters.txt"


def ensure_unavailable_files() -> None:
    ensure_dirs()
    for length in TRACKED_LENGTHS:
        unavailable_file(length).touch(exist_ok=True)


def clean_code(item: Any) -> str:
    text = str(item).strip()
    text = re.sub(r"https?://", "", text, flags=re.I)
    text = text.replace("discord.gg/", "")
    text = text.replace("discord.com/invite/", "")
    text = text.strip().strip("/").strip()
    text = text.split("?")[0].split("#")[0]
    text = text.lower()
    text = re.sub(r"[^a-z0-9_-]", "", text)
    return text


def countdown_length_bounds() -> tuple[int, int]:
    try:
        min_len = max(1, min(32, int(config.get("min_countdown_length", 2))))
    except Exception:
        min_len = 2
    try:
        max_len = max(min_len, min(32, int(config.get("max_countdown_length", 32))))
    except Exception:
        max_len = 32
    return min_len, max_len


def is_countdown_trackable(code: Any) -> bool:
    code = clean_code(code)
    if not code:
        return False
    min_len, max_len = countdown_length_bounds()
    return min_len <= len(code) <= max_len


def countdown_filter_label() -> str:
    min_len, max_len = countdown_length_bounds()
    if min_len == max_len:
        return f"{min_len} chars only"
    return f"{min_len}-{max_len} chars"


def prune_countdown_tracker_by_length(*, save: bool = True) -> dict:
    removed_active = []
    removed_expired = []
    for code in list(active_invalid_vanities.keys()):
        if not is_countdown_trackable(code):
            active_invalid_vanities.pop(code, None)
            removed_active.append(code)
    for code in list(expired_invalid_vanities.keys()):
        if not is_countdown_trackable(code):
            expired_invalid_vanities.pop(code, None)
            removed_expired.append(code)
    if save and (removed_active or removed_expired):
        save_invalid_tracker()
        write_event_log("countdowns_pruned_by_length", {
            "filter": countdown_filter_label(),
            "removed_active": len(removed_active),
            "removed_expired": len(removed_expired),
            "sample_active": removed_active[:50],
            "sample_expired": removed_expired[:50],
        })
    return {
        "removed_active": len(removed_active),
        "removed_expired": len(removed_expired),
        "filter": countdown_filter_label(),
    }


def parse_words(words: str) -> list[str]:
    raw_items = re.split(r"[,\n\s]+", words)
    seen = set()
    cleaned = []
    for item in raw_items:
        code = clean_code(item)
        if not code or code in seen:
            continue
        seen.add(code)
        cleaned.append(code)
    return cleaned


def load_unavailable_cache() -> None:
    unavailable_cache.clear()
    ensure_unavailable_files()
    for length in TRACKED_LENGTHS:
        path = unavailable_file(length)
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                code = clean_code(line)
                if code and len(code) == length:
                    unavailable_cache[length].add(code)


def rewrite_unavailable_file(length: int) -> None:
    path = unavailable_file(length)
    text = "".join(code + "\n" for code in sorted(unavailable_cache[length]))
    atomic_write_text(path, text)


def add_unavailable(code: str) -> bool:
    code = clean_code(code)
    length = len(code)
    if length not in TRACKED_LENGTHS:
        return False
    before = len(unavailable_cache[length])
    unavailable_cache[length].add(code)
    if len(unavailable_cache[length]) != before:
        rewrite_unavailable_file(length)
        return True
    return False


def remove_unavailable(code: str) -> bool:
    code = clean_code(code)
    length = len(code)
    if length not in TRACKED_LENGTHS:
        return False
    if code in unavailable_cache[length]:
        unavailable_cache[length].remove(code)
        rewrite_unavailable_file(length)
        return True
    return False

# =========================
# INVALID / TAKEN COUNTDOWN TRACKER
# =========================

TAKEN_TRANSITION_RE = re.compile(
    r"`?discord\.gg/([A-Za-z0-9_-]+)`?\s+is taken/on a server and was removed from the not-taken TXT file",
    re.I,
)
# Also treat normal per-check taken lines as a latest "taken" update.
# Example: 8 letters | Taken/on server: `discord.gg/example`
# This prevents backfill from keeping a countdown if a newer log message says the vanity is currently taken.
TAKEN_STATUS_RE = re.compile(
    r"(?:\d+\s+letters\s*\|\s*)?Taken/on server:\s*`?discord\.gg/([A-Za-z0-9_-]+)`?",
    re.I,
)
AVAILABLE_TRANSITION_RE = re.compile(
    r"`?discord\.gg/([A-Za-z0-9_-]+)`?\s+is not taken/available and was added to the not-taken TXT file",
    re.I,
)


def parse_iso_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def read_json_file(path: Path, default, *, label: Optional[str] = None):
    ensure_dirs()
    label = label or path.stem
    if not path.exists():
        if restore_latest_backup(path, label):
            write_event_log("json_restored_from_backup", {"file": path.name, "label": label})
        else:
            return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except Exception as e:
        logger.warning("Failed to read %s: %s", path.name, e)
        if restore_latest_backup(path, label):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                write_event_log("json_restored_after_read_failure", {"file": path.name, "label": label})
                return data if isinstance(data, type(default)) else default
            except Exception as restore_error:
                logger.warning("Restored %s still failed to read: %s", path.name, restore_error)
        return default


def write_json_file(path: Path, data, *, label: Optional[str] = None, backup: bool = True) -> None:
    atomic_write_json(path, data, label=label or path.stem, backup=backup)


def normalize_tracker_record(code: str, record: dict, *, expired: bool = False) -> Optional[dict]:
    code = clean_code(code or record.get("code", ""))
    if not code or not is_countdown_trackable(code):
        return None

    taken_at_dt = parse_iso_dt(record.get("taken_at") or record.get("invalid_at"))
    if not taken_at_dt:
        return None

    expires_at_dt = parse_iso_dt(record.get("expires_at")) or (taken_at_dt + timedelta(days=COUNTDOWN_DAYS))
    output = dict(record)
    output["code"] = code
    output["taken_at"] = taken_at_dt.isoformat()
    output["invalid_at"] = taken_at_dt.isoformat()
    output["expires_at"] = expires_at_dt.isoformat()
    output["length"] = int(output.get("length") or len(code))
    output.setdefault("list", "unknown")
    output.setdefault("source", "tracker")
    output.setdefault("created_at", output["taken_at"])
    output.setdefault("updated_at", now_iso())

    if expired:
        output.setdefault("expired_at", output["expires_at"])
        output.setdefault("moved_at", now_iso())
        output.setdefault("alert_sent", False)
        output.setdefault("alert_sent_at", None)
    return output


def load_invalid_tracker() -> None:
    active_invalid_vanities.clear()
    expired_invalid_vanities.clear()

    raw_active = read_json_file(ACTIVE_INVALID_FILE, {})
    raw_expired = read_json_file(EXPIRED_INVALID_FILE, {})

    for code, record in raw_active.items():
        if isinstance(record, dict):
            normalized = normalize_tracker_record(code, record, expired=False)
            if normalized:
                active_invalid_vanities[normalized["code"]] = normalized

    for code, record in raw_expired.items():
        if isinstance(record, dict):
            normalized = normalize_tracker_record(code, record, expired=True)
            if normalized:
                expired_invalid_vanities[normalized["code"]] = normalized


def save_invalid_tracker() -> None:
    write_json_file(ACTIVE_INVALID_FILE, active_invalid_vanities, label="invalid_vanities", backup=True)
    write_json_file(EXPIRED_INVALID_FILE, expired_invalid_vanities, label="expired_invalid_vanities", backup=True)
    write_event_log("countdowns_saved", {
        "active": len(active_invalid_vanities),
        "expired": len(expired_invalid_vanities),
    })


def load_backfill_progress() -> None:
    """Load incremental backfill cursors and matched transition events."""
    backfill_scan_state.clear()
    backfill_transition_events.clear()

    raw_state = read_json_file(BACKFILL_STATE_FILE, {})
    raw_events = read_json_file(BACKFILL_EVENTS_FILE, {})

    if isinstance(raw_state, dict):
        for channel_id, state in raw_state.items():
            if isinstance(state, dict):
                backfill_scan_state[str(channel_id)] = state

    if isinstance(raw_events, dict):
        for event_key, event in raw_events.items():
            if not isinstance(event, dict):
                continue
            code = clean_code(event.get("code", ""))
            event_type = str(event.get("event_type", "")).lower()
            event_at = parse_iso_dt(event.get("event_at"))
            if not code or event_type not in {"available", "taken"} or not event_at:
                continue
            normalized = dict(event)
            normalized["code"] = code
            normalized["event_type"] = event_type
            normalized["event_at"] = event_at.isoformat()
            backfill_transition_events[str(event_key)] = normalized


def save_backfill_progress() -> None:
    write_json_file(BACKFILL_STATE_FILE, backfill_scan_state, label="backfill_scan_state", backup=True)
    write_json_file(BACKFILL_EVENTS_FILE, backfill_transition_events, label="backfill_transition_events", backup=True)
    write_event_log("backfill_progress_saved", {
        "channels": len(backfill_scan_state),
        "events": len(backfill_transition_events),
    })


def load_vanity_statuses() -> None:
    """Load persistent last-known invite statuses.

    Status values:
    - available = Discord returned 404 / not taken
    - taken = Discord returned 200 / on server

    These statuses are separate from the old not-taken TXT files so the bot
    can survive updates/restarts without treating every available vanity as a
    brand-new transition.
    """
    vanity_statuses.clear()
    raw = read_json_file(VANITY_STATUS_FILE, {})
    if not isinstance(raw, dict):
        return
    for code, record in raw.items():
        code = clean_code(code)
        if not code:
            continue
        if isinstance(record, str):
            status = record.lower()
            record = {"code": code, "last_status": status, "last_checked_at": None}
        elif isinstance(record, dict):
            status = str(record.get("last_status", "")).lower()
        else:
            continue
        if status not in {"available", "taken"}:
            continue
        out = dict(record)
        out["code"] = code
        out["last_status"] = status
        vanity_statuses[code] = out


def save_vanity_statuses() -> None:
    write_json_file(VANITY_STATUS_FILE, vanity_statuses, label="vanity_statuses", backup=True)
    write_event_log("vanity_statuses_saved", {"statuses": len(vanity_statuses)})


def get_last_vanity_status(code: str) -> Optional[str]:
    code = clean_code(code)
    record = vanity_statuses.get(code)
    if not isinstance(record, dict):
        return None
    status = str(record.get("last_status", "")).lower()
    return status if status in {"available", "taken"} else None


def set_last_vanity_status(code: str, status: str, *, list_name: str = "unknown", source: str = "checker", checked_at: Optional[str] = None, save: bool = False) -> None:
    code = clean_code(code)
    status = str(status).lower()
    if not code or status not in {"available", "taken"}:
        return
    timestamp = checked_at or now_iso()
    previous = vanity_statuses.get(code, {}) if isinstance(vanity_statuses.get(code), dict) else {}
    vanity_statuses[code] = {
        "code": code,
        "last_status": status,
        "last_checked_at": timestamp,
        "last_list": list_name or previous.get("last_list") or "unknown",
        "last_source": source,
        "previous_status": previous.get("last_status"),
        "updated_at": timestamp,
        "created_at": previous.get("created_at") or timestamp,
    }
    if save:
        save_vanity_statuses()


def seed_statuses_from_not_taken_files() -> int:
    """Best-effort migration for older installs.

    Old versions only had TXT files of currently not-taken/available words.
    On startup we mark those as available so a fresh deploy does not start a
    countdown for every word it sees again. This does NOT create countdowns.
    """
    added = 0
    timestamp = now_iso()
    for length in TRACKED_LENGTHS:
        for code in list(unavailable_cache[length]):
            if code and code not in vanity_statuses:
                set_last_vanity_status(code, "available", list_name="txt_seed", source="startup_txt_seed", checked_at=timestamp, save=False)
                added += 1
    if added:
        save_vanity_statuses()
        write_event_log("vanity_statuses_seeded_from_txt", {"added": added})
    return added


def backfill_event_key(channel_id: int, message_id: int, event_type: str, code: str) -> str:
    return f"{int(channel_id)}:{int(message_id)}:{event_type}:{clean_code(code)}"


def extract_transition_events_from_message(message: discord.Message, channel_label: str) -> list[dict]:
    """Return transition events found in one log message."""
    content = message.content or ""
    event_at = message.created_at.astimezone(timezone.utc)
    events = []

    for match in AVAILABLE_TRANSITION_RE.finditer(content):
        code = clean_code(match.group(1))
        if code and is_countdown_trackable(code):
            events.append({
                "event_type": "available",
                "code": code,
                "event_at": event_at.isoformat(),
                "channel_id": int(message.channel.id),
                "channel_name": getattr(message.channel, "name", "unknown"),
                "message_id": int(message.id),
                "source": channel_label,
            })

    seen_taken_codes = set()
    for pattern in (TAKEN_TRANSITION_RE, TAKEN_STATUS_RE):
        for match in pattern.finditer(content):
            code = clean_code(match.group(1))
            if code and is_countdown_trackable(code) and code not in seen_taken_codes:
                seen_taken_codes.add(code)
                events.append({
                    "event_type": "taken",
                    "code": code,
                    "event_at": event_at.isoformat(),
                    "channel_id": int(message.channel.id),
                    "channel_name": getattr(message.channel, "name", "unknown"),
                    "message_id": int(message.id),
                    "source": channel_label,
                })

    return events


def update_channel_backfill_state(channel: discord.TextChannel, messages: list[discord.Message], *, initial_scan: bool, older_requested: bool) -> None:
    """Update per-channel cursors so future scans only pull unscanned history."""
    cid = str(channel.id)
    state = backfill_scan_state.setdefault(cid, {})
    state["channel_id"] = int(channel.id)
    state["channel_name"] = getattr(channel, "name", "unknown")
    state["guild_id"] = int(channel.guild.id) if getattr(channel, "guild", None) else None
    state["last_scan_at"] = now_iso()
    state["scan_runs"] = int(state.get("scan_runs", 0)) + 1

    if messages:
        sorted_messages = sorted(messages, key=lambda m: m.created_at)
        oldest_msg = sorted_messages[0]
        newest_msg = sorted_messages[-1]

        current_oldest_id = state.get("oldest_message_id")
        current_newest_id = state.get("newest_message_id")

        if not current_oldest_id or int(oldest_msg.id) < int(current_oldest_id):
            state["oldest_message_id"] = int(oldest_msg.id)
            state["oldest_message_at"] = oldest_msg.created_at.astimezone(timezone.utc).isoformat()

        if not current_newest_id or int(newest_msg.id) > int(current_newest_id):
            state["newest_message_id"] = int(newest_msg.id)
            state["newest_message_at"] = newest_msg.created_at.astimezone(timezone.utc).isoformat()

    if older_requested and not messages:
        state["older_history_complete"] = True

    if initial_scan and not messages:
        state["older_history_complete"] = True

    backfill_scan_state[cid] = state


def reset_backfill_channel_state(channel_id: int) -> bool:
    """Remove only the saved scan cursor for one channel.

    Stored transition events are kept so rescanning does not duplicate countdowns.
    """
    cid = str(int(channel_id))
    had_state = cid in backfill_scan_state
    backfill_scan_state.pop(cid, None)
    save_backfill_progress()
    return had_state


def get_events_by_code_from_backfill() -> dict[str, list[tuple[datetime, int, str, str, int]]]:
    """Return stored backfill events grouped by vanity code.

    Tuple format: (event_dt, message_id, event_type, source, channel_id)
    """
    events_by_code: dict[str, list[tuple[datetime, int, str, str, int]]] = defaultdict(list)
    for event in backfill_transition_events.values():
        event_dt = parse_iso_dt(event.get("event_at"))
        code = clean_code(event.get("code", ""))
        event_type = str(event.get("event_type", "")).lower()
        if event_dt and code and is_countdown_trackable(code) and event_type in {"available", "taken"}:
            events_by_code[code].append((
                event_dt,
                int(event.get("message_id", 0)),
                event_type,
                event.get("source", "backfill"),
                int(event.get("channel_id", 0) or 0),
            ))
    for code in events_by_code:
        events_by_code[code].sort(key=lambda item: (item[0], item[1]))
    return events_by_code


def compress_status_runs(events: list[tuple[datetime, int, str, str, int]]) -> list[dict]:
    """Collapse repeated same-status logs into compact runs.

    Example: taken, taken, taken, available, available ->
    [taken x3, available x2]
    """
    runs: list[dict] = []
    for event_dt, message_id, event_type, source, channel_id in sorted(events, key=lambda item: (item[0], item[1])):
        if runs and runs[-1]["status"] == event_type:
            run = runs[-1]
            run["count"] += 1
            run["last_at"] = event_dt.isoformat()
            run["last_message_id"] = message_id
            run["last_source"] = source
            run["last_channel_id"] = channel_id
        else:
            runs.append({
                "status": event_type,
                "count": 1,
                "first_at": event_dt.isoformat(),
                "last_at": event_dt.isoformat(),
                "first_message_id": message_id,
                "last_message_id": message_id,
                "first_source": source,
                "last_source": source,
                "first_channel_id": channel_id,
                "last_channel_id": channel_id,
            })
    return runs


def replay_stored_backfill_events_to_tracker() -> dict:
    """Replay stored backfill log events using strict status-run logic.

    This is the accuracy rule:

        taken/on-server -> FIRST not-taken/available after that = countdown start

    Repeated not-taken logs from rerunning the same list do NOT reset the timer.
    Repeated taken logs are collapsed/ignored. If the newest state is taken, no
    countdown is kept. If the scanned history only shows available logs and no
    earlier taken/on-server event, the bot skips it because it cannot prove when
    it actually became available.
    """
    events_by_code = get_events_by_code_from_backfill()

    reconstructed: dict[str, dict] = {}
    removed_by_taken = set()
    skipped_latest_taken = 0
    ignored_repeat_available = 0
    ignored_repeat_taken = 0
    skipped_no_prior_taken = 0

    for code, events in events_by_code.items():
        current_status: Optional[str] = None
        current_available_start: Optional[tuple[datetime, int, str]] = None
        seen_taken_before_current_available = False
        ever_saw_taken = False

        for event_dt, message_id, event_type, label, channel_id in events:
            if event_type == "taken":
                ever_saw_taken = True
                if current_status != "taken":
                    current_status = "taken"
                    current_available_start = None
                    seen_taken_before_current_available = False
                else:
                    ignored_repeat_taken += 1
                continue

            if event_type == "available":
                if current_status != "available":
                    # First available log after the most recent taken/on-server run.
                    current_status = "available"
                    current_available_start = (event_dt, message_id, label)
                    seen_taken_before_current_available = ever_saw_taken
                else:
                    # Rerun log while it is still available; keep the original start.
                    ignored_repeat_available += 1
                continue

        if current_status == "available" and current_available_start and seen_taken_before_current_available:
            start_dt, source_message_id, label = current_available_start
            record = make_tracker_record(code, label, taken_at=start_dt.isoformat(), source="backfill_first_available_after_taken_run")
            record["source_message_id"] = source_message_id
            record["backfill_logic"] = "first_available_after_most_recent_taken_strict"
            reconstructed[code] = record
        elif current_status == "available" and current_available_start and not seen_taken_before_current_available:
            # This avoids fake countdowns when the scan only found repeated available logs.
            skipped_no_prior_taken += 1
            active_invalid_vanities.pop(code, None)
            expired_invalid_vanities.pop(code, None)
        else:
            removed_by_taken.add(code)
            skipped_latest_taken += 1
            active_invalid_vanities.pop(code, None)
            expired_invalid_vanities.pop(code, None)

    added_active = 0
    updated_active = 0
    moved_expired = 0
    replaced_expired = 0
    replaced_newer_existing_with_earlier_streak = 0
    now_dt = datetime.now(timezone.utc)

    # Remove any saved countdown whose newest reconstructed state is not a confirmed available run.
    for code in list(active_invalid_vanities.keys()):
        if code in events_by_code and code not in reconstructed:
            active_invalid_vanities.pop(code, None)
    for code in list(expired_invalid_vanities.keys()):
        if code in events_by_code and code not in reconstructed:
            expired_invalid_vanities.pop(code, None)

    for code, record in reconstructed.items():
        record_dt = parse_iso_dt(record.get("taken_at") or record.get("invalid_at"))
        existing_active = active_invalid_vanities.get(code)
        existing_expired = expired_invalid_vanities.get(code)
        existing_active_dt = parse_iso_dt((existing_active or {}).get("taken_at") or (existing_active or {}).get("invalid_at"))
        existing_expired_dt = parse_iso_dt((existing_expired or {}).get("taken_at") or (existing_expired or {}).get("invalid_at"))
        newest_existing_dt = max([d for d in (existing_active_dt, existing_expired_dt) if d], default=None)

        if newest_existing_dt and record_dt and newest_existing_dt > record_dt:
            replaced_newer_existing_with_earlier_streak += 1

        expires_dt = parse_iso_dt(record.get("expires_at"))
        if expires_dt and expires_dt <= now_dt:
            active_invalid_vanities.pop(code, None)
            expired_record = dict(record)
            expired_record["expired_at"] = expires_dt.isoformat()
            expired_record["moved_at"] = now_iso()
            expired_record["moved_by"] = "backfill"
            expired_record["alert_sent"] = False
            expired_record["alert_sent_at"] = None
            expired_record["alert_skipped_reason"] = "Backfilled after the timer had already expired; not auto-pinged to avoid spam."
            if code not in expired_invalid_vanities:
                moved_expired += 1
            else:
                replaced_expired += 1
            expired_invalid_vanities[code] = expired_record
            continue

        expired_invalid_vanities.pop(code, None)
        existing = active_invalid_vanities.get(code)
        if existing:
            existing_dt = parse_iso_dt(existing.get("taken_at") or existing.get("invalid_at"))
            if record_dt and (not existing_dt or record_dt != existing_dt):
                active_invalid_vanities[code] = record
                updated_active += 1
        else:
            active_invalid_vanities[code] = record
            added_active += 1

    save_invalid_tracker()
    write_event_log("backfill_replayed_status_runs", {
        "vanities_with_events": len(events_by_code),
        "reconstructed_available": len(reconstructed),
        "skipped_latest_taken": skipped_latest_taken,
        "skipped_available_without_prior_taken": skipped_no_prior_taken,
        "repeated_available_logs_ignored": ignored_repeat_available,
        "repeated_taken_logs_ignored": ignored_repeat_taken,
        "replaced_newer_existing_with_earlier_streak": replaced_newer_existing_with_earlier_streak,
    })
    return {
        "stored_events_total": len(backfill_transition_events),
        "vanities_with_events": len(events_by_code),
        "added_active": added_active,
        "updated_active": updated_active,
        "moved_expired": moved_expired,
        "replaced_expired": replaced_expired,
        "removed_by_taken": len(removed_by_taken),
        "skipped_latest_taken": skipped_latest_taken,
        "skipped_no_prior_taken": skipped_no_prior_taken,
        "ignored_repeat_available": ignored_repeat_available,
        "ignored_repeat_taken": ignored_repeat_taken,
        "replaced_newer_existing_with_earlier_streak": replaced_newer_existing_with_earlier_streak,
        "active_total": len(active_invalid_vanities),
        "expired_total": len(expired_invalid_vanities),
    }

def make_tracker_record(code: str, list_name: str, taken_at: Optional[str] = None, source: str = "checker") -> dict:
    # The parameter name is kept as taken_at for compatibility with older saved JSON.
    # For this tracker, it means the time the vanity became not-taken/available.
    code = clean_code(code)
    taken_dt = parse_iso_dt(taken_at) or datetime.now(timezone.utc)
    expires_dt = taken_dt + timedelta(days=COUNTDOWN_DAYS)
    timestamp = now_iso()
    return {
        "code": code,
        "taken_at": taken_dt.isoformat(),
        "invalid_at": taken_dt.isoformat(),
        "expires_at": expires_dt.isoformat(),
        "length": len(code),
        "list": list_name or "unknown",
        "source": source,
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def add_invalid_vanity(code: str, list_name: str, taken_at: Optional[str] = None, source: str = "checker") -> dict:
    """Start/reset a 30-day countdown after taken/on-server -> not-taken/available.

    If an active countdown already exists, only reset it when the new not-taken
    timestamp is newer than the saved start time. This keeps countdowns based on
    the most recent log/check that showed the vanity became not-taken/available.
    """
    code = clean_code(code)
    if not code or not is_countdown_trackable(code):
        write_event_log("countdown_skipped_untrackable_length", {"code": code, "filter": countdown_filter_label(), "source": source})
        return {}

    # A fresh not-taken transition should remove any old expired record for the same code.
    removed_expired = expired_invalid_vanities.pop(code, None) is not None

    record = make_tracker_record(code, list_name, taken_at=taken_at, source=source)
    incoming_dt = parse_iso_dt(record.get("taken_at") or record.get("invalid_at"))

    existing = active_invalid_vanities.get(code)
    if existing:
        existing_dt = parse_iso_dt(existing.get("taken_at") or existing.get("invalid_at"))

        # Same or older transition: do not move the countdown backwards.
        if existing_dt and incoming_dt and incoming_dt <= existing_dt:
            existing["updated_at"] = now_iso()
            existing["last_seen_available_at"] = incoming_dt.isoformat()
            existing["last_seen_available_source"] = source
            save_invalid_tracker()
            write_event_log("countdown_kept_existing_newer", {
                "code": code,
                "existing_taken_at": existing.get("taken_at"),
                "incoming_taken_at": record.get("taken_at"),
                "source": source,
            })
            return existing

        # Newer not-taken transition: reset the countdown to the most recent log/check.
        record["previous_taken_at"] = existing.get("taken_at")
        record["reset_at"] = now_iso()
        record["reset_reason"] = "newer_not_taken_transition"
        active_invalid_vanities[code] = record
        save_invalid_tracker()
        write_event_log("countdown_reset_to_newer_available_transition", {
            "code": code,
            "list": list_name,
            "source": source,
            "old_taken_at": existing.get("taken_at"),
            "new_taken_at": record.get("taken_at"),
            "new_expires_at": record.get("expires_at"),
        })
        return record

    active_invalid_vanities[code] = record
    save_invalid_tracker()
    write_event_log("countdown_started", {
        "code": code,
        "list": list_name,
        "source": source,
        "taken_at": record.get("taken_at"),
        "expires_at": record.get("expires_at"),
        "removed_existing_expired_record": removed_expired,
    })
    return record


def remove_invalid_vanity(code: str, reason: str = "became_taken", seen_at: Optional[str] = None, save: bool = True) -> tuple[Optional[dict], Optional[dict]]:
    """Remove countdown records when the vanity becomes taken/on-server again."""
    code = clean_code(code)
    if not code:
        return None, None

    active_record = active_invalid_vanities.pop(code, None)
    expired_record = expired_invalid_vanities.pop(code, None)

    if save and (active_record or expired_record):
        save_invalid_tracker()
        write_event_log("countdown_removed", {
            "code": code,
            "reason": reason,
            "seen_at": seen_at or now_iso(),
            "had_active": bool(active_record),
            "had_expired": bool(expired_record),
        })
    return active_record, expired_record


def seconds_until(iso_time: Optional[str]) -> int:
    dt = parse_iso_dt(iso_time)
    if not dt:
        return 0
    return int((dt - datetime.now(timezone.utc)).total_seconds())


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def discord_relative(iso_time: Optional[str]) -> str:
    dt = parse_iso_dt(iso_time)
    if not dt:
        return "Unknown"
    unix = int(dt.timestamp())
    return f"<t:{unix}:F> • <t:{unix}:R>"


def active_sorted_recent() -> list[dict]:
    return sorted(
        active_invalid_vanities.values(),
        key=lambda r: parse_iso_dt(r.get("taken_at")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )


def active_sorted_expiring() -> list[dict]:
    return sorted(
        active_invalid_vanities.values(),
        key=lambda r: parse_iso_dt(r.get("expires_at")) or datetime.max.replace(tzinfo=timezone.utc),
    )


def expired_sorted_recent() -> list[dict]:
    return sorted(
        expired_invalid_vanities.values(),
        key=lambda r: parse_iso_dt(r.get("expired_at")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )


def build_invalid_detail_embed(record: dict, *, expired: bool = False) -> discord.Embed:
    code = record.get("code", "unknown")
    color = discord.Color.orange() if not expired else discord.Color.dark_gray()
    title = f"Countdown {'Expired' if expired else 'Active'}: discord.gg/{code}"
    embed = discord.Embed(title=title, color=color)
    embed.add_field(name="Vanity", value=f"`discord.gg/{code}`", inline=False)
    embed.add_field(name="Started When", value="Taken / on server → Not taken / available", inline=False)
    embed.add_field(name="Not Taken Since", value=discord_relative(record.get("taken_at") or record.get("invalid_at")), inline=False)
    embed.add_field(name="30-Day Timer Ends", value=discord_relative(record.get("expires_at")), inline=False)
    if expired:
        embed.add_field(name="Timer Expired", value=discord_relative(record.get("expired_at") or record.get("expires_at")), inline=False)
    else:
        remaining = seconds_until(record.get("expires_at"))
        embed.add_field(name="Live Countdown", value=f"`{format_duration(remaining)}` remaining • <t:{int(parse_iso_dt(record.get('expires_at')).timestamp())}:R>", inline=False)
    embed.add_field(name="List", value=f"`{record.get('list', 'unknown')}`", inline=True)
    embed.add_field(name="Length", value=f"`{record.get('length', len(code))}`", inline=True)
    embed.set_footer(text="Countdowns are based on when this bot detected taken/on-server → not-taken/available.")
    return embed


def build_active_list_embed(limit: int = 10, *, recent: bool = True) -> discord.Embed:
    limit = max(1, min(int(limit), 25))
    records = active_sorted_recent() if recent else active_sorted_expiring()
    shown = records[:limit]
    embed = discord.Embed(
        title="Active Vanity Countdowns",
        description="Tracking vanities that changed from taken/on-server to not-taken/available.",
        color=discord.Color.orange(),
    )
    embed.add_field(name="Active", value=str(len(active_invalid_vanities)), inline=True)
    embed.add_field(name="Expired Saved", value=str(len(expired_invalid_vanities)), inline=True)
    alert_channel_id = config.get("invalid_alert_channel_id")
    embed.add_field(name="Alert Channel", value=f"<#{alert_channel_id}>" if alert_channel_id else "Not set", inline=True)

    if not shown:
        embed.add_field(name="No Active Countdowns", value="No vanities are currently in the active countdown tracker.", inline=False)
        return embed

    lines = []
    for idx, record in enumerate(shown, start=1):
        code = record.get("code", "unknown")
        expires_dt = parse_iso_dt(record.get("expires_at"))
        expires_unix = int(expires_dt.timestamp()) if expires_dt else 0
        remaining = format_duration(seconds_until(record.get("expires_at")))
        lines.append(f"`{idx}.` `discord.gg/{code}` — `{remaining}` left • <t:{expires_unix}:R>")
    embed.add_field(name="Recent" if recent else "Expiring Soon", value="\n".join(lines), inline=False)
    embed.set_footer(text=f"Use {config.get('prefix', DEFAULT_PREFIX)}invalid <vanity> for one exact countdown.")
    return embed




def split_embed_lines(lines: list[str], max_chars: int = 1000) -> list[str]:
    chunks = []
    current = []
    current_len = 0
    for line in lines:
        line_len = len(line) + 1
        if current and current_len + line_len > max_chars:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks


def build_top_shortest_countdowns_embeds(limit: int = 50, per_embed: int = 45) -> list[discord.Embed]:
    limit = max(1, min(int(limit), 500))
    per_embed = max(10, min(int(per_embed), 50))
    records = active_sorted_expiring()[:limit]
    if not records:
        embed = discord.Embed(
            title="Top Shortest Vanity Countdowns",
            description="No vanities are currently in the active countdown tracker.",
            color=discord.Color.orange(),
        )
        embed.add_field(name="Active Countdowns", value=str(len(active_invalid_vanities)), inline=True)
        return [embed]

    pages: list[discord.Embed] = []
    total_pages = (len(records) + per_embed - 1) // per_embed
    alert_channel_id = config.get("invalid_alert_channel_id")

    for page_index in range(total_pages):
        start = page_index * per_embed
        chunk_records = records[start:start + per_embed]
        embed = discord.Embed(
            title=f"Top {len(records)} Shortest Vanity Countdowns",
            description="Active countdowns sorted by the least time remaining. Repeated not-taken rerun logs do not reset timers.",
            color=discord.Color.orange(),
        )
        embed.add_field(name="Active Countdowns", value=str(len(active_invalid_vanities)), inline=True)
        embed.add_field(name="Showing", value=f"{start + 1}-{start + len(chunk_records)} of {len(records)}", inline=True)
        embed.add_field(name="Alert Channel", value=f"<#{alert_channel_id}>" if alert_channel_id else "Not set", inline=True)
        embed.add_field(name="Length Filter", value=f"`{countdown_filter_label()}`", inline=True)

        lines = []
        for idx, record in enumerate(chunk_records, start=start + 1):
            code = record.get("code", "unknown")
            expires_dt = parse_iso_dt(record.get("expires_at"))
            expires_unix = int(expires_dt.timestamp()) if expires_dt else 0
            remaining = format_duration(seconds_until(record.get("expires_at")))
            started_dt = parse_iso_dt(record.get("taken_at") or record.get("invalid_at"))
            started_unix = int(started_dt.timestamp()) if started_dt else 0
            lines.append(f"`{idx}.` `discord.gg/{code}` — `{remaining}` left • ends <t:{expires_unix}:R> • since <t:{started_unix}:R>")

        for chunk_index, chunk in enumerate(split_embed_lines(lines, max_chars=1000), start=1):
            name = "Shortest Countdowns" if chunk_index == 1 else f"Shortest Countdowns Continued {chunk_index}"
            embed.add_field(name=name, value=chunk, inline=False)

        embed.set_footer(text=f"Page {page_index + 1}/{total_pages} • Use {config.get('prefix', DEFAULT_PREFIX)}countdown <vanity> for details. Max shown: 500.")
        pages.append(embed)

    return pages


def build_top_shortest_countdowns_embed(limit: int = 50) -> discord.Embed:
    # Compatibility wrapper for older call sites.
    return build_top_shortest_countdowns_embeds(limit=limit)[0]


def build_expired_list_embed(limit: int = 10) -> discord.Embed:
    limit = max(1, min(int(limit), 25))
    records = expired_sorted_recent()[:limit]
    embed = discord.Embed(
        title="Expired Vanity Countdown List",
        description="These moved out of active tracking after their 30-day timer ended.",
        color=discord.Color.dark_gray(),
    )
    embed.add_field(name="Expired Saved", value=str(len(expired_invalid_vanities)), inline=True)

    if not records:
        embed.add_field(name="No Expired Countdowns", value="No completed countdowns have been moved here yet.", inline=False)
        return embed

    lines = []
    for idx, record in enumerate(records, start=1):
        code = record.get("code", "unknown")
        expired_dt = parse_iso_dt(record.get("expired_at") or record.get("expires_at"))
        expired_unix = int(expired_dt.timestamp()) if expired_dt else 0
        lines.append(f"`{idx}.` `discord.gg/{code}` — expired <t:{expired_unix}:F> • <t:{expired_unix}:R>")
    embed.add_field(name="Recently Expired", value="\n".join(lines), inline=False)
    return embed


def build_countdown_complete_embed(record: dict) -> discord.Embed:
    code = record.get("code", "unknown")
    embed = discord.Embed(title="Vanity Countdown Complete", color=discord.Color.red())
    embed.add_field(name="Vanity", value=f"`discord.gg/{code}`", inline=False)
    embed.add_field(name="Not Taken Since", value=discord_relative(record.get("taken_at") or record.get("invalid_at")), inline=False)
    embed.add_field(name="Timer Expired", value=discord_relative(record.get("expired_at") or record.get("expires_at")), inline=False)
    embed.add_field(name="Moved To", value="Expired countdown list", inline=False)
    embed.set_footer(text="30 days elapsed since the bot detected taken/on-server → not-taken/available.")
    return embed


def move_due_countdowns_to_expired(*, source: str = "loop", alert_sent_default: bool = False, alert_skipped_reason: Optional[str] = None) -> list[dict]:
    now_dt = datetime.now(timezone.utc)
    moved = []
    for code, record in list(active_invalid_vanities.items()):
        expires_dt = parse_iso_dt(record.get("expires_at"))
        if not expires_dt or expires_dt > now_dt:
            continue

        active_invalid_vanities.pop(code, None)
        expired_record = dict(record)
        expired_record["expired_at"] = expires_dt.isoformat()
        expired_record["moved_at"] = now_iso()
        expired_record["moved_by"] = source
        expired_record["alert_sent"] = bool(alert_sent_default)
        expired_record["alert_sent_at"] = now_iso() if alert_sent_default else None
        if alert_skipped_reason:
            expired_record["alert_skipped_reason"] = alert_skipped_reason
        expired_invalid_vanities[code] = expired_record
        moved.append(expired_record)

    if moved:
        save_invalid_tracker()
        write_event_log("countdowns_moved_to_expired", {
            "source": source,
            "count": len(moved),
            "codes": [record.get("code") for record in moved[:50]],
        })
    return moved


async def verify_countdown_records(records: list[dict], *, max_checks: int = 100, only_remove_taken: bool = True) -> dict:
    """Live-check active countdown records against Discord.

    A countdown should only remain active while the invite returns 404 / available.
    If Discord returns 200 / taken, the record is removed immediately.
    """
    max_checks = max(1, min(int(max_checks), 500))
    checked = 0
    kept_available = 0
    removed_taken = 0
    skipped_untrackable = 0
    errors = 0
    blocked = 0
    samples_removed = []

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for record in list(records)[:max_checks]:
            code = clean_code(record.get("code", ""))
            if not code or code not in active_invalid_vanities:
                continue
            if not is_countdown_trackable(code):
                active_invalid_vanities.pop(code, None)
                skipped_untrackable += 1
                continue

            result, payload = await fetch_invite_status(session, code)
            checked += 1
            if result == "available":
                active_invalid_vanities[code]["last_verified_at"] = now_iso()
                active_invalid_vanities[code]["last_verified_status"] = "available"
                kept_available += 1
            elif result == "taken":
                remove_invalid_vanity(code, reason="live_verify_found_taken", seen_at=now_iso(), save=False)
                removed_taken += 1
                samples_removed.append(code)
            elif result == "blocked":
                blocked += 1
                break
            else:
                errors += 1

            # Small safety pause so a manual verify command does not hammer the endpoint.
            if checked < max_checks:
                await asyncio.sleep(0.35)

    if checked or removed_taken or skipped_untrackable or kept_available:
        save_invalid_tracker()
        write_event_log("countdowns_live_verified", {
            "checked": checked,
            "kept_available": kept_available,
            "removed_taken": removed_taken,
            "skipped_untrackable": skipped_untrackable,
            "errors": errors,
            "blocked": blocked,
            "removed_sample": samples_removed[:50],
        })

    return {
        "checked": checked,
        "kept_available": kept_available,
        "removed_taken": removed_taken,
        "skipped_untrackable": skipped_untrackable,
        "errors": errors,
        "blocked": blocked,
        "removed_sample": samples_removed[:20],
    }


async def verify_shortest_candidates_for_top(limit: int = 50) -> dict:
    """Verify enough shortest candidates so !topcountdowns doesn't display stale taken vanities."""
    limit = max(1, min(int(limit), 500))
    # Check a little more than the display limit so stale records are removed and replaced by the next shortest records.
    candidate_records = active_sorted_expiring()[:min(len(active_invalid_vanities), max(limit, min(limit + 25, 500)))]
    return await verify_countdown_records(candidate_records, max_checks=len(candidate_records))


# =========================
# DISCORD HELPERS
# =========================

async def get_channel(channel_id: Any):
    try:
        channel_id = int(channel_id)
    except Exception:
        return None

    channel = bot.get_channel(channel_id)
    if channel:
        return channel
    try:
        return await bot.fetch_channel(channel_id)
    except Exception:
        return None


async def safe_send(channel, content=None, embed=None, file=None):
    if not channel:
        return None
    try:
        return await channel.send(content=content, embed=embed, file=file)
    except Exception as e:
        logger.warning("Send failed: %s", e)
        return None


async def sleep_with_stop(seconds: int | float) -> bool:
    waited = 0.0
    while waited < float(seconds):
        if check_state["stop_requested"]:
            return True
        await asyncio.sleep(0.5)
        waited += 0.5
    return False


def find_role(ctx, role_input: str):
    if not role_input:
        return None

    role_input = role_input.strip()
    if role_input.lower() in {"none", "no", "null", "0"}:
        return None

    if role_input.startswith("<@&") and role_input.endswith(">"):
        role_id = role_input.replace("<@&", "").replace(">", "")
        if role_id.isdigit():
            return ctx.guild.get_role(int(role_id))

    if role_input.isdigit():
        return ctx.guild.get_role(int(role_input))

    lowered = role_input.lower()
    for role in ctx.guild.roles:
        if role.name.lower() == lowered:
            return role
    return None


def short_text(text: Any, limit: int = 180) -> str:
    text = str(text).replace("`", "'").replace("\n", " ").strip()
    return text[:limit] + ("..." if len(text) > limit else "")


def make_txt_file(list_name: str, label: str, words: list[str]) -> Optional[discord.File]:
    if not words:
        return None
    safe_name = clean_code(list_name) or "list"
    file_path = DATA_DIR / f"{safe_name}_{label}.txt"
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(", ".join(words))
    return discord.File(str(file_path), filename=file_path.name)


async def send_countdown_complete_alert(record: dict) -> bool:
    channel_id = config.get("invalid_alert_channel_id")
    channel = await get_channel(channel_id) if channel_id else None
    if not channel:
        logger.warning("Countdown completed for %s but invalid_alert_channel_id is not set or could not be fetched.", record.get("code"))
        return False

    embed = build_countdown_complete_embed(record)
    sent = await safe_send(channel, content="@everyone", embed=embed)
    return sent is not None


async def process_due_countdowns(source: str = "loop") -> list[dict]:
    moved = move_due_countdowns_to_expired(source=source, alert_sent_default=False)
    if not moved:
        return []

    changed = False
    for record in moved:
        sent = await send_countdown_complete_alert(record)
        code = record.get("code")
        if code not in expired_invalid_vanities:
            continue

        if sent:
            expired_invalid_vanities[code]["alert_sent"] = True
            expired_invalid_vanities[code]["alert_sent_at"] = now_iso()
            expired_invalid_vanities[code]["alert_channel_id"] = config.get("invalid_alert_channel_id")
        else:
            expired_invalid_vanities[code]["alert_sent"] = False
            expired_invalid_vanities[code]["alert_skipped_reason"] = "Alert channel missing or send failed."
        changed = True

    if changed:
        save_invalid_tracker()
    return moved

# =========================
# INVITE CHECKING
# =========================

async def fetch_invite_status(session: aiohttp.ClientSession, code: str) -> Tuple[str, Optional[Any]]:
    """
    available = Discord says Unknown Invite / 404. This usually means not taken.
    taken     = Invite exists / 200. This means currently on a server.
    """
    code = clean_code(code)
    if not code:
        return "error", "Empty code"

    url = f"{API_BASE}/{code}?with_counts=true&with_expiration=true"
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 DiscordBot VanityChecker/3.0"
    }

    for attempt in range(1, MAX_RETRIES + 1):
        if check_state["stop_requested"]:
            return "stopped", None

        try:
            async with session.get(url, headers=headers) as resp:
                status = resp.status
                content_type = resp.headers.get("content-type", "").lower()
                text = await resp.text()

                if "application/json" not in content_type:
                    lowered = text.lower()
                    if "cloudflare" in lowered or "cf-challenge" in lowered or "challenge-platform" in lowered or "<html" in lowered:
                        return "blocked", f"Cloudflare/non-JSON response. HTTP {status}. Cool down before checking again."
                    return "error", f"Non-JSON response. HTTP {status}. Content-Type: {content_type or 'unknown'}"

                try:
                    data = json.loads(text) if text else {}
                except json.JSONDecodeError:
                    return "error", f"JSON decode failed. HTTP {status}."

                if status == 200:
                    return "taken", data

                if status == 404:
                    return "available", data

                if status == 429:
                    retry_after = data.get("retry_after") or resp.headers.get("Retry-After") or 10
                    try:
                        retry_after = float(retry_after)
                    except Exception:
                        retry_after = 10.0

                    if attempt < MAX_RETRIES:
                        logger.warning("Rate limited on %s. Waiting %.2fs", code, retry_after)
                        stopped = await sleep_with_stop(retry_after + 1)
                        if stopped:
                            return "stopped", None
                        continue
                    return "rate_limited", f"Still rate limited after {MAX_RETRIES} retries."

                if status in {500, 502, 503, 504} and attempt < MAX_RETRIES:
                    wait_time = 15 * attempt
                    stopped = await sleep_with_stop(wait_time)
                    if stopped:
                        return "stopped", None
                    continue

                return "error", f"HTTP {status}: {short_text(data)}"

        except aiohttp.ClientError as e:
            if attempt < MAX_RETRIES:
                stopped = await sleep_with_stop(10 * attempt)
                if stopped:
                    return "stopped", None
                continue
            return "error", f"Network error: {short_text(e)}"
        except asyncio.TimeoutError:
            if attempt < MAX_RETRIES:
                stopped = await sleep_with_stop(10 * attempt)
                if stopped:
                    return "stopped", None
                continue
            return "error", "Request timed out"
        except Exception as e:
            return "error", f"Unexpected error: {short_text(e)}"

    return "error", "Unknown error"

# =========================
# EMBEDS / OUTPUT
# =========================

def build_summary_embed(list_name, processed, available, taken, errors, blocked, added, removed, stopped, updated_at):
    if blocked:
        title = f"Check Paused: {list_name}"
        color = discord.Color.red()
    elif stopped:
        title = f"Check Stopped: {list_name}"
        color = discord.Color.orange()
    else:
        title = f"Check Finished: {list_name}"
        color = discord.Color.green()

    embed = discord.Embed(title=title, color=color)
    embed.add_field(name="Processed", value=str(processed), inline=True)
    embed.add_field(name="Not Taken / Available", value=str(available), inline=True)
    embed.add_field(name="Taken / On Server", value=str(taken), inline=True)
    embed.add_field(name="Errors", value=str(errors), inline=True)
    embed.add_field(name="Cloudflare Blocks", value=str(blocked), inline=True)
    embed.add_field(name="Added To Available TXT", value=str(added), inline=True)
    embed.add_field(name="Removed From Available TXT", value=str(removed), inline=True)
    embed.add_field(name="List Last Updated", value=format_time(updated_at), inline=False)
    embed.set_footer(text="Vanity checker • 404 = not taken/available • 200 = taken/on server • TXT stores not-taken words only")
    return embed


async def send_words_output(channel, title: str, words: list[str]):
    if not words:
        await safe_send(channel, f"{title}: `None`")
        return

    paragraph = ", ".join(words)
    if len(paragraph) <= 1850:
        await safe_send(channel, f"{title}:\n```txt\n{paragraph}\n```")
        return

    label = clean_code(title.replace(" ", "_")) or "words"
    file_path = DATA_DIR / f"{label}.txt"
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(paragraph)
    await safe_send(channel, content=f"{title} was too long, so here is the txt file:", file=discord.File(str(file_path), filename=file_path.name))

# =========================
# CHECK RUNNERS
# =========================

async def run_list_check(list_name: str, list_data: dict, manual_ctx=None):
    list_data = normalize_list_record(list_data)

    available_channel = await get_channel(list_data.get("available_channel_id"))
    taken_channel = await get_channel(list_data.get("taken_channel_id"))
    log_channel = await get_channel(list_data.get("log_channel_id"))
    summary_channel = await get_channel(list_data.get("summary_channel_id"))
    ping_role_id = list_data.get("ping_role_id")
    words = list_data.get("words", [])

    if not available_channel or not taken_channel or not log_channel or not summary_channel:
        if manual_ctx:
            await manual_ctx.send(
                f"`{list_name}` has a missing/broken channel setup. Use "
                f"`{config['prefix']}setchannels {list_name} <available_channel> <taken_channel> <log_channel> <summary_channel>`."
            )
        return

    cleaned_codes = []
    seen = set()
    for word in words:
        code = clean_code(word)
        if not code or code in seen:
            continue
        seen.add(code)
        cleaned_codes.append(code)

    if not cleaned_codes:
        await safe_send(summary_channel, f"`{list_name}` has no usable words.")
        return

    cleaned_codes = cleaned_codes[:MAX_CODES_PER_LIST]

    check_state["running"] = True
    check_state["stop_requested"] = False
    check_state["current"] = 0
    check_state["total"] = len(cleaned_codes)
    check_state["mode"] = list_name

    available_count = 0
    taken_count = 0
    error_count = 0
    blocked_count = 0
    added_count = 0
    removed_count = 0
    available_found = []
    taken_found = []
    error_found = []

    status_msg = await safe_send(summary_channel, f"Checking `{list_name}` — `{len(cleaned_codes)}` word(s)...")

    timeout = aiohttp.ClientTimeout(total=30)
    batch_size = max(1, int(config.get("batch_size", DEFAULT_BATCH_SIZE)))
    delay_seconds = max(3, int(config.get("delay_seconds", DEFAULT_DELAY_SECONDS)))
    batch_cooldown = max(10, int(config.get("batch_cooldown_seconds", DEFAULT_BATCH_COOLDOWN_SECONDS)))

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for index, code in enumerate(cleaned_codes, start=1):
                check_state["current"] = index

                if check_state["stop_requested"]:
                    break

                result, payload = await fetch_invite_status(session, code)
                length = len(code)

                if result == "stopped":
                    break

                if result == "available":
                    # 404 / Unknown Invite = NOT on a server.
                    # Only start a countdown when the persistent last-known status
                    # was taken/on-server. This prevents first scans, lost TXT files,
                    # or redeploys from creating fake fresh timers for everything.
                    available_count += 1
                    available_found.append(code)

                    previous_status = get_last_vanity_status(code)
                    was_added_to_txt = add_unavailable(code)
                    if was_added_to_txt:
                        added_count += 1
                        await safe_send(log_channel, f"`discord.gg/{code}` is not taken/available and was added to the not-taken TXT file.")

                    transition_time = now_iso()
                    if previous_status == "taken":
                        record = add_invalid_vanity(code, list_name, taken_at=transition_time, source="checker_taken_to_available_transition")
                        if record:
                            await safe_send(
                                log_channel,
                                f"Started 30-day countdown for `discord.gg/{code}`. Ends {discord_relative(record.get('expires_at'))}."
                            )
                        else:
                            await safe_send(
                                log_channel,
                                f"Skipped countdown for `discord.gg/{code}` because it does not match the countdown length filter `{countdown_filter_label()}`."
                            )
                    elif was_added_to_txt and previous_status is None:
                        write_event_log("available_seeded_without_countdown", {
                            "code": code,
                            "list": list_name,
                            "reason": "no_previous_taken_status",
                        })

                    set_last_vanity_status(code, "available", list_name=list_name, source="checker", checked_at=transition_time, save=True)

                    await safe_send(available_channel, f"discord.gg/{code}")

                elif result == "taken":
                    # 200 / Invite exists = currently on a server. Make sure it is NOT inside the TXT file.
                    taken_count += 1
                    taken_found.append(code)

                    transition_time = now_iso()
                    if remove_unavailable(code):
                        removed_count += 1
                        await safe_send(log_channel, f"`discord.gg/{code}` is taken/on a server and was removed from the not-taken TXT file.")

                    removed_active, removed_expired = remove_invalid_vanity(code, reason="became_taken_on_server", seen_at=transition_time, save=True)
                    if removed_active or removed_expired:
                        await safe_send(log_channel, f"Removed countdown for `discord.gg/{code}` because it is taken/on a server again.")

                    set_last_vanity_status(code, "taken", list_name=list_name, source="checker", checked_at=transition_time, save=True)

                    await safe_send(taken_channel, f"discord.gg/{code}")
                    await safe_send(log_channel, f"{length} letters | Taken/on server: `discord.gg/{code}`")

                elif result == "blocked":
                    blocked_count += 1
                    error_found.append(f"{code} - blocked")
                    await safe_send(log_channel, f"Cloudflare block while checking `discord.gg/{code}`: `{short_text(payload, 350)}`")
                    await safe_send(summary_channel, f"Cloudflare blocked the checker. Pausing `{list_name}` for safety. Wait at least `{BLOCK_COOLDOWN_SECONDS // 60}` minutes before trying again.")
                    check_state["stop_requested"] = True
                    break

                elif result == "rate_limited":
                    error_count += 1
                    error_found.append(f"{code} - rate limited")
                    await safe_send(log_channel, f"Rate limited checking `discord.gg/{code}`: `{short_text(payload, 350)}`")

                else:
                    error_count += 1
                    error_found.append(f"{code} - {short_text(payload, 90)}")
                    await safe_send(log_channel, f"Error checking `discord.gg/{code}`: `{short_text(payload, 350)}`")

                if status_msg and (index == 1 or index % 10 == 0 or index == len(cleaned_codes)):
                    try:
                        await status_msg.edit(
                            content=(
                                f"Checking `{list_name}`...\n"
                                f"Progress: `{index}/{len(cleaned_codes)}`\n"
                                f"Not taken: `{available_count}` | Taken/on server: `{taken_count}` | Errors: `{error_count}` | Blocks: `{blocked_count}`"
                            )
                        )
                    except Exception:
                        pass

                if index < len(cleaned_codes):
                    if index % batch_size == 0:
                        stopped = await sleep_with_stop(batch_cooldown)
                    else:
                        stopped = await sleep_with_stop(delay_seconds)
                    if stopped:
                        break

    finally:
        stopped = check_state["stop_requested"]

        embed = build_summary_embed(
            list_name=list_name,
            processed=check_state["current"],
            available=available_count,
            taken=taken_count,
            errors=error_count,
            blocked=blocked_count,
            added=added_count,
            removed=removed_count,
            stopped=stopped,
            updated_at=list_data.get("updated_at")
        )

        ping_text = f"<@&{ping_role_id}> " if ping_role_id else ""
        await safe_send(summary_channel, content=ping_text, embed=embed)

        # End-of-check word lists go to the summary channel.
        await send_words_output(summary_channel, f"Not taken / available words for `{list_name}`", available_found)
        await send_words_output(summary_channel, f"Taken / currently on a server words for `{list_name}`", taken_found)
        if error_found:
            await send_words_output(summary_channel, f"Errors / skipped words for `{list_name}`", error_found)

        check_state["running"] = False
        check_state["stop_requested"] = False
        check_state["current"] = 0
        check_state["total"] = 0
        check_state["mode"] = None


async def run_all_checks(manual_ctx=None):
    if check_state["running"]:
        if manual_ctx:
            await manual_ctx.send(
                f"A check is already running: `{check_state['mode']}` "
                f"`{check_state['current']}/{check_state['total']}`"
            )
        return

    load_unavailable_cache()

    if not config["lists"]:
        if manual_ctx:
            await manual_ctx.send("No saved lists found.")
        return

    list_cooldown = max(10, int(config.get("list_cooldown_seconds", DEFAULT_LIST_COOLDOWN_SECONDS)))
    items = list(config["lists"].items())

    for idx, (list_name, list_data) in enumerate(items, start=1):
        await run_list_check(list_name, list_data, manual_ctx=manual_ctx)
        if check_state["stop_requested"]:
            break
        if idx < len(items):
            await sleep_with_stop(list_cooldown)

# =========================
# AUTO LOOP
# =========================

@tasks.loop(minutes=1)
async def auto_check_loop():
    if not config.get("auto_enabled", False):
        return

    minutes = max(5, int(config.get("auto_minutes", DEFAULT_AUTO_MINUTES)))

    if not hasattr(auto_check_loop, "counter"):
        auto_check_loop.counter = 0

    auto_check_loop.counter += 1
    if auto_check_loop.counter < minutes:
        return

    auto_check_loop.counter = 0
    if not check_state["running"]:
        await run_all_checks()


@tasks.loop(minutes=1)
async def invalid_countdown_loop():
    await process_due_countdowns(source="loop")


@tasks.loop(minutes=2)
async def autosave_loop():
    # Extra safety net: saves all in-memory stores every 2 minutes.
    # Normal commands and transitions still save immediately.
    save_all_data(reason="autosave_loop")


# =========================
# SLASH HELP PAGES
# =========================

HELP_PAGE_TITLES = [
    "Setup",
    "Lists",
    "Checks",
    "Countdowns",
    "Backfill",
    "Data / Saves",
]


def build_help_page(page: int = 1) -> discord.Embed:
    p = config.get("prefix", DEFAULT_PREFIX)
    total = len(HELP_PAGE_TITLES)
    page = max(1, min(int(page), total))
    title = HELP_PAGE_TITLES[page - 1]
    embed = discord.Embed(
        title=f"Vanity Checker Help — {title}",
        description="Use the buttons to switch pages. Prefix commands still work, and this slash help is synced as `/help`.",
        color=discord.Color.blurple(),
    )

    if page == 1:
        embed.add_field(name="Add a List", value=(
            f"`{p}addlist <name> <available_channel> <taken_channel> <log_channel> <summary_channel> <ping_role|none> <words>`\n"
            f"Example:\n`{p}addlist 4letters #available #taken #log #summary none love, hate, void, glow`"
        ), inline=False)
        embed.add_field(name="Alert Channel", value=f"`{p}setalertchannel #channel` — choose where @everyone completion alerts go", inline=False)
        embed.add_field(name="Persistence", value="For Railway, attach a Volume and set `DATA_DIR=/data` so lists/countdowns survive redeploys.", inline=False)
    elif page == 2:
        embed.add_field(name="Manage Lists", value=(
            f"`{p}lists`\n`{p}listinfo <name>`\n`{p}addwords <name> <words>`\n"
            f"`{p}removewords <name> <words>`\n`{p}setchannels <name> <available> <taken> <log> <summary>`\n"
            f"`{p}setpingrole <name> <role|none>`\n`{p}removelist <name>`\n`{p}clearlists`"
        ), inline=False)
        embed.add_field(name="TXT Files", value=f"`{p}unavailablecount <length>`\n`{p}getunavailable <length>`\n`{p}clearunavailable [length]`", inline=False)
    elif page == 3:
        embed.add_field(name="Manual Checks", value=f"`{p}checklist <name>`\n`{p}checkall`\n`{p}stop`", inline=False)
        embed.add_field(name="Auto Checks", value=f"`{p}autocheck <minutes>`\n`{p}autostop`\n`{p}autostatus`", inline=False)
        embed.add_field(name="Rate Settings", value=f"`{p}ratelimit <delay_seconds> <batch_size> <batch_cooldown_seconds> <list_cooldown_seconds>`", inline=False)
    elif page == 4:
        embed.add_field(name="Countdown Logic", value="Countdowns start only on a confirmed `taken/on-server → not-taken/available` transition. Repeated not-taken rerun logs do not reset the timer.", inline=False)
        embed.add_field(name="Countdown Commands", value=(
            f"`{p}invalid`\n`{p}invalid <vanity>`\n`{p}countdown <vanity>`\n"
            f"`{p}invalidrecent [limit]`\n`{p}invalidexpiring [limit]`\n`{p}topcountdowns [limit up to 500]`\n"
            f"`{p}invalidexpired [limit]`\n`{p}invalidcount`\n`{p}invalidexport`"
        ), inline=False)
        embed.add_field(name="Admin Countdown Tools", value=f"`{p}verifycountdowns [limit]`\n`{p}setcountdownlengths <min> [max]`\n`{p}prunecountdowns`\n`{p}resetcountdowns`", inline=False)
    elif page == 5:
        embed.add_field(name="Backfill Logic", value="Backfill groups logs by vanity, sorts oldest→newest, then uses the FIRST not-taken log after the most recent taken log. If the newest state is taken, no countdown is kept.", inline=False)
        embed.add_field(name="Backfill Commands", value=(
            f"`{p}backfillchannel #channel [message_limit]`\n"
            f"`{p}backfillinvalid [messages_per_log_channel]`\n"
            f"`{p}backfillstatus [#channel]`\n"
            f"`{p}backfilltimeline <vanity> [runs]` — compressed runs like `taken x10`\n"
            f"`{p}resetbackfill #channel`"
        ), inline=False)
        embed.add_field(name="Clean Rebuild", value=f"`{p}resetcountdowns`\n`{p}resetbackfill #log`\n`{p}backfillchannel #log 10000`\n`{p}verifycountdowns 500`", inline=False)
    else:
        embed.add_field(name="Data / Saves", value=f"`{p}datastatus`\n`{p}savedata`\n`{p}exportdata`", inline=False)
        embed.add_field(name="Files Saved", value="`vanity_config.json`, `vanity_statuses.json`, `invalid_vanities.json`, `expired_invalid_vanities.json`, `backfill_scan_state.json`, `backfill_transition_events.json`, and backups in `data/backups/`.", inline=False)
        embed.add_field(name="Prefix", value=f"`{p}setprefix <prefix>`", inline=False)

    embed.set_footer(text=f"Page {page}/{total}")
    return embed


class HelpPager(discord.ui.View):
    def __init__(self, start_page: int = 1):
        super().__init__(timeout=180)
        self.page = max(1, min(int(start_page), len(HELP_PAGE_TITLES)))
        self.update_buttons()

    def update_buttons(self):
        self.prev_button.disabled = self.page <= 1
        self.next_button.disabled = self.page >= len(HELP_PAGE_TITLES)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(1, self.page - 1)
        self.update_buttons()
        await interaction.response.edit_message(embed=build_help_page(self.page), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(len(HELP_PAGE_TITLES), self.page + 1)
        self.update_buttons()
        await interaction.response.edit_message(embed=build_help_page(self.page), view=self)


@bot.tree.command(name="help", description="Show paged vanity bot help.")
@app_commands.describe(page="Help page number")
async def slash_help(interaction: discord.Interaction, page: int = 1):
    view = HelpPager(start_page=page)
    await interaction.response.send_message(embed=build_help_page(view.page), view=view, ephemeral=True)

# =========================
# COMMANDS
# =========================

@bot.command(name="help")
async def help_command(ctx, page: int = 1):
    """Prefix fallback help. The slash /help command has the button pages."""
    await ctx.send(embed=build_help_page(page), view=HelpPager(start_page=page))


@bot.command()
@commands.has_permissions(administrator=True)
async def setprefix(ctx, new_prefix: str):
    if len(new_prefix) > 5:
        await ctx.send("Prefix must be 5 characters or less.")
        return
    config["prefix"] = new_prefix
    save_config()
    await ctx.send(f"Prefix changed to `{new_prefix}`. All help embeds will now show `{new_prefix}` commands.")


@bot.command()
@commands.has_permissions(administrator=True)
async def ratelimit(ctx, delay_seconds: int, batch_size: int, batch_cooldown_seconds: int, list_cooldown_seconds: int):
    if delay_seconds < 3:
        await ctx.send("Delay must be at least `3` seconds.")
        return
    if batch_size < 1 or batch_size > 20:
        await ctx.send("Batch size must be between `1` and `20`.")
        return
    if batch_cooldown_seconds < 10:
        await ctx.send("Batch cooldown must be at least `10` seconds.")
        return
    if list_cooldown_seconds < 10:
        await ctx.send("List cooldown must be at least `10` seconds.")
        return

    config["delay_seconds"] = delay_seconds
    config["batch_size"] = batch_size
    config["batch_cooldown_seconds"] = batch_cooldown_seconds
    config["list_cooldown_seconds"] = list_cooldown_seconds
    save_config()
    await ctx.send(
        "Rate settings saved:\n"
        f"Delay: `{delay_seconds}s`\n"
        f"Batch size: `{batch_size}`\n"
        f"Batch cooldown: `{batch_cooldown_seconds}s`\n"
        f"List cooldown: `{list_cooldown_seconds}s`"
    )


@bot.command()
@commands.has_permissions(administrator=True)
async def addlist(ctx, name: str, available_channel: discord.TextChannel, taken_channel: discord.TextChannel, log_channel: discord.TextChannel, summary_channel: discord.TextChannel, ping_role_input: str, *, words: str):
    cleaned = parse_words(words)
    if not cleaned:
        await ctx.send("No usable words found.")
        return
    if len(cleaned) > MAX_CODES_PER_LIST:
        await ctx.send(f"Too many words. Max per list is `{MAX_CODES_PER_LIST}`.")
        return

    ping_role = find_role(ctx, ping_role_input)
    if ping_role_input.lower() not in {"none", "no", "null", "0"} and not ping_role:
        await ctx.send("I could not find that ping role. Use a role mention, role ID, exact role name, or `none`.")
        return

    name = clean_code(name)
    if not name:
        await ctx.send("Invalid list name.")
        return

    timestamp = now_iso()
    old_created = config["lists"].get(name, {}).get("created_at", timestamp)

    config["lists"][name] = {
        "available_channel_id": available_channel.id,
        "taken_channel_id": taken_channel.id,
        "log_channel_id": log_channel.id,
        "summary_channel_id": summary_channel.id,
        "ping_role_id": ping_role.id if ping_role else None,
        "words": cleaned,
        "created_at": old_created,
        "updated_at": timestamp
    }
    save_config()

    await ctx.send(
        f"Saved list `{name}` with `{len(cleaned)}` word(s).\n"
        f"Not taken/available channel: {available_channel.mention}\n"
        f"Taken/on-server channel: {taken_channel.mention}\n"
        f"Logs: {log_channel.mention}\n"
        f"Summaries: {summary_channel.mention}\n"
        f"Ping role: {ping_role.mention if ping_role else '`None`'}\n"
        f"Updated: {format_time(timestamp)}"
    )


@bot.command()
@commands.has_permissions(administrator=True)
async def addwords(ctx, name: str, *, words: str):
    name = clean_code(name)
    if name not in config["lists"]:
        await ctx.send(f"No list named `{name}` exists.")
        return

    new_words = parse_words(words)
    existing = list(config["lists"][name].get("words", []))
    existing_set = set(existing)
    added = []

    for code in new_words:
        if code not in existing_set:
            existing.append(code)
            existing_set.add(code)
            added.append(code)

    if len(existing) > MAX_CODES_PER_LIST:
        await ctx.send(f"That would go over the max of `{MAX_CODES_PER_LIST}` words per list.")
        return

    config["lists"][name]["words"] = existing
    config["lists"][name]["updated_at"] = now_iso()
    save_config()
    await ctx.send(f"Added `{len(added)}` new word(s) to `{name}`. Total: `{len(existing)}`.")


@bot.command()
@commands.has_permissions(administrator=True)
async def removewords(ctx, name: str, *, words: str):
    name = clean_code(name)
    if name not in config["lists"]:
        await ctx.send(f"No list named `{name}` exists.")
        return

    remove_set = set(parse_words(words))
    before = config["lists"][name].get("words", [])
    after = [w for w in before if clean_code(w) not in remove_set]
    removed = len(before) - len(after)

    config["lists"][name]["words"] = after
    config["lists"][name]["updated_at"] = now_iso()
    save_config()
    await ctx.send(f"Removed `{removed}` word(s) from `{name}`. Total: `{len(after)}`.")


@bot.command()
@commands.has_permissions(administrator=True)
async def setchannels(ctx, name: str, available_channel: discord.TextChannel, taken_channel: discord.TextChannel, log_channel: discord.TextChannel, summary_channel: discord.TextChannel):
    name = clean_code(name)
    if name not in config["lists"]:
        await ctx.send(f"No list named `{name}` exists.")
        return

    config["lists"][name] = normalize_list_record(config["lists"][name])
    config["lists"][name]["available_channel_id"] = available_channel.id
    config["lists"][name]["taken_channel_id"] = taken_channel.id
    config["lists"][name]["log_channel_id"] = log_channel.id
    config["lists"][name]["summary_channel_id"] = summary_channel.id
    config["lists"][name]["updated_at"] = now_iso()
    save_config()
    await ctx.send(
        f"Updated channels for `{name}`:\n"
        f"Not taken/available: {available_channel.mention}\n"
        f"Taken/on-server: {taken_channel.mention}\n"
        f"Logs: {log_channel.mention}\n"
        f"Summaries: {summary_channel.mention}"
    )


@bot.command()
@commands.has_permissions(administrator=True)
async def setpingrole(ctx, name: str, *, role_input: str):
    name = clean_code(name)
    if name not in config["lists"]:
        await ctx.send(f"No list named `{name}` exists.")
        return

    role = find_role(ctx, role_input)
    if role_input.lower() not in {"none", "no", "null", "0"} and not role:
        await ctx.send("I could not find that role. Use a mention, role ID, exact role name, or `none`.")
        return

    config["lists"][name]["ping_role_id"] = role.id if role else None
    config["lists"][name]["updated_at"] = now_iso()
    save_config()
    await ctx.send(f"Updated ping role for `{name}` to {role.mention if role else '`None`'}.")


@bot.command()
async def lists(ctx):
    if not config["lists"]:
        await ctx.send("No saved lists yet.")
        return

    embed = discord.Embed(title="Saved Vanity Lists", color=discord.Color.blurple())
    for name, raw_data in config["lists"].items():
        data = normalize_list_record(raw_data)
        ping_value = f"<@&{data.get('ping_role_id')}>" if data.get("ping_role_id") else "`None`"
        embed.add_field(
            name=name,
            value=(
                f"Words: `{len(data.get('words', []))}`\n"
                f"Not taken/available: <#{data.get('available_channel_id')}>\n"
                f"Taken/on-server: <#{data.get('taken_channel_id')}>\n"
                f"Logs: <#{data.get('log_channel_id')}>\n"
                f"Summaries: <#{data.get('summary_channel_id')}>\n"
                f"Ping: {ping_value}\n"
                f"Updated: {format_time(data.get('updated_at'))}"
            ),
            inline=False
        )
    await ctx.send(embed=embed)


@bot.command()
async def listinfo(ctx, name: str):
    name = clean_code(name)
    if name not in config["lists"]:
        await ctx.send(f"No list named `{name}` exists.")
        return

    data = normalize_list_record(config["lists"][name])
    embed = discord.Embed(title=f"List Info: {name}", color=discord.Color.blurple())
    embed.add_field(name="Words", value=str(len(data.get("words", []))), inline=True)
    embed.add_field(name="Created", value=format_time(data.get("created_at")), inline=False)
    embed.add_field(name="Last Updated", value=format_time(data.get("updated_at")), inline=False)
    embed.add_field(name="Not Taken / Available Channel", value=f"<#{data.get('available_channel_id')}>", inline=True)
    embed.add_field(name="Taken / On-Server Channel", value=f"<#{data.get('taken_channel_id')}>", inline=True)
    embed.add_field(name="Log Channel", value=f"<#{data.get('log_channel_id')}>", inline=True)
    embed.add_field(name="Summary Channel", value=f"<#{data.get('summary_channel_id')}>", inline=True)
    embed.add_field(name="Ping Role", value=f"<@&{data.get('ping_role_id')}>" if data.get("ping_role_id") else "None", inline=True)

    preview = ", ".join(data.get("words", [])[:35])
    if len(data.get("words", [])) > 35:
        preview += "..."
    embed.add_field(name="Word Preview", value=preview or "None", inline=False)
    await ctx.send(embed=embed)


@bot.command()
@commands.has_permissions(administrator=True)
async def removelist(ctx, name: str):
    name = clean_code(name)
    if name not in config["lists"]:
        await ctx.send(f"No list named `{name}` exists.")
        return
    del config["lists"][name]
    save_config()
    await ctx.send(f"Removed list `{name}`.")


@bot.command()
@commands.has_permissions(administrator=True)
async def clearlists(ctx):
    config["lists"] = {}
    save_config()
    await ctx.send("Removed all saved lists.")


@bot.command()
@commands.has_permissions(administrator=True)
async def checklist(ctx, name: str):
    name = clean_code(name)
    if name not in config["lists"]:
        await ctx.send(f"No list named `{name}` exists.")
        return
    if check_state["running"]:
        await ctx.send(f"A check is already running: `{check_state['mode']}` `{check_state['current']}/{check_state['total']}`")
        return
    await ctx.send(f"Starting check for `{name}`.")
    load_unavailable_cache()
    await run_list_check(name, config["lists"][name], manual_ctx=ctx)


@bot.command()
@commands.has_permissions(administrator=True)
async def checkall(ctx):
    await ctx.send("Starting all saved list checks.")
    await run_all_checks(manual_ctx=ctx)


@bot.command()
async def stop(ctx):
    if not check_state["running"]:
        await ctx.send("No check is currently running.")
        return
    check_state["stop_requested"] = True
    await ctx.send(f"Stop requested for `{check_state['mode']}`. Progress: `{check_state['current']}/{check_state['total']}`")


@bot.command()
@commands.has_permissions(administrator=True)
async def autocheck(ctx, minutes: int):
    if minutes < 15:
        await ctx.send("Use at least `15` minutes to reduce rate-limit/Cloudflare issues.")
        return
    config["auto_enabled"] = True
    config["auto_minutes"] = minutes
    save_config()
    auto_check_loop.counter = 0
    await ctx.send(f"Automatic checks enabled every `{minutes}` minute(s).")


@bot.command()
@commands.has_permissions(administrator=True)
async def autostop(ctx):
    config["auto_enabled"] = False
    save_config()
    await ctx.send("Automatic checks disabled.")


@bot.command()
async def autostatus(ctx):
    await ctx.send(
        f"Auto checks: `{'Enabled' if config['auto_enabled'] else 'Disabled'}`\n"
        f"Interval: `{config['auto_minutes']}` minute(s)\n"
        f"Saved lists: `{len(config['lists'])}`\n"
        f"Currently running: `{'Yes' if check_state['running'] else 'No'}`\n"
        f"Delay: `{config['delay_seconds']}s` | Batch: `{config['batch_size']}` | Batch cooldown: `{config['batch_cooldown_seconds']}s`"
    )


@bot.command()
@commands.has_permissions(administrator=True)
async def setalertchannel(ctx, channel: discord.TextChannel):
    """Choose where @everyone countdown-complete alerts go."""
    config["invalid_alert_channel_id"] = channel.id
    save_config()
    await ctx.send(f"Countdown completion alerts will now go to {channel.mention} with `@everyone`.")


@bot.command(name="invalid")
async def invalid_command(ctx, vanity: str = None):
    """Show active countdowns, or a specific vanity countdown."""
    await process_due_countdowns(source="command")
    if vanity is None:
        await ctx.send(embed=build_active_list_embed(limit=10, recent=True))
        return

    code = clean_code(vanity)
    if not code:
        await ctx.send("Give me a valid vanity, like `example` or `discord.gg/example`.")
        return

    if code in active_invalid_vanities:
        await ctx.send(embed=build_invalid_detail_embed(active_invalid_vanities[code], expired=False))
        return

    if code in expired_invalid_vanities:
        await ctx.send(embed=build_invalid_detail_embed(expired_invalid_vanities[code], expired=True))
        return

    await ctx.send(f"`discord.gg/{code}` is not in the active or expired countdown tracker.")


@bot.command(name="countdown")
async def countdown_command(ctx, vanity: str):
    await process_due_countdowns(source="command")
    code = clean_code(vanity)
    if not code:
        await ctx.send("Give me a valid vanity, like `example` or `discord.gg/example`.")
        return

    if code in active_invalid_vanities:
        await ctx.send(embed=build_invalid_detail_embed(active_invalid_vanities[code], expired=False))
    elif code in expired_invalid_vanities:
        await ctx.send(embed=build_invalid_detail_embed(expired_invalid_vanities[code], expired=True))
    else:
        await ctx.send(f"`discord.gg/{code}` is not in the countdown tracker.")


@bot.command(name="invalidrecent")
async def invalidrecent(ctx, limit: int = 10):
    await process_due_countdowns(source="command")
    await ctx.send(embed=build_active_list_embed(limit=limit, recent=True))


@bot.command(name="invalidexpiring")
async def invalidexpiring(ctx, limit: int = 10):
    await process_due_countdowns(source="command")
    await ctx.send(embed=build_active_list_embed(limit=limit, recent=False))


@bot.command(name="topcountdowns", aliases=["shortestcountdowns", "invalidtop", "topinvalid", "soonestcountdowns"])
async def topcountdowns(ctx, limit: int = 50):
    """Show active countdowns with the shortest time remaining. Supports up to 500."""
    limit = max(1, min(int(limit), 500))
    await process_due_countdowns(source="command")
    if config.get("topcountdowns_live_verify", True):
        msg = await ctx.send(f"Verifying the shortest countdown candidates live before showing the top `{limit}`...")
        stats = await verify_shortest_candidates_for_top(limit=limit)
        try:
            await msg.edit(content=f"Verified `{stats['checked']}` countdown candidate(s). Removed `{stats['removed_taken']}` that are now taken and `{stats['skipped_untrackable']}` outside the length filter.")
        except Exception:
            pass
    for embed in build_top_shortest_countdowns_embeds(limit=limit):
        await ctx.send(embed=embed)


@bot.command(name="invalidexpired", aliases=["expiredinvalid"])
async def invalidexpired(ctx, limit: int = 10):
    await ctx.send(embed=build_expired_list_embed(limit=limit))


@bot.command(name="verifycountdowns", aliases=["verifyinvalid", "checkcountdowns"])
@commands.has_permissions(administrator=True)
async def verifycountdowns(ctx, limit: int = 100):
    """Live-check active countdowns and remove any that are currently taken/on-server."""
    limit = max(1, min(int(limit), 500))
    await process_due_countdowns(source="command")
    records = active_sorted_expiring()[:limit]
    if not records:
        await ctx.send("No active countdowns to verify.")
        return
    status_msg = await ctx.send(f"Live-verifying `{len(records)}` active countdown(s). This can take a bit...")
    stats = await verify_countdown_records(records, max_checks=len(records))
    embed = discord.Embed(title="Countdown Live Verification Complete", color=discord.Color.blurple())
    embed.add_field(name="Checked", value=str(stats["checked"]), inline=True)
    embed.add_field(name="Still Not Taken / Available", value=str(stats["kept_available"]), inline=True)
    embed.add_field(name="Removed Because Taken", value=str(stats["removed_taken"]), inline=True)
    embed.add_field(name="Removed By Length Filter", value=str(stats["skipped_untrackable"]), inline=True)
    embed.add_field(name="Errors", value=str(stats["errors"]), inline=True)
    embed.add_field(name="Blocks", value=str(stats["blocked"]), inline=True)
    embed.add_field(name="Active Countdown Total", value=str(len(active_invalid_vanities)), inline=True)
    embed.add_field(name="Length Filter", value=f"`{countdown_filter_label()}`", inline=True)
    if stats.get("removed_sample"):
        embed.add_field(name="Removed Sample", value=", ".join(f"`{c}`" for c in stats["removed_sample"]), inline=False)
    try:
        await status_msg.edit(content=None, embed=embed)
    except Exception:
        await ctx.send(embed=embed)


@bot.command(name="setcountdownlengths", aliases=["setcountdownlength", "countdownlengths"])
@commands.has_permissions(administrator=True)
async def setcountdownlengths(ctx, min_length: int, max_length: int = None):
    """Set which vanity lengths are allowed in the countdown tracker and prune bad saved data."""
    if max_length is None:
        max_length = 32
    min_length = max(1, min(32, int(min_length)))
    max_length = max(min_length, min(32, int(max_length)))
    config["min_countdown_length"] = min_length
    config["max_countdown_length"] = max_length
    save_config()
    stats = prune_countdown_tracker_by_length(save=True)
    await ctx.send(
        f"Countdown length filter set to `{countdown_filter_label()}`.\n"
        f"Removed `{stats['removed_active']}` active countdown(s) and `{stats['removed_expired']}` expired countdown(s) outside that range."
    )


@bot.command(name="prunecountdowns", aliases=["cleanbadcountdowns", "cleancountdowns"])
@commands.has_permissions(administrator=True)
async def prunecountdowns(ctx):
    """Remove saved countdown records outside the current length filter."""
    stats = prune_countdown_tracker_by_length(save=True)
    await ctx.send(
        f"Pruned countdown tracker using filter `{stats['filter']}`.\n"
        f"Removed `{stats['removed_active']}` active countdown(s) and `{stats['removed_expired']}` expired countdown(s)."
    )


@bot.command(name="resetcountdowns", aliases=["clearcountdowns", "wipecountdowns", "resetinvalids"])
@commands.has_permissions(administrator=True)
async def resetcountdowns(ctx):
    """Delete all active/expired countdowns and stored backfill transition events.

    This keeps saved vanity lists and channel scan cursors, but removes the stored
    events that would recreate old countdowns on the next backfill replay.
    """
    active_before = len(active_invalid_vanities)
    expired_before = len(expired_invalid_vanities)
    events_before = len(backfill_transition_events)

    active_invalid_vanities.clear()
    expired_invalid_vanities.clear()
    backfill_transition_events.clear()

    save_invalid_tracker()
    save_backfill_progress()
    write_event_log("countdowns_reset_by_command", {
        "user_id": int(ctx.author.id),
        "guild_id": int(ctx.guild.id) if ctx.guild else None,
        "active_removed": active_before,
        "expired_removed": expired_before,
        "backfill_events_removed": events_before,
    })

    embed = discord.Embed(title="Countdown Tracker Reset", color=discord.Color.orange())
    embed.add_field(name="Active Countdowns Removed", value=str(active_before), inline=True)
    embed.add_field(name="Expired Countdowns Removed", value=str(expired_before), inline=True)
    embed.add_field(name="Stored Backfill Events Removed", value=str(events_before), inline=True)
    embed.add_field(
        name="Saved Lists",
        value="Kept. This command does not delete your vanity lists or channel setup.",
        inline=False,
    )
    embed.add_field(
        name="Backfill Cursors",
        value="Kept. Use `!resetbackfill #channel` if you want to rescan old channel history from scratch.",
        inline=False,
    )
    await ctx.send(embed=embed)


@bot.command(name="invalidcount")
async def invalidcount_command(ctx):
    await process_due_countdowns(source="command")
    expiring_today = 0
    expiring_week = 0
    now_dt = datetime.now(timezone.utc)
    for record in active_invalid_vanities.values():
        expires_dt = parse_iso_dt(record.get("expires_at"))
        if not expires_dt:
            continue
        delta = expires_dt - now_dt
        if timedelta(seconds=0) <= delta <= timedelta(days=1):
            expiring_today += 1
        if timedelta(seconds=0) <= delta <= timedelta(days=7):
            expiring_week += 1

    alert_channel_id = config.get("invalid_alert_channel_id")
    embed = discord.Embed(title="Vanity Countdown Counts", color=discord.Color.blurple())
    embed.add_field(name="Active Countdowns", value=str(len(active_invalid_vanities)), inline=True)
    embed.add_field(name="Expired List", value=str(len(expired_invalid_vanities)), inline=True)
    embed.add_field(name="Expiring Today", value=str(expiring_today), inline=True)
    embed.add_field(name="Expiring This Week", value=str(expiring_week), inline=True)
    embed.add_field(name="Alert Channel", value=f"<#{alert_channel_id}>" if alert_channel_id else "Not set", inline=False)
    await ctx.send(embed=embed)


@bot.command(name="invalidexport")
async def invalidexport(ctx):
    export_path = DATA_DIR / "invalid_tracker_export.json"
    export = {
        "exported_at": now_iso(),
        "active_countdowns": active_invalid_vanities,
        "expired_countdowns": expired_invalid_vanities,
    }
    write_json_file(export_path, export)
    await ctx.send(
        content=f"Exported `{len(active_invalid_vanities)}` active countdown(s) and `{len(expired_invalid_vanities)}` expired countdown(s).",
        file=discord.File(str(export_path), filename=export_path.name),
    )


async def rebuild_countdowns_from_messages(
    channels: list[discord.TextChannel],
    limit_per_channel: int,
    *,
    list_label: str = "manual-channel",
) -> dict:
    """Incrementally scan chosen channels and replay stored transition events.

    First run scans up to the latest `limit_per_channel` messages.
    Future runs skip that scanned range, check newer messages, then continue into older
    unscanned history using saved cursors in data/backfill_scan_state.json.
    """
    load_backfill_progress()

    scanned = 0
    new_messages_scanned = 0
    older_messages_scanned = 0
    initial_messages_scanned = 0
    matched_available = 0
    matched_taken = 0
    new_events_saved = 0
    duplicate_events = 0
    failed_channels = []
    channel_notes = []

    for channel in channels:
        cid = str(channel.id)
        state = backfill_scan_state.get(cid, {})
        messages: list[discord.Message] = []
        initial_scan = not state.get("oldest_message_id") or not state.get("newest_message_id")
        older_requested = False

        try:
            if initial_scan:
                batch = [m async for m in channel.history(limit=limit_per_channel, oldest_first=True)]
                messages.extend(batch)
                initial_messages_scanned += len(batch)
                # If Discord returned fewer than the requested limit, this channel likely has no older backlog.
                if len(batch) < limit_per_channel:
                    state["older_history_complete"] = True
            else:
                remaining = limit_per_channel

                # 1) Catch messages posted after the last scan.
                newest_id = int(state.get("newest_message_id"))
                newer_batch = [
                    m async for m in channel.history(
                        limit=remaining,
                        after=discord.Object(id=newest_id),
                        oldest_first=True,
                    )
                ]
                messages.extend(newer_batch)
                new_messages_scanned += len(newer_batch)
                remaining -= len(newer_batch)

                # 2) Use any remaining budget to continue farther back in old history.
                if remaining > 0 and not state.get("older_history_complete", False):
                    older_requested = True
                    oldest_id = int(state.get("oldest_message_id"))
                    older_batch = [
                        m async for m in channel.history(
                            limit=remaining,
                            before=discord.Object(id=oldest_id),
                            oldest_first=False,
                        )
                    ]
                    messages.extend(older_batch)
                    older_messages_scanned += len(older_batch)
                    if len(older_batch) == 0 or len(older_batch) < remaining:
                        state["older_history_complete"] = True

            scanned += len(messages)
            update_channel_backfill_state(channel, messages, initial_scan=initial_scan, older_requested=older_requested)
            # Preserve older_history_complete updates made above.
            if state.get("older_history_complete"):
                backfill_scan_state[cid]["older_history_complete"] = True

            if not messages:
                if state.get("older_history_complete"):
                    channel_notes.append(f"#{getattr(channel, 'name', channel.id)}: no new messages; older history already complete")
                else:
                    channel_notes.append(f"#{getattr(channel, 'name', channel.id)}: no new messages found this run")
                continue

            channel_label = f"{list_label}:{channel.id}"
            for message in messages:
                events = extract_transition_events_from_message(message, channel_label)
                for event in events:
                    if event["event_type"] == "available":
                        matched_available += 1
                    elif event["event_type"] == "taken":
                        matched_taken += 1

                    key = backfill_event_key(event["channel_id"], event["message_id"], event["event_type"], event["code"])
                    if key in backfill_transition_events:
                        duplicate_events += 1
                    else:
                        backfill_transition_events[key] = event
                        new_events_saved += 1

        except discord.Forbidden:
            failed_channels.append(f"#{getattr(channel, 'name', channel.id)} (missing Read Message History permission)")
        except Exception as e:
            failed_channels.append(f"#{getattr(channel, 'name', channel.id)} ({short_text(e, 80)})")

    save_backfill_progress()
    replay_stats = replay_stored_backfill_events_to_tracker()

    return {
        "scanned": scanned,
        "initial_messages_scanned": initial_messages_scanned,
        "new_messages_scanned": new_messages_scanned,
        "older_messages_scanned": older_messages_scanned,
        "matched_available": matched_available,
        "matched_taken": matched_taken,
        "new_events_saved": new_events_saved,
        "duplicate_events": duplicate_events,
        "stored_events_total": replay_stats.get("stored_events_total", len(backfill_transition_events)),
        "added_active": replay_stats.get("added_active", 0),
        "updated_active": replay_stats.get("updated_active", 0),
        "moved_expired": replay_stats.get("moved_expired", 0),
        "replaced_expired": replay_stats.get("replaced_expired", 0),
        "removed_by_taken": replay_stats.get("removed_by_taken", 0),
        "skipped_latest_taken": replay_stats.get("skipped_latest_taken", replay_stats.get("removed_by_taken", 0)),
        "skipped_no_prior_taken": replay_stats.get("skipped_no_prior_taken", 0),
        "ignored_repeat_available": replay_stats.get("ignored_repeat_available", 0),
        "ignored_repeat_taken": replay_stats.get("ignored_repeat_taken", 0),
        "replaced_newer_existing_with_earlier_streak": replay_stats.get("replaced_newer_existing_with_earlier_streak", 0),
        "kept_newer_existing": replay_stats.get("kept_newer_existing", 0),
        "active_total": replay_stats.get("active_total", len(active_invalid_vanities)),
        "expired_total": replay_stats.get("expired_total", len(expired_invalid_vanities)),
        "failed_channels": failed_channels,
        "channel_notes": channel_notes,
    }


def build_backfill_result_embed(title: str, stats: dict) -> discord.Embed:
    embed = discord.Embed(title=title, color=discord.Color.green())
    embed.add_field(name="Messages Scanned This Run", value=str(stats.get("scanned", 0)), inline=True)
    embed.add_field(name="Initial / New / Older", value=f"`{stats.get('initial_messages_scanned', 0)}` / `{stats.get('new_messages_scanned', 0)}` / `{stats.get('older_messages_scanned', 0)}`", inline=True)
    embed.add_field(name="New Events Saved", value=str(stats.get("new_events_saved", 0)), inline=True)
    embed.add_field(name="Not-Taken Transitions Found", value=str(stats.get("matched_available", 0)), inline=True)
    embed.add_field(name="Taken Transitions Found", value=str(stats.get("matched_taken", 0)), inline=True)
    embed.add_field(name="Duplicate Events Skipped", value=str(stats.get("duplicate_events", 0)), inline=True)
    embed.add_field(name="Stored Events Total", value=str(stats.get("stored_events_total", 0)), inline=True)
    embed.add_field(name="Active Added", value=str(stats.get("added_active", 0)), inline=True)
    embed.add_field(name="Active Updated", value=str(stats.get("updated_active", 0)), inline=True)
    embed.add_field(name="Moved To Expired", value=str(stats.get("moved_expired", 0)), inline=True)
    embed.add_field(name="Latest Taken / Skipped", value=str(stats.get("skipped_latest_taken", stats.get("removed_by_taken", 0))), inline=True)
    embed.add_field(name="Repeat Available Logs Ignored", value=str(stats.get("ignored_repeat_available", 0)), inline=True)
    embed.add_field(name="Repeat Taken Logs Ignored", value=str(stats.get("ignored_repeat_taken", 0)), inline=True)
    embed.add_field(name="Available Without Prior Taken Skipped", value=str(stats.get("skipped_no_prior_taken", 0)), inline=True)
    embed.add_field(name="Newer Fake Timer Replaced", value=str(stats.get("replaced_newer_existing_with_earlier_streak", 0)), inline=True)
    embed.add_field(name="Active Total", value=str(stats.get("active_total", 0)), inline=True)
    embed.add_field(name="Expired Total", value=str(stats.get("expired_total", 0)), inline=True)
    failed = stats.get("failed_channels") or []
    if failed:
        embed.add_field(name="Skipped Channels", value="\n".join(failed[:8]), inline=False)
    notes = stats.get("channel_notes") or []
    if notes:
        embed.add_field(name="Channel Notes", value="\n".join(notes[:8]), inline=False)
    embed.set_footer(text="Backfill uses the first not-taken log after the most recent taken log. Repeated not-taken/taken rerun logs are collapsed and ignored.")
    return embed


def build_backfill_timeline_embed(code: str, limit: int = 30) -> discord.Embed:
    code = clean_code(code)
    events_by_code = get_events_by_code_from_backfill()
    events = events_by_code.get(code, [])

    embed = discord.Embed(
        title=f"Backfill Status Runs: discord.gg/{code}",
        color=discord.Color.blurple(),
    )

    if not events:
        embed.description = "No stored backfill transition events were found for this vanity. Run `!backfillchannel #log 10000` first."
        return embed

    runs = compress_status_runs(events)
    shown_runs = runs[-max(1, min(int(limit), 50)):]

    latest = runs[-1]
    latest_status = latest.get("status", "unknown")
    embed.add_field(name="Stored Raw Events", value=str(len(events)), inline=True)
    embed.add_field(name="Compressed Runs", value=str(len(runs)), inline=True)
    embed.add_field(name="Newest Status", value=f"`{latest_status}`", inline=True)

    active = active_invalid_vanities.get(code)
    expired = expired_invalid_vanities.get(code)
    if active:
        embed.add_field(name="Countdown Start", value=discord_relative(active.get("taken_at") or active.get("invalid_at")), inline=False)
        embed.add_field(name="Countdown Ends", value=discord_relative(active.get("expires_at")), inline=False)
    elif expired:
        embed.add_field(name="Expired Countdown Start", value=discord_relative(expired.get("taken_at") or expired.get("invalid_at")), inline=False)
        embed.add_field(name="Expired At", value=discord_relative(expired.get("expired_at") or expired.get("expires_at")), inline=False)
    else:
        if latest_status == "taken":
            reason = "No countdown is kept because the newest stored status is taken/on-server."
        else:
            reason = "No countdown is kept because the available run does not have a prior taken/on-server event in scanned history."
        embed.add_field(name="Countdown Status", value=reason, inline=False)

    lines = []
    for idx, run in enumerate(shown_runs, start=max(1, len(runs) - len(shown_runs) + 1)):
        status_label = "not taken" if run["status"] == "available" else "taken"
        count_suffix = f" x{run['count']}" if run.get("count", 1) > 1 else ""
        first_at = parse_iso_dt(run.get("first_at"))
        last_at = parse_iso_dt(run.get("last_at"))
        if first_at and last_at and run.get("count", 1) > 1:
            time_text = f"<t:{int(first_at.timestamp())}:g> → <t:{int(last_at.timestamp())}:g>"
        elif first_at:
            time_text = f"<t:{int(first_at.timestamp())}:g>"
        else:
            time_text = "Unknown time"
        lines.append(f"`{idx}.` **{status_label}{count_suffix}** — {time_text}")

    for chunk_index, chunk in enumerate(split_embed_lines(lines, max_chars=1000), start=1):
        embed.add_field(name="Compressed Status Runs" if chunk_index == 1 else f"Compressed Status Runs {chunk_index}", value=chunk, inline=False)

    embed.set_footer(text="Repeated same-status logs are compressed, like taken x10 or not taken x4. The countdown starts at the first not-taken run after the latest taken run.")
    return embed


@bot.command(name="backfilltimeline", aliases=["statusruns", "vanitytimeline", "backfillruns"])
async def backfilltimeline(ctx, vanity: str, limit: int = 30):
    """Show compressed backfill status runs for one vanity without flooding duplicates."""
    code = clean_code(vanity)
    if not code:
        await ctx.send("Give me a valid vanity, like `mean` or `discord.gg/mean`.")
        return
    load_backfill_progress()
    await ctx.send(embed=build_backfill_timeline_embed(code, limit=limit))


@bot.command(name="backfillchannel", aliases=["backfillinvalidchannel", "backfillfromchannel"])
@commands.has_permissions(administrator=True)
async def backfillchannel(ctx, channel: discord.TextChannel, limit_per_channel: int = 5000):
    """Backfill countdowns from any chosen channel."""
    if limit_per_channel < 1:
        await ctx.send("Use a message limit of at least `1`.")
        return
    if limit_per_channel > 50000:
        await ctx.send("Use `50000` or less so Discord does not throttle the bot too hard.")
        return

    status_msg = await ctx.send(
        f"Backfilling countdowns from {channel.mention}... scanning up to `{limit_per_channel}` messages."
    )
    stats = await rebuild_countdowns_from_messages([channel], limit_per_channel, list_label=f"manual:{channel.name}")
    await status_msg.edit(content=None, embed=build_backfill_result_embed("Manual Channel Backfill Complete", stats))



@bot.command(name="backfillstatus", aliases=["backfillprogress"])
async def backfillstatus(ctx, channel: Optional[discord.TextChannel] = None):
    """Show saved incremental backfill scan progress."""
    load_backfill_progress()
    embed = discord.Embed(title="Backfill Scan Progress", color=discord.Color.blurple())
    embed.add_field(name="Stored Transition Events", value=str(len(backfill_transition_events)), inline=True)

    states = backfill_scan_state
    if channel:
        states = {str(channel.id): backfill_scan_state.get(str(channel.id), {})}

    if not states or (channel and not states.get(str(channel.id))):
        embed.add_field(
            name="No Progress Saved",
            value="No incremental cursor is saved for that channel yet. Run `!backfillchannel #channel 5000` first.",
            inline=False,
        )
        await ctx.send(embed=embed)
        return

    for cid, state in list(states.items())[:10]:
        if not state:
            continue
        older_done = "Yes" if state.get("older_history_complete") else "No"
        oldest = discord_relative(state.get("oldest_message_at")) if state.get("oldest_message_at") else "Unknown"
        newest = discord_relative(state.get("newest_message_at")) if state.get("newest_message_at") else "Unknown"
        embed.add_field(
            name=f"#{state.get('channel_name', cid)}",
            value=(
                f"Channel: <#{cid}>\n"
                f"Scan runs: `{state.get('scan_runs', 0)}`\n"
                f"Oldest scanned: {oldest}\n"
                f"Newest scanned: {newest}\n"
                f"Older history complete: `{older_done}`\n"
                f"Last scan: {discord_relative(state.get('last_scan_at')) if state.get('last_scan_at') else 'Unknown'}"
            ),
            inline=False,
        )

    embed.set_footer(text="Backfill skips the saved scanned range, checks newer messages, then continues older history.")
    await ctx.send(embed=embed)


@bot.command(name="resetbackfill", aliases=["resetbackfillchannel"])
@commands.has_permissions(administrator=True)
async def resetbackfill(ctx, channel: discord.TextChannel):
    """Reset a channel scan cursor so the next backfill can rescan from the latest messages."""
    load_backfill_progress()
    had_state = reset_backfill_channel_state(channel.id)
    await ctx.send(
        f"Backfill cursor for {channel.mention} has been reset. "
        f"Stored transition events were kept to prevent duplicate countdowns. "
        f"Previous cursor existed: `{'Yes' if had_state else 'No'}`"
    )


@bot.command(name="backfillinvalid")
@commands.has_permissions(administrator=True)
async def backfillinvalid(ctx, limit_per_log_channel: int = 5000):
    """Rebuild countdowns from old transition messages in configured log channels."""
    if limit_per_log_channel < 1:
        await ctx.send("Use a message limit of at least `1`.")
        return
    if limit_per_log_channel > 50000:
        await ctx.send("Use `50000` or less per log channel so Discord does not throttle the bot too hard.")
        return

    status_msg = await ctx.send(f"Backfilling countdowns from saved log channels... scanning up to `{limit_per_log_channel}` messages per log channel.")

    log_channels = []
    seen_channel_ids = set()
    for list_name, raw_data in config.get("lists", {}).items():
        data = normalize_list_record(raw_data)
        cid = data.get("log_channel_id")
        if not cid:
            continue
        try:
            cid_int = int(cid)
        except Exception:
            continue
        if cid_int in seen_channel_ids:
            continue
        channel = await get_channel(cid_int)
        if channel:
            log_channels.append(channel)
            seen_channel_ids.add(cid_int)

    if not log_channels:
        await status_msg.edit(content="No log channels found in saved lists. Use `!backfillchannel #log 5000` to scan a channel manually, or set list channels first.")
        return

    stats = await rebuild_countdowns_from_messages(log_channels, limit_per_log_channel, list_label="saved-log")
    await status_msg.edit(content=None, embed=build_backfill_result_embed("Saved Log Backfill Complete", stats))


@bot.command(name="unavailablecount")
async def unavailablecount(ctx, length: int):
    if length < 1 or length > 32:
        await ctx.send("Use a length between 1 and 32.")
        return
    load_unavailable_cache()
    await ctx.send(f"{length}-letter not-taken/available TXT count: `{len(unavailable_cache[length])}`")


@bot.command(name="getunavailable", aliases=["getinvalid"])
async def getunavailable(ctx, length: int):
    if length < 1 or length > 32:
        await ctx.send("Use a length between 1 and 32.")
        return
    load_unavailable_cache()
    rewrite_unavailable_file(length)
    path = unavailable_file(length)
    await ctx.send(content=f"Not-taken/available TXT file for `{length}` letters:", file=discord.File(str(path), filename=path.name))


@bot.command(name="clearunavailable", aliases=["clearinvalid"])
@commands.has_permissions(administrator=True)
async def clearunavailable(ctx, length: int = None):
    load_unavailable_cache()
    if length is None:
        for l in TRACKED_LENGTHS:
            unavailable_cache[l].clear()
            rewrite_unavailable_file(l)
        await ctx.send("Cleared all not-taken/available TXT files.")
        return
    if length < 1 or length > 32:
        await ctx.send("Use a length between 1 and 32.")
        return
    unavailable_cache[length].clear()
    rewrite_unavailable_file(length)
    await ctx.send(f"Cleared not-taken/available TXT file for `{length}` letters.")


def file_size_label(path: Path) -> str:
    try:
        size = path.stat().st_size
    except Exception:
        return "missing"
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


@bot.command(name="datastatus", aliases=["savestatus", "persistence"])
async def datastatus(ctx):
    load_config()
    load_invalid_tracker()
    load_backfill_progress()

    uses_volume = str(DATA_DIR).startswith("/data")
    embed = discord.Embed(title="Vanity Bot Data Status", color=discord.Color.blurple())
    embed.add_field(name="Data Folder", value=f"`{DATA_DIR}`", inline=False)
    embed.add_field(name="Railway Volume Path", value="`Yes`" if uses_volume else "`No / local fallback`", inline=True)
    embed.add_field(name="Saved Lists", value=f"`{len(config.get('lists', {}))}`", inline=True)
    embed.add_field(name="Active Countdowns", value=f"`{len(active_invalid_vanities)}`", inline=True)
    embed.add_field(name="Expired Countdowns", value=f"`{len(expired_invalid_vanities)}`", inline=True)
    embed.add_field(name="Backfill Channels", value=f"`{len(backfill_scan_state)}`", inline=True)
    embed.add_field(name="Backfill Events", value=f"`{len(backfill_transition_events)}`", inline=True)
    embed.add_field(name="Saved Vanity Statuses", value=f"`{len(vanity_statuses)}`", inline=True)

    files = [
        CONFIG_FILE, ACTIVE_INVALID_FILE, EXPIRED_INVALID_FILE,
        BACKFILL_STATE_FILE, BACKFILL_EVENTS_FILE, EVENT_LOG_FILE,
    ]
    file_lines = [f"`{path.name}` — {file_size_label(path)}" for path in files]
    embed.add_field(name="Saved Files", value="\n".join(file_lines), inline=False)
    if not uses_volume:
        embed.add_field(
            name="Important",
            value="For Railway redeploy/update persistence, attach a Railway Volume and set `DATA_DIR=/data`. Normal restarts still save to the folder above.",
            inline=False,
        )
    embed.set_footer(text="Commands and transitions save immediately; an autosave also runs every 2 minutes.")
    await ctx.send(embed=embed)


@bot.command(name="savedata", aliases=["forcesave"])
@commands.has_permissions(administrator=True)
async def savedata(ctx):
    save_all_data(reason=f"manual_command_by_{ctx.author.id}")
    await ctx.send(
        "Saved all data now:\n"
        f"Lists: `{len(config.get('lists', {}))}`\n"
        f"Active countdowns: `{len(active_invalid_vanities)}`\n"
        f"Expired countdowns: `{len(expired_invalid_vanities)}`\n"
        f"Data folder: `{DATA_DIR}`"
    )


@bot.command(name="exportdata", aliases=["backupdata"])
@commands.has_permissions(administrator=True)
async def exportdata(ctx):
    save_all_data(reason=f"export_command_by_{ctx.author.id}")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    export_path = DATA_DIR / f"vanity_bot_data_export_{timestamp}.zip"

    with zipfile.ZipFile(export_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in DATA_DIR.rglob("*"):
            if path.is_file() and path != export_path:
                try:
                    zf.write(path, arcname=str(path.relative_to(DATA_DIR)))
                except Exception:
                    pass

    await ctx.send(
        content=(
            "Here is a full data backup. Keep this before major Railway updates/redeploys.\n"
            f"Lists: `{len(config.get('lists', {}))}` | Active: `{len(active_invalid_vanities)}` | Expired: `{len(expired_invalid_vanities)}`"
        ),
        file=discord.File(str(export_path), filename=export_path.name),
    )


@bot.command()
@commands.has_permissions(manage_messages=True)
async def purge(ctx, amount: int):
    if amount < 1 or amount > 100:
        await ctx.send("Use an amount between `1` and `100`.")
        return
    deleted = await ctx.channel.purge(limit=amount + 1)
    msg = await ctx.send(f"Deleted `{len(deleted) - 1}` messages.")
    await asyncio.sleep(3)
    try:
        await msg.delete()
    except Exception:
        pass

# =========================
# ERROR HANDLING / STARTUP
# =========================

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You do not have permission to use that command.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing argument: `{error.param.name}`. Run `{config.get('prefix', DEFAULT_PREFIX)}help`.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send(f"Bad argument. Run `{config.get('prefix', DEFAULT_PREFIX)}help` and check the command format.")
    elif isinstance(error, commands.CommandNotFound):
        return
    else:
        logger.exception("Command error: %s", error)
        await ctx.send(f"Command error: `{short_text(error, 500)}`")


@bot.event
async def on_disconnect():
    # Discord disconnects can happen during restarts/redeploys. Save current in-memory data.
    save_all_data(reason="discord_disconnect")


@bot.event
async def on_resumed():
    write_event_log("discord_resumed", {"data_dir": str(DATA_DIR)})


@bot.event
async def on_ready():
    global _slash_commands_synced
    ensure_dirs()
    ensure_unavailable_files()
    load_config()
    load_unavailable_cache()
    load_invalid_tracker()
    load_backfill_progress()
    load_vanity_statuses()
    seed_statuses_from_not_taken_files()

    if not auto_check_loop.is_running():
        auto_check_loop.start()
    if not invalid_countdown_loop.is_running():
        invalid_countdown_loop.start()
    if not autosave_loop.is_running():
        autosave_loop.start()

    await process_due_countdowns(source="startup")
    if not _slash_commands_synced:
        try:
            await bot.tree.sync()
            _slash_commands_synced = True
            write_event_log("slash_commands_synced", {"commands": len(bot.tree.get_commands())})
        except Exception as e:
            logger.warning("Slash command sync failed: %s", e)
            write_event_log("slash_command_sync_failed", {"error": short_text(e, 200)})
    save_all_data(reason="startup_sync")

    logger.info(
        "Logged in as %s | Prefix: %s | Data dir: %s | Lists: %s | Active countdowns: %s | Expired countdowns: %s",
        bot.user,
        config.get("prefix", DEFAULT_PREFIX),
        DATA_DIR,
        len(config.get("lists", {})),
        len(active_invalid_vanities),
        len(expired_invalid_vanities),
    )

def handle_shutdown_signal(signum=None, frame=None):
    save_all_data(reason=f"shutdown_signal_{signum}")


atexit.register(lambda: save_all_data(reason="atexit"))
try:
    signal.signal(signal.SIGTERM, handle_shutdown_signal)
    signal.signal(signal.SIGINT, handle_shutdown_signal)
except Exception:
    pass


if __name__ == "__main__":
    ensure_dirs()
    load_config()
    load_unavailable_cache()
    load_invalid_tracker()
    load_backfill_progress()
    load_vanity_statuses()
    seed_statuses_from_not_taken_files()
    write_event_log("bot_process_starting", {"data_dir": str(DATA_DIR), "lists": len(config.get("lists", {}))})
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing from environment variables. Add it in Railway Variables.")
    bot.run(TOKEN)

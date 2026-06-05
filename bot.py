import os
import re
import json
import asyncio
import logging
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple, Any

import aiohttp
import discord
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
DATA_DIR = BASE_DIR / "data"
# Kept as unavailable_vanities for compatibility with your existing hosted files.
# The contents are now NOT-TAKEN / AVAILABLE words only.
UNAVAILABLE_DIR = DATA_DIR / "unavailable_vanities"
CONFIG_FILE = DATA_DIR / "vanity_config.json"
ACTIVE_INVALID_FILE = DATA_DIR / "invalid_vanities.json"
EXPIRED_INVALID_FILE = DATA_DIR / "expired_invalid_vanities.json"
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
    "lists": {}
}


def get_prefix(bot_obj, message):
    return config.get("prefix", DEFAULT_PREFIX)


bot = commands.Bot(command_prefix=get_prefix, intents=intents, help_command=None)

# Stores not-taken / available words, even though older command/file names say unavailable.
unavailable_cache = defaultdict(set)

# Active countdowns start when a vanity goes from available/not-taken (404) to taken/on-server (200).
# Expired countdowns are moved out of the active tracker after 30 days.
active_invalid_vanities: dict[str, dict] = {}
expired_invalid_vanities: dict[str, dict] = {}

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
    ensure_dirs()
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4)


def load_config() -> None:
    ensure_dirs()
    if not CONFIG_FILE.exists():
        save_config()
        return

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
    except Exception as e:
        logger.warning("Config failed to load, using defaults: %s", e)
        save_config()
        return

    config["prefix"] = str(loaded.get("prefix", DEFAULT_PREFIX))[:5] or DEFAULT_PREFIX
    config["auto_enabled"] = bool(loaded.get("auto_enabled", False))
    config["auto_minutes"] = int(loaded.get("auto_minutes", DEFAULT_AUTO_MINUTES))
    config["delay_seconds"] = int(loaded.get("delay_seconds", DEFAULT_DELAY_SECONDS))
    config["batch_size"] = int(loaded.get("batch_size", DEFAULT_BATCH_SIZE))
    config["batch_cooldown_seconds"] = int(loaded.get("batch_cooldown_seconds", DEFAULT_BATCH_COOLDOWN_SECONDS))
    config["list_cooldown_seconds"] = int(loaded.get("list_cooldown_seconds", DEFAULT_LIST_COOLDOWN_SECONDS))
    config["invalid_alert_channel_id"] = loaded.get("invalid_alert_channel_id")

    loaded_lists = loaded.get("lists", {}) if isinstance(loaded.get("lists", {}), dict) else {}
    config["lists"] = {clean_code(name): normalize_list_record(data) for name, data in loaded_lists.items() if clean_code(name)}


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
    with open(path, "w", encoding="utf-8") as f:
        for code in sorted(unavailable_cache[length]):
            f.write(code + "\n")


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


def read_json_file(path: Path, default):
    ensure_dirs()
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except Exception as e:
        logger.warning("Failed to read %s: %s", path.name, e)
        return default


def write_json_file(path: Path, data) -> None:
    ensure_dirs()
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, sort_keys=True)
    tmp_path.replace(path)


def normalize_tracker_record(code: str, record: dict, *, expired: bool = False) -> Optional[dict]:
    code = clean_code(code or record.get("code", ""))
    if not code:
        return None

    taken_at_dt = parse_iso_dt(record.get("taken_at") or record.get("invalid_at"))
    if not taken_at_dt:
        return None

    expires_at_dt = parse_iso_dt(record.get("expires_at")) or (taken_at_dt + timedelta(days=COUNTDOWN_DAYS))
    output = dict(record)
    output["code"] = code
    output["taken_at"] = taken_at_dt.isoformat()
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
    write_json_file(ACTIVE_INVALID_FILE, active_invalid_vanities)
    write_json_file(EXPIRED_INVALID_FILE, expired_invalid_vanities)


def make_tracker_record(code: str, list_name: str, taken_at: Optional[str] = None, source: str = "checker") -> dict:
    code = clean_code(code)
    taken_dt = parse_iso_dt(taken_at) or datetime.now(timezone.utc)
    expires_dt = taken_dt + timedelta(days=COUNTDOWN_DAYS)
    timestamp = now_iso()
    return {
        "code": code,
        "taken_at": taken_dt.isoformat(),
        "expires_at": expires_dt.isoformat(),
        "length": len(code),
        "list": list_name or "unknown",
        "source": source,
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def add_invalid_vanity(code: str, list_name: str, taken_at: Optional[str] = None, source: str = "checker") -> dict:
    """Start a 30-day countdown after available/not-taken -> taken/on-server."""
    code = clean_code(code)
    if not code:
        return {}

    # A fresh taken transition should remove any old expired record for the same code.
    expired_invalid_vanities.pop(code, None)

    if code in active_invalid_vanities:
        return active_invalid_vanities[code]

    record = make_tracker_record(code, list_name, taken_at=taken_at, source=source)
    active_invalid_vanities[code] = record
    save_invalid_tracker()
    return record


def remove_invalid_vanity(code: str, reason: str = "became_available", seen_at: Optional[str] = None, save: bool = True) -> tuple[Optional[dict], Optional[dict]]:
    """Remove countdown records when the vanity becomes available/not-taken again."""
    code = clean_code(code)
    if not code:
        return None, None

    active_record = active_invalid_vanities.pop(code, None)
    expired_record = expired_invalid_vanities.pop(code, None)

    if save and (active_record or expired_record):
        save_invalid_tracker()
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
    embed.add_field(name="Started When", value="Available / not taken → Taken / on server", inline=False)
    embed.add_field(name="Taken Since", value=discord_relative(record.get("taken_at")), inline=False)
    embed.add_field(name="30-Day Timer Ends", value=discord_relative(record.get("expires_at")), inline=False)
    if expired:
        embed.add_field(name="Timer Expired", value=discord_relative(record.get("expired_at") or record.get("expires_at")), inline=False)
    else:
        remaining = seconds_until(record.get("expires_at"))
        embed.add_field(name="Live Countdown", value=f"`{format_duration(remaining)}` remaining • <t:{int(parse_iso_dt(record.get('expires_at')).timestamp())}:R>", inline=False)
    embed.add_field(name="List", value=f"`{record.get('list', 'unknown')}`", inline=True)
    embed.add_field(name="Length", value=f"`{record.get('length', len(code))}`", inline=True)
    embed.set_footer(text="Countdowns are based on when this bot detected available/not-taken → taken/on-server.")
    return embed


def build_active_list_embed(limit: int = 10, *, recent: bool = True) -> discord.Embed:
    limit = max(1, min(int(limit), 25))
    records = active_sorted_recent() if recent else active_sorted_expiring()
    shown = records[:limit]
    embed = discord.Embed(
        title="Active Vanity Countdowns",
        description="Tracking vanities that changed from not taken/available to taken/on-server.",
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
    embed.add_field(name="Started", value=discord_relative(record.get("taken_at")), inline=False)
    embed.add_field(name="Timer Expired", value=discord_relative(record.get("expired_at") or record.get("expires_at")), inline=False)
    embed.add_field(name="Moved To", value="Expired countdown list", inline=False)
    embed.set_footer(text="30 days elapsed since the bot detected not-taken/available → taken/on-server.")
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
    return moved

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
                    # 404 / Unknown Invite = NOT on a server. This is the only result that belongs in the TXT file.
                    available_count += 1
                    available_found.append(code)

                    if add_unavailable(code):
                        added_count += 1
                        await safe_send(log_channel, f"`discord.gg/{code}` is not taken/available and was added to the not-taken TXT file.")

                    # If it becomes available again, remove it from active/expired countdown tracking.
                    removed_active, removed_expired = remove_invalid_vanity(code, reason="became_available", seen_at=now_iso(), save=True)
                    if removed_active or removed_expired:
                        await safe_send(
                            log_channel,
                            f"`discord.gg/{code}` became not taken/available again and was removed from the countdown tracker."
                        )

                    await safe_send(available_channel, f"discord.gg/{code}")

                elif result == "taken":
                    # 200 / Invite exists = currently on a server. Make sure it is NOT inside the TXT file.
                    taken_count += 1
                    taken_found.append(code)

                    if remove_unavailable(code):
                        # This is the exact transition you requested: not taken/available -> taken/on server.
                        removed_count += 1
                        transition_time = now_iso()
                        record = add_invalid_vanity(code, list_name, taken_at=transition_time, source="checker")
                        await safe_send(log_channel, f"`discord.gg/{code}` is taken/on a server and was removed from the not-taken TXT file.")
                        await safe_send(
                            log_channel,
                            f"Started 30-day countdown for `discord.gg/{code}`. Ends {discord_relative(record.get('expires_at'))}."
                        )

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

# =========================
# COMMANDS
# =========================

@bot.command(name="help")
async def help_command(ctx):
    p = config.get("prefix", DEFAULT_PREFIX)
    embed = discord.Embed(
        title="Vanity Checker Help",
        description=(
            "Checks invite codes safely using Discord's API. "
            "Not taken/available vanities go to one channel, and taken/on-server vanities go to another."
        ),
        color=discord.Color.blurple()
    )
    embed.add_field(
        name="Setup",
        value=(
            f"`{p}addlist <name> <available_channel> <taken_channel> <log_channel> <summary_channel> <ping_role|none> <words>`\n"
            f"Example:\n`{p}addlist 4letters #available #taken #vanity-logs #summaries @Hunters love, hate, void, glow`"
        ),
        inline=False
    )
    embed.add_field(
        name="Manage Lists",
        value=(
            f"`{p}lists`\n`{p}listinfo <name>`\n`{p}addwords <name> <words>`\n"
            f"`{p}removewords <name> <words>`\n`{p}setchannels <name> <available> <taken> <log> <summary>`\n"
            f"`{p}setpingrole <name> <role|none>`\n`{p}removelist <name>`\n`{p}clearlists`"
        ),
        inline=False
    )
    embed.add_field(name="Checks", value=f"`{p}checklist <name>`\n`{p}checkall`\n`{p}stop`", inline=False)
    embed.add_field(name="Auto Checks", value=f"`{p}autocheck <minutes>`\n`{p}autostop`\n`{p}autostatus`", inline=False)
    embed.add_field(
        name="30-Day Taken Countdown Tracker",
        value=(
            f"`{p}setalertchannel #channel` - choose where @everyone completion alerts go\n"
            f"`{p}invalid` - show active countdowns\n"
            f"`{p}invalid <vanity>` or `{p}countdown <vanity>` - check one countdown\n"
            f"`{p}invalidrecent [limit]` - recent active countdowns\n"
            f"`{p}invalidexpired [limit]` - expired countdown list\n"
            f"`{p}invalidcount` - active/expired totals\n"
            f"`{p}invalidexport` - export tracker JSON\n"
            f"`{p}backfillinvalid [messages_per_log_channel]` - rebuild old countdowns from log messages"
        ),
        inline=False
    )
    embed.add_field(name="Settings", value=f"`{p}setprefix <prefix>`\n`{p}ratelimit <delay_seconds> <batch_size> <batch_cooldown_seconds> <list_cooldown_seconds>`", inline=False)
    embed.add_field(name="Available TXT Files", value=f"`{p}unavailablecount <length>`\n`{p}getunavailable <length>`\n`{p}clearunavailable [length]`", inline=False)
    await ctx.send(embed=embed)


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


@bot.command(name="invalidexpired", aliases=["expiredinvalid"])
async def invalidexpired(ctx, limit: int = 10):
    await ctx.send(embed=build_expired_list_embed(limit=limit))


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

    load_unavailable_cache()
    status_msg = await ctx.send(f"Backfilling countdowns from log channels... scanning up to `{limit_per_log_channel}` messages per log channel.")

    log_channel_ids = set()
    channel_to_lists = defaultdict(list)
    for list_name, raw_data in config.get("lists", {}).items():
        data = normalize_list_record(raw_data)
        cid = data.get("log_channel_id")
        if cid:
            try:
                cid_int = int(cid)
                log_channel_ids.add(cid_int)
                channel_to_lists[cid_int].append(list_name)
            except Exception:
                pass

    if not log_channel_ids:
        await status_msg.edit(content="No log channels found in saved lists. Set list channels first, then run backfill again.")
        return

    timeline = []
    scanned = 0
    matched_taken = 0
    matched_available = 0
    failed_channels = []

    for channel_id in log_channel_ids:
        channel = await get_channel(channel_id)
        if not channel:
            failed_channels.append(str(channel_id))
            continue
        try:
            async for message in channel.history(limit=limit_per_log_channel, oldest_first=True):
                scanned += 1
                content = message.content or ""
                list_label = ",".join(channel_to_lists.get(channel_id, [])) or f"log:{channel_id}"

                for match in AVAILABLE_TRANSITION_RE.finditer(content):
                    code = clean_code(match.group(1))
                    if code:
                        matched_available += 1
                        timeline.append((message.created_at.astimezone(timezone.utc), "available", code, list_label))

                for match in TAKEN_TRANSITION_RE.finditer(content):
                    code = clean_code(match.group(1))
                    if code:
                        matched_taken += 1
                        timeline.append((message.created_at.astimezone(timezone.utc), "taken", code, list_label))
        except discord.Forbidden:
            failed_channels.append(f"{channel_id} (missing Read Message History permission)")
        except Exception as e:
            failed_channels.append(f"{channel_id} ({short_text(e, 80)})")

    timeline.sort(key=lambda item: item[0])
    reconstructed = {}
    for event_dt, event_type, code, list_label in timeline:
        if event_type == "available":
            reconstructed.pop(code, None)
        elif event_type == "taken":
            reconstructed[code] = make_tracker_record(code, list_label, taken_at=event_dt.isoformat(), source="backfill")

    added_active = 0
    updated_active = 0
    moved_expired = 0
    skipped_currently_available = 0
    now_dt = datetime.now(timezone.utc)

    for code, record in reconstructed.items():
        length = len(code)
        if length in TRACKED_LENGTHS and code in unavailable_cache[length]:
            # The current TXT files say this is available/not-taken, so do not resurrect an old countdown.
            active_invalid_vanities.pop(code, None)
            expired_invalid_vanities.pop(code, None)
            skipped_currently_available += 1
            continue

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
            expired_invalid_vanities[code] = expired_record
            moved_expired += 1
            continue

        expired_invalid_vanities.pop(code, None)
        existing = active_invalid_vanities.get(code)
        if existing:
            existing_dt = parse_iso_dt(existing.get("taken_at"))
            record_dt = parse_iso_dt(record.get("taken_at"))
            if record_dt and (not existing_dt or record_dt < existing_dt):
                active_invalid_vanities[code] = record
                updated_active += 1
        else:
            active_invalid_vanities[code] = record
            added_active += 1

    save_invalid_tracker()

    embed = discord.Embed(title="Backfill Complete", color=discord.Color.green())
    embed.add_field(name="Messages Scanned", value=str(scanned), inline=True)
    embed.add_field(name="Taken Transitions Found", value=str(matched_taken), inline=True)
    embed.add_field(name="Available Transitions Found", value=str(matched_available), inline=True)
    embed.add_field(name="Active Added", value=str(added_active), inline=True)
    embed.add_field(name="Active Updated Earlier", value=str(updated_active), inline=True)
    embed.add_field(name="Moved To Expired", value=str(moved_expired), inline=True)
    embed.add_field(name="Skipped Currently Available", value=str(skipped_currently_available), inline=True)
    embed.add_field(name="Active Total", value=str(len(active_invalid_vanities)), inline=True)
    embed.add_field(name="Expired Total", value=str(len(expired_invalid_vanities)), inline=True)
    if failed_channels:
        embed.add_field(name="Skipped Channels", value="\n".join(failed_channels[:8]), inline=False)
    embed.set_footer(text="Backfilled expired timers are saved, but not @everyone pinged to avoid old-alert spam.")
    await status_msg.edit(content=None, embed=embed)


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
async def on_ready():
    ensure_dirs()
    ensure_unavailable_files()
    load_config()
    load_unavailable_cache()
    load_invalid_tracker()

    if not auto_check_loop.is_running():
        auto_check_loop.start()
    if not invalid_countdown_loop.is_running():
        invalid_countdown_loop.start()

    await process_due_countdowns(source="startup")

    logger.info("Logged in as %s | Prefix: %s", bot.user, config.get("prefix", DEFAULT_PREFIX))


if __name__ == "__main__":
    ensure_dirs()
    load_config()
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing from environment variables. Add it in Railway Variables.")
    bot.run(TOKEN)

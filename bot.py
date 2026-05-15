import os
import re
import json
import asyncio
import logging
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional, Tuple, Any

import aiohttp
import discord
from discord.ext import commands, tasks

# =========================
# BASIC SETTINGS
# =========================
TOKEN = os.getenv("DISCORD_TOKEN")
DEFAULT_PREFIX = os.getenv("BOT_PREFIX", "!")

# Safer defaults for Discord / Cloudflare. You can change these with commands too.
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
UNAVAILABLE_DIR = DATA_DIR / "unavailable_vanities"
CONFIG_FILE = DATA_DIR / "vanity_config.json"
TRACKED_LENGTHS = range(1, 33)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("vanity_checker")

intents = discord.Intents.default()
intents.guilds = True
intents.message_content = True

# config gets loaded before the bot goes ready. The dynamic prefix uses this.
config = {
    "prefix": DEFAULT_PREFIX,
    "auto_enabled": False,
    "auto_minutes": DEFAULT_AUTO_MINUTES,
    "delay_seconds": DEFAULT_DELAY_SECONDS,
    "batch_size": DEFAULT_BATCH_SIZE,
    "batch_cooldown_seconds": DEFAULT_BATCH_COOLDOWN_SECONDS,
    "list_cooldown_seconds": DEFAULT_LIST_COOLDOWN_SECONDS,
    "lists": {}
}


def get_prefix(bot_obj, message):
    return config.get("prefix", DEFAULT_PREFIX)


bot = commands.Bot(command_prefix=get_prefix, intents=intents, help_command=None)

unavailable_cache = defaultdict(set)

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
    config["lists"] = loaded.get("lists", {}) if isinstance(loaded.get("lists", {}), dict) else {}


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
    # Discord invite codes are usually letters/numbers/underscore/hyphen.
    text = re.sub(r"[^a-z0-9_-]", "", text)
    return text


def parse_words(words: str) -> list[str]:
    # Supports comma-separated, space-separated, or newline-separated words.
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

# =========================
# INVITE CHECKING
# =========================

async def fetch_invite_status(session: aiohttp.ClientSession, code: str) -> Tuple[str, Optional[Any]]:
    """
    Returns:
      available  = Discord says Unknown Invite / 404. This is the one hunters usually want.
      taken      = Invite exists / 200.
      rate_limited = Retry failed too many times.
      blocked    = Cloudflare / HTML / non-JSON response. The checker should stop or cool down.
      error      = Other HTTP/network issue.
      stopped    = User requested stop.
    """
    code = clean_code(code)
    if not code:
        return "error", "Empty code"

    url = f"{API_BASE}/{code}?with_counts=true&with_expiration=true"
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 DiscordBot VanityChecker/2.0"
    }

    for attempt in range(1, MAX_RETRIES + 1):
        if check_state["stop_requested"]:
            return "stopped", None

        try:
            async with session.get(url, headers=headers) as resp:
                status = resp.status
                content_type = resp.headers.get("content-type", "").lower()
                text = await resp.text()

                # Cloudflare/challenge pages are HTML, not JSON. Do not keep hammering it.
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
                    # Discord code 10006 = Unknown Invite.
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
    embed.add_field(name="Available", value=str(available), inline=True)
    embed.add_field(name="Taken/Unavailable", value=str(taken), inline=True)
    embed.add_field(name="Errors", value=str(errors), inline=True)
    embed.add_field(name="Cloudflare Blocks", value=str(blocked), inline=True)
    embed.add_field(name="Added To Unavailable Files", value=str(added), inline=True)
    embed.add_field(name="Removed From Unavailable Files", value=str(removed), inline=True)
    embed.add_field(name="List Last Updated", value=format_time(updated_at), inline=False)
    embed.set_footer(text="Vanity checker • available means Discord returned Unknown Invite")
    return embed


async def send_available_words(channel, list_name: str, available_found: list[str]):
    if not available_found:
        await safe_send(channel, f"No available words found for `{list_name}`.")
        return

    paragraph = ", ".join(available_found)
    if len(paragraph) <= 1900:
        await safe_send(channel, f"Available words for `{list_name}`:\n```txt\n{paragraph}\n```")
        return

    file_path = DATA_DIR / f"{list_name}_available_words.txt"
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(paragraph)

    await safe_send(
        channel,
        content=f"Available words for `{list_name}` were too long for one message, so here is the txt file:",
        file=discord.File(str(file_path), filename=file_path.name)
    )

# =========================
# CHECK RUNNERS
# =========================

async def run_list_check(list_name: str, list_data: dict, manual_ctx=None):
    claim_channel = await get_channel(list_data.get("claim_channel_id"))
    log_channel = await get_channel(list_data.get("log_channel_id"))
    summary_channel = await get_channel(list_data.get("summary_channel_id"))
    ping_role_id = list_data.get("ping_role_id")
    words = list_data.get("words", [])

    if not claim_channel or not log_channel or not summary_channel:
        if manual_ctx:
            await manual_ctx.send(f"`{list_name}` has a missing or broken channel setup. Run `{config['prefix']}listinfo {list_name}` and fix the channels.")
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
                    available_count += 1
                    available_found.append(code)

                    if remove_unavailable(code):
                        removed_count += 1
                        await safe_send(log_channel, f"`discord.gg/{code}` is now available and was removed from unavailable files.")

                    await safe_send(claim_channel, f"discord.gg/{code}")

                elif result == "taken":
                    taken_count += 1
                    if add_unavailable(code):
                        added_count += 1
                        await safe_send(log_channel, f"{length} letters | Taken/unavailable: `discord.gg/{code}`")

                elif result == "blocked":
                    blocked_count += 1
                    await safe_send(log_channel, f"Cloudflare block while checking `discord.gg/{code}`: `{short_text(payload, 350)}`")
                    await safe_send(summary_channel, f"Cloudflare blocked the checker. Pausing `{list_name}` for safety. Wait at least `{BLOCK_COOLDOWN_SECONDS // 60}` minutes before trying again.")
                    check_state["stop_requested"] = True
                    break

                elif result == "rate_limited":
                    error_count += 1
                    await safe_send(log_channel, f"Rate limited checking `discord.gg/{code}`: `{short_text(payload, 350)}`")

                else:
                    error_count += 1
                    await safe_send(log_channel, f"Error checking `discord.gg/{code}`: `{short_text(payload, 350)}`")

                if status_msg and (index == 1 or index % 10 == 0 or index == len(cleaned_codes)):
                    try:
                        await status_msg.edit(
                            content=(
                                f"Checking `{list_name}`...\n"
                                f"Progress: `{index}/{len(cleaned_codes)}`\n"
                                f"Available: `{available_count}` | Taken: `{taken_count}` | Errors: `{error_count}` | Blocks: `{blocked_count}`"
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
        await send_available_words(claim_channel, list_name, available_found)

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

# =========================
# COMMANDS
# =========================

@bot.command(name="help")
async def help_command(ctx):
    p = config.get("prefix", DEFAULT_PREFIX)
    embed = discord.Embed(
        title="Vanity Checker Help",
        description="Checks invite codes safely using Discord's API, handles rate limits, and stops on Cloudflare HTML blocks instead of crashing.",
        color=discord.Color.blurple()
    )
    embed.add_field(
        name="Setup",
        value=(
            f"`{p}addlist <name> <claim_channel> <log_channel> <summary_channel> <ping_role|none> <words>`\n"
            f"Example:\n`{p}addlist 4letters #claims #vanity-logs #summaries @Hunters love, hate, void, glow`"
        ),
        inline=False
    )
    embed.add_field(
        name="Manage Lists",
        value=(
            f"`{p}lists`\n`{p}listinfo <name>`\n`{p}addwords <name> <words>`\n"
            f"`{p}removewords <name> <words>`\n`{p}setchannels <name> <claim> <log> <summary>`\n"
            f"`{p}setpingrole <name> <role|none>`\n`{p}removelist <name>`\n`{p}clearlists`"
        ),
        inline=False
    )
    embed.add_field(
        name="Checks",
        value=f"`{p}checklist <name>`\n`{p}checkall`\n`{p}stop`",
        inline=False
    )
    embed.add_field(
        name="Auto Checks",
        value=f"`{p}autocheck <minutes>`\n`{p}autostop`\n`{p}autostatus`",
        inline=False
    )
    embed.add_field(
        name="Settings",
        value=f"`{p}setprefix <prefix>`\n`{p}ratelimit <delay_seconds> <batch_size> <batch_cooldown_seconds> <list_cooldown_seconds>`",
        inline=False
    )
    embed.add_field(
        name="Unavailable Files",
        value=f"`{p}unavailablecount <length>`\n`{p}getunavailable <length>`\n`{p}clearunavailable [length]`",
        inline=False
    )
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
async def addlist(ctx, name: str, claim_channel: discord.TextChannel, log_channel: discord.TextChannel, summary_channel: discord.TextChannel, ping_role_input: str, *, words: str):
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
        "claim_channel_id": claim_channel.id,
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
        f"Claims: {claim_channel.mention}\n"
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
async def setchannels(ctx, name: str, claim_channel: discord.TextChannel, log_channel: discord.TextChannel, summary_channel: discord.TextChannel):
    name = clean_code(name)
    if name not in config["lists"]:
        await ctx.send(f"No list named `{name}` exists.")
        return

    config["lists"][name]["claim_channel_id"] = claim_channel.id
    config["lists"][name]["log_channel_id"] = log_channel.id
    config["lists"][name]["summary_channel_id"] = summary_channel.id
    config["lists"][name]["updated_at"] = now_iso()
    save_config()
    await ctx.send(f"Updated channels for `{name}`: claims {claim_channel.mention}, logs {log_channel.mention}, summaries {summary_channel.mention}.")


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
    for name, data in config["lists"].items():
        embed.add_field(
            name=name,
            value=(
                f"Words: `{len(data.get('words', []))}`\n"
                f"Claims: <#{data.get('claim_channel_id')}>\n"
                f"Logs: <#{data.get('log_channel_id')}>\n"
                f"Summaries: <#{data.get('summary_channel_id')}>\n"
                f"Ping: {f'<@&{data.get('ping_role_id')}>' if data.get('ping_role_id') else '`None`'}\n"
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

    data = config["lists"][name]
    embed = discord.Embed(title=f"List Info: {name}", color=discord.Color.blurple())
    embed.add_field(name="Words", value=str(len(data.get("words", []))), inline=True)
    embed.add_field(name="Created", value=format_time(data.get("created_at")), inline=False)
    embed.add_field(name="Last Updated", value=format_time(data.get("updated_at")), inline=False)
    embed.add_field(name="Claim Channel", value=f"<#{data.get('claim_channel_id')}>", inline=True)
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


@bot.command(name="unavailablecount", aliases=["invalidcount"])
async def unavailablecount(ctx, length: int):
    if length < 1 or length > 32:
        await ctx.send("Use a length between 1 and 32.")
        return
    load_unavailable_cache()
    await ctx.send(f"{length}-letter unavailable count: `{len(unavailable_cache[length])}`")


@bot.command(name="getunavailable", aliases=["getinvalid"])
async def getunavailable(ctx, length: int):
    if length < 1 or length > 32:
        await ctx.send("Use a length between 1 and 32.")
        return
    load_unavailable_cache()
    rewrite_unavailable_file(length)
    path = unavailable_file(length)
    await ctx.send(content=f"Unavailable file for `{length}` letters:", file=discord.File(str(path), filename=path.name))


@bot.command(name="clearunavailable", aliases=["clearinvalid"])
@commands.has_permissions(administrator=True)
async def clearunavailable(ctx, length: int = None):
    load_unavailable_cache()
    if length is None:
        for l in TRACKED_LENGTHS:
            unavailable_cache[l].clear()
            rewrite_unavailable_file(l)
        await ctx.send("Cleared all unavailable files.")
        return
    if length < 1 or length > 32:
        await ctx.send("Use a length between 1 and 32.")
        return
    unavailable_cache[length].clear()
    rewrite_unavailable_file(length)
    await ctx.send(f"Cleared unavailable file for `{length}` letters.")


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

    if not auto_check_loop.is_running():
        auto_check_loop.start()

    logger.info("Logged in as %s | Prefix: %s", bot.user, config.get("prefix", DEFAULT_PREFIX))


if __name__ == "__main__":
    ensure_dirs()
    load_config()
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing from environment variables. Add it in Railway Variables.")
    bot.run(TOKEN)

import os
import json
import asyncio
import logging
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

# =========================================================
# VANITY CHECKER BOT
# =========================================================
# Meaning used by this bot:
# - Available / Untaken = discord.gg/code does NOT currently point to a server
# - Taken / Unavailable = discord.gg/code already points to a server
# =========================================================

TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("BOT_PREFIX", "!")

DELAY_SECONDS = float(os.getenv("DELAY_SECONDS", "3"))
BACKOFF_SECONDS = int(os.getenv("BACKOFF_SECONDS", "60"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "2"))
MAX_CODES_PER_LIST = int(os.getenv("MAX_CODES_PER_LIST", "1000"))
MIN_AUTO_MINUTES = int(os.getenv("MIN_AUTO_MINUTES", "5"))

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
INVALID_DIR = DATA_DIR / "available_vanities"
CONFIG_FILE = DATA_DIR / "vanity_config.json"
TRACKED_LENGTHS = range(1, 33)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("vanity_checker")

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

# One global lock means only one list/check batch can run at once.
check_lock = asyncio.Lock()

invalid_cache = defaultdict(set)

check_state = {
    "running": False,
    "stop_requested": False,
    "current": 0,
    "total": 0,
    "mode": None,
}

default_config = {
    "auto_enabled": False,
    "auto_minutes": 60,
    "lists": {}
}

config = default_config.copy()


# =========================================================
# BASIC HELPERS
# =========================================================

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def format_time(iso_time: str | None) -> str:
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
    INVALID_DIR.mkdir(parents=True, exist_ok=True)


def save_config() -> None:
    ensure_dirs()
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4)


def load_config() -> None:
    global config
    ensure_dirs()

    if not CONFIG_FILE.exists():
        config = {
            "auto_enabled": False,
            "auto_minutes": 60,
            "lists": {}
        }
        save_config()
        return

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)

        config = {
            "auto_enabled": loaded.get("auto_enabled", False),
            "auto_minutes": loaded.get("auto_minutes", 60),
            "lists": loaded.get("lists", {})
        }

    except Exception:
        logger.exception("Failed to load config. Creating backup and fresh config.")
        backup = CONFIG_FILE.with_suffix(".broken.json")
        try:
            CONFIG_FILE.rename(backup)
        except Exception:
            pass

        config = {
            "auto_enabled": False,
            "auto_minutes": 60,
            "lists": {}
        }
        save_config()


def clean_code(item: str) -> str:
    return (
        str(item)
        .replace("https://discord.gg/", "")
        .replace("http://discord.gg/", "")
        .replace("discord.gg/", "")
        .replace("https://discord.com/invite/", "")
        .replace("http://discord.com/invite/", "")
        .replace("discord.com/invite/", "")
        .strip()
        .strip("/")
        .lower()
    )


def parse_words(words: str) -> list[str]:
    seen = set()
    cleaned = []

    for item in words.replace("\n", ",").split(","):
        code = clean_code(item)

        if not code:
            continue

        if code in seen:
            continue

        seen.add(code)
        cleaned.append(code)

    return cleaned


def available_file(length: int) -> Path:
    return INVALID_DIR / f"available_{length}_letters.txt"


def ensure_available_files() -> None:
    ensure_dirs()
    for length in TRACKED_LENGTHS:
        available_file(length).touch(exist_ok=True)


def load_available_cache() -> None:
    invalid_cache.clear()
    ensure_available_files()

    for length in TRACKED_LENGTHS:
        path = available_file(length)
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    code = clean_code(line)
                    if code and len(code) == length:
                        invalid_cache[length].add(code)
        except Exception:
            logger.exception("Failed reading available file: %s", path)


def rewrite_available_file(length: int) -> None:
    ensure_available_files()
    path = available_file(length)

    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for code in sorted(invalid_cache[length]):
            f.write(code + "\n")


def add_available(code: str) -> bool:
    code = clean_code(code)
    length = len(code)

    if length not in TRACKED_LENGTHS:
        return False

    before = len(invalid_cache[length])
    invalid_cache[length].add(code)

    if len(invalid_cache[length]) != before:
        rewrite_available_file(length)
        return True

    return False


def remove_available(code: str) -> bool:
    code = clean_code(code)
    length = len(code)

    if length not in TRACKED_LENGTHS:
        return False

    if code in invalid_cache[length]:
        invalid_cache[length].remove(code)
        rewrite_available_file(length)
        return True

    return False


def is_list_ready(data: dict) -> tuple[bool, str]:
    required = [
        "claim_channel_id",
        "copy_channel_id",
        "log_channel_id",
        "summary_channel_id",
        "ping_role_id",
        "words"
    ]

    for key in required:
        if key not in data or data[key] in (None, "", []):
            return False, f"Missing `{key}`"

    return True, "Ready"


# =========================================================
# DISCORD HELPERS
# =========================================================

async def get_channel_safe(channel_id):
    if not channel_id:
        return None

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


def find_role(ctx, role_input: str):
    role_input = str(role_input).strip()

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


async def sleep_with_stop(seconds: float) -> bool:
    waited = 0.0

    while waited < seconds:
        if check_state["stop_requested"]:
            return True

        await asyncio.sleep(0.5)
        waited += 0.5

    return False


async def fetch_invite_status(code: str):
    """
    Returns:
    - ("taken", invite)        discord.gg/code points to a server
    - ("available", None)      invite not found, therefore untaken/available
    - ("error", text)
    - ("fatal", text)
    - ("stopped", None)
    """
    for attempt in range(1, MAX_RETRIES + 1):
        if check_state["stop_requested"]:
            return "stopped", None

        try:
            invite = await bot.fetch_invite(code)
            return "taken", invite

        except discord.NotFound:
            return "available", None

        except discord.Forbidden as e:
            return "fatal", f"Forbidden: {e}"

        except discord.HTTPException as e:
            logger.warning("HTTP error checking %s attempt %s/%s: %s", code, attempt, MAX_RETRIES, e)

            if attempt < MAX_RETRIES:
                stopped = await sleep_with_stop(BACKOFF_SECONDS * attempt)
                if stopped:
                    return "stopped", None
                continue

            return "error", f"HTTPException: {e}"

        except Exception as e:
            logger.exception("Unexpected error checking %s", code)

            if attempt < MAX_RETRIES:
                stopped = await sleep_with_stop(BACKOFF_SECONDS * attempt)
                if stopped:
                    return "stopped", None
                continue

            return "error", f"{type(e).__name__}: {e}"

    return "error", "Unknown error"


# =========================================================
# EMBEDS / OUTPUT
# =========================================================

def build_summary_embed(
    list_name,
    processed,
    available,
    taken,
    errors,
    added_available,
    removed_available,
    stopped,
    updated_at
):
    embed = discord.Embed(
        title=f"{'Check Stopped' if stopped else 'Check Finished'}: {list_name}",
        color=discord.Color.orange() if stopped else discord.Color.green()
    )

    embed.add_field(name="Processed", value=str(processed), inline=True)
    embed.add_field(name="Available / Untaken", value=str(available), inline=True)
    embed.add_field(name="Taken / Unavailable", value=str(taken), inline=True)
    embed.add_field(name="Errors", value=str(errors), inline=True)
    embed.add_field(name="Added to Available Files", value=str(added_available), inline=True)
    embed.add_field(name="Removed from Available Files", value=str(removed_available), inline=True)
    embed.add_field(name="List Last Updated", value=format_time(updated_at), inline=False)
    embed.set_footer(text="Vanity checker")

    return embed


async def send_copy_paste_words(channel, list_name: str, available_found: list[str]) -> None:
    if not available_found:
        await safe_send(channel, f"No available / untaken words found for `{list_name}`.")
        return

    paragraph = ", ".join(available_found)

    if len(paragraph) <= 1900:
        await safe_send(channel, f"Available / untaken words for `{list_name}`:\n```txt\n{paragraph}\n```")
        return

    file_path = DATA_DIR / f"{list_name}_available_words.txt"

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(paragraph)

    await safe_send(
        channel,
        content=f"Available / untaken words for `{list_name}` were too long for one message:",
        file=discord.File(str(file_path), filename=file_path.name)
    )


# =========================================================
# CHECK ENGINE
# =========================================================

async def run_list_check_unlocked(list_name: str, list_data: dict, manual_ctx=None) -> None:
    ready, reason = is_list_ready(list_data)

    if not ready:
        if manual_ctx:
            await manual_ctx.send(f"`{list_name}` is not ready. {reason}. Use `{PREFIX}list status {list_name}`.")
        return

    claim_channel = await get_channel_safe(list_data["claim_channel_id"])
    copy_channel = await get_channel_safe(list_data["copy_channel_id"])
    log_channel = await get_channel_safe(list_data["log_channel_id"])
    summary_channel = await get_channel_safe(list_data["summary_channel_id"])

    if not claim_channel or not copy_channel or not log_channel or not summary_channel:
        if manual_ctx:
            await manual_ctx.send(f"`{list_name}` has a channel I cannot access. Check permissions or reset the channels.")
        return

    words = list_data.get("words", [])
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

    if len(cleaned_codes) > MAX_CODES_PER_LIST:
        cleaned_codes = cleaned_codes[:MAX_CODES_PER_LIST]

    check_state["running"] = True
    check_state["stop_requested"] = False
    check_state["current"] = 0
    check_state["total"] = len(cleaned_codes)
    check_state["mode"] = list_name

    available_count = 0
    taken_count = 0
    error_count = 0
    added_available_count = 0
    removed_available_count = 0
    available_found = []

    status_msg = await safe_send(
        summary_channel,
        f"Checking `{list_name}` — `{len(cleaned_codes)}` word(s)..."
    )

    try:
        for index, code in enumerate(cleaned_codes, start=1):
            check_state["current"] = index

            if check_state["stop_requested"]:
                break

            result, payload = await fetch_invite_status(code)

            if result == "stopped":
                break

            if result == "available":
                available_count += 1
                available_found.append(code)

                if add_available(code):
                    added_available_count += 1
                    await safe_send(log_channel, f"✅ Available / untaken: `{code}`")

                await safe_send(claim_channel, f"discord.gg/{code}")

            elif result == "taken":
                taken_count += 1

                if remove_available(code):
                    removed_available_count += 1
                    await safe_send(log_channel, f"❌ Taken / unavailable now: `{code}`")

            elif result == "fatal":
                error_count += 1
                await safe_send(log_channel, f"⚠️ Fatal error checking `{code}`: `{payload}`")

            else:
                error_count += 1
                await safe_send(log_channel, f"⚠️ Error checking `{code}`: `{payload}`")

            if status_msg and (index == 1 or index % 10 == 0 or index == len(cleaned_codes)):
                try:
                    await status_msg.edit(
                        content=(
                            f"Checking `{list_name}`...\n"
                            f"Progress: `{index}/{len(cleaned_codes)}`\n"
                            f"Available / Untaken: `{available_count}` | "
                            f"Taken / Unavailable: `{taken_count}` | Errors: `{error_count}`"
                        )
                    )
                except Exception:
                    pass

            if index < len(cleaned_codes):
                stopped = await sleep_with_stop(DELAY_SECONDS)
                if stopped:
                    break

    except Exception as e:
        logger.exception("List check failed for %s", list_name)
        await safe_send(summary_channel, f"⚠️ `{list_name}` had an unexpected error, but the bot kept running.\n`{type(e).__name__}: {e}`")

    finally:
        stopped = check_state["stop_requested"]

        # Always send the clean copy/paste list to the selected copy channel.
        await send_copy_paste_words(copy_channel, list_name, available_found)

        embed = build_summary_embed(
            list_name=list_name,
            processed=check_state["current"],
            available=available_count,
            taken=taken_count,
            errors=error_count,
            added_available=added_available_count,
            removed_available=removed_available_count,
            stopped=stopped,
            updated_at=list_data.get("updated_at")
        )

        ping_role_id = list_data.get("ping_role_id")
        ping_text = f"<@&{ping_role_id}> " if ping_role_id else ""

        await safe_send(summary_channel, content=ping_text, embed=embed)

        check_state["running"] = False
        check_state["stop_requested"] = False
        check_state["current"] = 0
        check_state["total"] = 0
        check_state["mode"] = None


async def run_one_list_with_lock(list_name: str, manual_ctx=None) -> None:
    if check_lock.locked():
        if manual_ctx:
            await manual_ctx.send("List is already running, please wait.")
        return

    list_name = list_name.lower()

    if list_name not in config["lists"]:
        if manual_ctx:
            await manual_ctx.send(f"No list named `{list_name}` exists.")
        return

    async with check_lock:
        load_available_cache()
        await run_list_check_unlocked(list_name, config["lists"][list_name], manual_ctx=manual_ctx)


async def run_all_checks(manual_ctx=None) -> None:
    if check_lock.locked():
        if manual_ctx:
            await manual_ctx.send("List is already running, please wait.")
        return

    async with check_lock:
        load_available_cache()

        if not config["lists"]:
            if manual_ctx:
                await manual_ctx.send("No saved lists found.")
            return

        for list_name, list_data in list(config["lists"].items()):
            if check_state["stop_requested"]:
                break

            try:
                await run_list_check_unlocked(list_name, list_data, manual_ctx=manual_ctx)
                await asyncio.sleep(3)
            except Exception as e:
                logger.exception("Failed running list %s", list_name)

                summary_channel = await get_channel_safe(list_data.get("summary_channel_id"))
                await safe_send(
                    summary_channel,
                    f"⚠️ `{list_name}` had an error, but the bot continued.\n`{type(e).__name__}: {e}`"
                )


# =========================================================
# AUTO LOOP
# =========================================================

@tasks.loop(minutes=1)
async def auto_check_loop():
    if not config.get("auto_enabled", False):
        return

    minutes = int(config.get("auto_minutes", 60))

    if not hasattr(auto_check_loop, "counter"):
        auto_check_loop.counter = 0

    auto_check_loop.counter += 1

    if auto_check_loop.counter < minutes:
        return

    auto_check_loop.counter = 0

    if check_lock.locked():
        return

    await run_all_checks()


# =========================================================
# HELP COMMAND
# =========================================================

@bot.command(name="help")
async def help_command(ctx):
    embed = discord.Embed(
        title="Vanity Checker Help",
        description=(
            "Checks Discord invite codes across multiple saved lists.\n\n"
            "**Available / Untaken** = invite does not point to a server.\n"
            "**Taken / Unavailable** = invite already points to a server."
        ),
        color=discord.Color.blurple()
    )

    embed.add_field(
        name="Easy Setup",
        value=(
            f"`{PREFIX}list create <name>`\n"
            f"`{PREFIX}list channels <name> <claims> <copy> <logs> <summaries>`\n"
            f"`{PREFIX}list role <name> <role>`\n"
            f"`{PREFIX}list words <name> <comma separated words>`\n"
            f"`{PREFIX}list run <name>`"
        ),
        inline=False
    )

    embed.add_field(
        name="Quick Setup",
        value=(
            f"`{PREFIX}addlist <name> <claims> <copy> <logs> <summaries> <role> <words>`\n\n"
            f"Example:\n"
            f"`{PREFIX}addlist 4letters #claims #copy #logs #summaries @Hunters love, hate, void, glow`"
        ),
        inline=False
    )

    embed.add_field(
        name="List Commands",
        value=(
            f"`{PREFIX}list all`\n"
            f"`{PREFIX}list status <name>`\n"
            f"`{PREFIX}list remove <name>`\n"
            f"`{PREFIX}list clearall`\n"
            f"`{PREFIX}list append <name> <words>`\n"
            f"`{PREFIX}list remove_words <name> <words>`"
        ),
        inline=False
    )

    embed.add_field(
        name="Checking",
        value=(
            f"`{PREFIX}checkall`\n"
            f"`{PREFIX}checklist <name>`\n"
            f"`{PREFIX}stop`"
        ),
        inline=False
    )

    embed.add_field(
        name="Auto Checks",
        value=(
            f"`{PREFIX}autocheck <minutes>`\n"
            f"`{PREFIX}autostop`\n"
            f"`{PREFIX}autostatus`"
        ),
        inline=False
    )

    embed.add_field(
        name="Available Files",
        value=(
            f"`{PREFIX}availablecount <length>`\n"
            f"`{PREFIX}getavailable <length>`\n"
            f"`{PREFIX}clearavailable [length]`"
        ),
        inline=False
    )

    await ctx.send(embed=embed)


# =========================================================
# LIST COMMAND GROUP
# =========================================================

@bot.group(name="list", invoke_without_command=True)
async def list_group(ctx):
    await ctx.send(f"Use `{PREFIX}help` to see list setup syntax.")


@list_group.command(name="create")
@commands.has_permissions(administrator=True)
async def list_create(ctx, name: str):
    name = name.lower()

    if name in config["lists"]:
        await ctx.send(f"`{name}` already exists.")
        return

    timestamp = now_iso()

    config["lists"][name] = {
        "claim_channel_id": None,
        "copy_channel_id": None,
        "log_channel_id": None,
        "summary_channel_id": None,
        "ping_role_id": None,
        "words": [],
        "created_at": timestamp,
        "updated_at": timestamp
    }

    save_config()
    await ctx.send(f"✅ Created list `{name}`.")


@list_group.command(name="channels")
@commands.has_permissions(administrator=True)
async def list_channels(
    ctx,
    name: str,
    claim_channel: discord.TextChannel,
    copy_channel: discord.TextChannel,
    log_channel: discord.TextChannel,
    summary_channel: discord.TextChannel
):
    name = name.lower()

    if name not in config["lists"]:
        await ctx.send(f"No list named `{name}` exists. Create it first with `{PREFIX}list create {name}`.")
        return

    data = config["lists"][name]
    data["claim_channel_id"] = claim_channel.id
    data["copy_channel_id"] = copy_channel.id
    data["log_channel_id"] = log_channel.id
    data["summary_channel_id"] = summary_channel.id
    data["updated_at"] = now_iso()

    save_config()

    await ctx.send(
        f"✅ Channels saved for `{name}`.\n"
        f"Claims: {claim_channel.mention}\n"
        f"Copy list: {copy_channel.mention}\n"
        f"Logs: {log_channel.mention}\n"
        f"Summaries: {summary_channel.mention}"
    )


@list_group.command(name="role")
@commands.has_permissions(administrator=True)
async def list_role(ctx, name: str, *, role_input: str):
    name = name.lower()

    if name not in config["lists"]:
        await ctx.send(f"No list named `{name}` exists.")
        return

    role = find_role(ctx, role_input)

    if not role:
        await ctx.send("I could not find that role. Use a role mention, role ID, or exact role name.")
        return

    config["lists"][name]["ping_role_id"] = role.id
    config["lists"][name]["updated_at"] = now_iso()
    save_config()

    await ctx.send(f"✅ Ping role for `{name}` set to {role.mention}.")


@list_group.command(name="words")
@commands.has_permissions(administrator=True)
async def list_words(ctx, name: str, *, words: str):
    name = name.lower()

    if name not in config["lists"]:
        await ctx.send(f"No list named `{name}` exists.")
        return

    cleaned = parse_words(words)

    if not cleaned:
        await ctx.send("No usable words found.")
        return

    if len(cleaned) > MAX_CODES_PER_LIST:
        await ctx.send(f"Too many words. Max per list is `{MAX_CODES_PER_LIST}`.")
        return

    config["lists"][name]["words"] = cleaned
    config["lists"][name]["updated_at"] = now_iso()
    save_config()

    await ctx.send(f"✅ Saved `{len(cleaned)}` words to `{name}`.")


@list_group.command(name="append")
@commands.has_permissions(administrator=True)
async def list_append(ctx, name: str, *, words: str):
    name = name.lower()

    if name not in config["lists"]:
        await ctx.send(f"No list named `{name}` exists.")
        return

    current = config["lists"][name].get("words", [])
    cleaned = parse_words(words)

    seen = set(current)
    added = []

    for word in cleaned:
        if word not in seen:
            current.append(word)
            seen.add(word)
            added.append(word)

    if len(current) > MAX_CODES_PER_LIST:
        await ctx.send(f"This would exceed the max of `{MAX_CODES_PER_LIST}` words.")
        return

    config["lists"][name]["words"] = current
    config["lists"][name]["updated_at"] = now_iso()
    save_config()

    await ctx.send(f"✅ Added `{len(added)}` new word(s) to `{name}`.")


@list_group.command(name="remove_words")
@commands.has_permissions(administrator=True)
async def list_remove_words(ctx, name: str, *, words: str):
    name = name.lower()

    if name not in config["lists"]:
        await ctx.send(f"No list named `{name}` exists.")
        return

    remove_set = set(parse_words(words))
    old_words = config["lists"][name].get("words", [])
    new_words = [word for word in old_words if word not in remove_set]

    removed = len(old_words) - len(new_words)

    config["lists"][name]["words"] = new_words
    config["lists"][name]["updated_at"] = now_iso()
    save_config()

    await ctx.send(f"✅ Removed `{removed}` word(s) from `{name}`.")


@list_group.command(name="run")
@commands.has_permissions(administrator=True)
async def list_run(ctx, name: str):
    await run_one_list_with_lock(name, manual_ctx=ctx)


@list_group.command(name="status")
async def list_status(ctx, name: str):
    name = name.lower()

    if name not in config["lists"]:
        await ctx.send(f"No list named `{name}` exists.")
        return

    data = config["lists"][name]
    ready, reason = is_list_ready(data)

    embed = discord.Embed(
        title=f"List Status: {name}",
        color=discord.Color.green() if ready else discord.Color.orange()
    )

    embed.add_field(name="Status", value="Ready" if ready else reason, inline=False)
    embed.add_field(name="Words", value=str(len(data.get("words", []))), inline=True)
    embed.add_field(name="Created", value=format_time(data.get("created_at")), inline=False)
    embed.add_field(name="Last Updated", value=format_time(data.get("updated_at")), inline=False)

    embed.add_field(name="Claims", value=f"<#{data.get('claim_channel_id')}>" if data.get("claim_channel_id") else "Not set", inline=True)
    embed.add_field(name="Copy List", value=f"<#{data.get('copy_channel_id')}>" if data.get("copy_channel_id") else "Not set", inline=True)
    embed.add_field(name="Logs", value=f"<#{data.get('log_channel_id')}>" if data.get("log_channel_id") else "Not set", inline=True)
    embed.add_field(name="Summaries", value=f"<#{data.get('summary_channel_id')}>" if data.get("summary_channel_id") else "Not set", inline=True)
    embed.add_field(name="Ping Role", value=f"<@&{data.get('ping_role_id')}>" if data.get("ping_role_id") else "Not set", inline=True)

    preview = ", ".join(data.get("words", [])[:30])
    if len(data.get("words", [])) > 30:
        preview += "..."

    embed.add_field(name="Word Preview", value=preview or "None", inline=False)

    await ctx.send(embed=embed)


@list_group.command(name="all")
async def list_all(ctx):
    if not config["lists"]:
        await ctx.send("No saved lists yet.")
        return

    embed = discord.Embed(title="Saved Lists", color=discord.Color.blurple())

    for name, data in config["lists"].items():
        ready, reason = is_list_ready(data)
        embed.add_field(
            name=name,
            value=(
                f"Status: `{'Ready' if ready else reason}`\n"
                f"Words: `{len(data.get('words', []))}`\n"
                f"Claims: <#{data.get('claim_channel_id')}>\n"
                f"Copy: <#{data.get('copy_channel_id')}>\n"
                f"Logs: <#{data.get('log_channel_id')}>\n"
                f"Summaries: <#{data.get('summary_channel_id')}>\n"
                f"Ping: <@&{data.get('ping_role_id')}>\n"
                f"Updated: {format_time(data.get('updated_at'))}"
            ),
            inline=False
        )

    await ctx.send(embed=embed)


@list_group.command(name="remove")
@commands.has_permissions(administrator=True)
async def list_remove(ctx, name: str):
    name = name.lower()

    if name not in config["lists"]:
        await ctx.send(f"No list named `{name}` exists.")
        return

    del config["lists"][name]
    save_config()

    await ctx.send(f"✅ Removed list `{name}`.")


@list_group.command(name="clearall")
@commands.has_permissions(administrator=True)
async def list_clearall(ctx):
    config["lists"] = {}
    save_config()

    await ctx.send("✅ Removed all saved lists.")


# =========================================================
# QUICK OLD-STYLE COMMANDS
# =========================================================

@bot.command()
@commands.has_permissions(administrator=True)
async def addlist(
    ctx,
    name: str,
    claim_channel: discord.TextChannel,
    copy_channel: discord.TextChannel,
    log_channel: discord.TextChannel,
    summary_channel: discord.TextChannel,
    role: discord.Role,
    *,
    words: str
):
    cleaned = parse_words(words)

    if not cleaned:
        await ctx.send("No usable words found.")
        return

    if len(cleaned) > MAX_CODES_PER_LIST:
        await ctx.send(f"Too many words. Max per list is `{MAX_CODES_PER_LIST}`.")
        return

    name = name.lower()
    timestamp = now_iso()
    old_created = config["lists"].get(name, {}).get("created_at", timestamp)

    config["lists"][name] = {
        "claim_channel_id": claim_channel.id,
        "copy_channel_id": copy_channel.id,
        "log_channel_id": log_channel.id,
        "summary_channel_id": summary_channel.id,
        "ping_role_id": role.id,
        "words": cleaned,
        "created_at": old_created,
        "updated_at": timestamp
    }

    save_config()

    await ctx.send(
        f"✅ Saved list `{name}` with `{len(cleaned)}` words.\n"
        f"Claims: {claim_channel.mention}\n"
        f"Copy list: {copy_channel.mention}\n"
        f"Logs: {log_channel.mention}\n"
        f"Summaries: {summary_channel.mention}\n"
        f"Ping: {role.mention}\n"
        f"Updated: {format_time(timestamp)}"
    )


@bot.command()
@commands.has_permissions(administrator=True)
async def checklist(ctx, name: str):
    await run_one_list_with_lock(name, manual_ctx=ctx)


@bot.command()
@commands.has_permissions(administrator=True)
async def checkall(ctx):
    await run_all_checks(manual_ctx=ctx)


@bot.command()
async def stop(ctx):
    if not check_state["running"]:
        await ctx.send("No list is currently running.")
        return

    check_state["stop_requested"] = True
    await ctx.send(
        f"Stop requested for `{check_state['mode']}`. "
        f"Progress: `{check_state['current']}/{check_state['total']}`."
    )


# =========================================================
# AUTO COMMANDS
# =========================================================

@bot.command()
@commands.has_permissions(administrator=True)
async def autocheck(ctx, minutes: int):
    if minutes < MIN_AUTO_MINUTES:
        await ctx.send(f"Use at least `{MIN_AUTO_MINUTES}` minutes to avoid rate limits.")
        return

    config["auto_enabled"] = True
    config["auto_minutes"] = minutes
    save_config()

    auto_check_loop.counter = 0

    await ctx.send(f"✅ Automatic checks enabled every `{minutes}` minute(s). Lists will run one-by-one, never overlapping.")


@bot.command()
@commands.has_permissions(administrator=True)
async def autostop(ctx):
    config["auto_enabled"] = False
    save_config()

    await ctx.send("✅ Automatic checks disabled.")


@bot.command()
async def autostatus(ctx):
    await ctx.send(
        f"Auto checks: `{'Enabled' if config['auto_enabled'] else 'Disabled'}`\n"
        f"Interval: `{config['auto_minutes']}` minute(s)\n"
        f"Saved lists: `{len(config['lists'])}`\n"
        f"Currently running: `{'Yes' if check_state['running'] else 'No'}`\n"
        f"Locked: `{'Yes' if check_lock.locked() else 'No'}`"
    )


# =========================================================
# AVAILABLE FILE COMMANDS
# =========================================================

@bot.command()
async def availablecount(ctx, length: int):
    if length < 1 or length > 32:
        await ctx.send("Use a length between 1 and 32.")
        return

    load_available_cache()
    await ctx.send(f"{length}-letter available / untaken count: `{len(invalid_cache[length])}`")


@bot.command()
async def getavailable(ctx, length: int):
    if length < 1 or length > 32:
        await ctx.send("Use a length between 1 and 32.")
        return

    load_available_cache()
    rewrite_available_file(length)

    path = available_file(length)

    await ctx.send(
        content=f"Available / untaken file for `{length}` letters:",
        file=discord.File(str(path), filename=path.name)
    )


@bot.command()
@commands.has_permissions(administrator=True)
async def clearavailable(ctx, length: int = None):
    if check_lock.locked():
        await ctx.send("List is already running, please wait.")
        return

    load_available_cache()

    if length is None:
        for l in TRACKED_LENGTHS:
            invalid_cache[l].clear()
            rewrite_available_file(l)

        await ctx.send("✅ Cleared all available / untaken files.")
        return

    if length < 1 or length > 32:
        await ctx.send("Use a length between 1 and 32.")
        return

    invalid_cache[length].clear()
    rewrite_available_file(length)

    await ctx.send(f"✅ Cleared available / untaken file for `{length}` letters.")


# Backwards-compatible aliases
invalidcount = availablecount
getinvalid = getavailable
clearinvalid = clearavailable


# =========================================================
# UTILITY
# =========================================================

@bot.command()
@commands.has_permissions(manage_messages=True)
async def purge(ctx, amount: int):
    if amount < 1:
        await ctx.send("Use a number above 0.")
        return

    deleted = await ctx.channel.purge(limit=amount + 1)
    msg = await ctx.send(f"Deleted `{max(len(deleted) - 1, 0)}` messages.")
    await asyncio.sleep(3)
    try:
        await msg.delete()
    except Exception:
        pass


# =========================================================
# ERRORS / READY
# =========================================================

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You do not have permission to use that command.")
        return

    if isinstance(error, commands.BadArgument):
        await ctx.send(f"Bad command format. Use `{PREFIX}help` to see the correct syntax.")
        return

    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing something in the command. Use `{PREFIX}help` to see the syntax.")
        return

    if isinstance(error, commands.CommandNotFound):
        return

    logger.exception("Command error: %s", error)
    await ctx.send(f"An error happened, but the bot is still running. Use `{PREFIX}help` to check syntax.")


@bot.event
async def on_ready():
    ensure_dirs()
    ensure_available_files()
    load_config()
    load_available_cache()

    if not auto_check_loop.is_running():
        auto_check_loop.start()

    logger.info("Logged in as %s | Prefix: %s", bot.user, PREFIX)
    logger.info("Saved lists: %s", len(config["lists"]))
    logger.info("Auto enabled: %s every %s minutes", config["auto_enabled"], config["auto_minutes"])


if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing. Add it to your environment variables as DISCORD_TOKEN.")

bot.run(TOKEN)

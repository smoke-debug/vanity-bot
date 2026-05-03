import os
import json
import asyncio
import logging
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

TOKEN = os.getenv("DISCORD_TOKEN")

PREFIX = "!"
DELAY_SECONDS = 3
MAX_RETRIES = 2
BACKOFF_SECONDS = 60
MAX_CODES_PER_LIST = 1000

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
INVALID_DIR = DATA_DIR / "invalid_vanities"
CONFIG_FILE = DATA_DIR / "vanity_config.json"

TRACKED_LENGTHS = range(1, 33)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vanity_checker")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

invalid_cache = defaultdict(set)

check_state = {
    "running": False,
    "stop_requested": False,
    "current": 0,
    "total": 0,
    "mode": None,
}

config = {
    "auto_enabled": False,
    "auto_minutes": 60,
    "lists": {}
}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def format_time(iso_time):
    if not iso_time:
        return "Unknown"

    try:
        dt = datetime.fromisoformat(iso_time)
        unix = int(dt.timestamp())
        return f"<t:{unix}:F> • <t:{unix}:R>"
    except Exception:
        return "Unknown"


def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    INVALID_DIR.mkdir(parents=True, exist_ok=True)


def save_config():
    ensure_dirs()
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4)


def load_config():
    global config
    ensure_dirs()

    if not CONFIG_FILE.exists():
        save_config()
        return

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        loaded = json.load(f)

    config["auto_enabled"] = loaded.get("auto_enabled", False)
    config["auto_minutes"] = loaded.get("auto_minutes", 60)
    config["lists"] = loaded.get("lists", {})


def invalid_file(length):
    return INVALID_DIR / f"invalid_{length}_letters.txt"


def ensure_invalid_files():
    ensure_dirs()
    for length in TRACKED_LENGTHS:
        invalid_file(length).touch(exist_ok=True)


def clean_code(item):
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


def parse_words(words):
    seen = set()
    cleaned = []

    for item in words.split(","):
        code = clean_code(item)

        if not code or code in seen:
            continue

        seen.add(code)
        cleaned.append(code)

    return cleaned


def load_invalid_cache():
    invalid_cache.clear()
    ensure_invalid_files()

    for length in TRACKED_LENGTHS:
        path = invalid_file(length)
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                code = clean_code(line)
                if code and len(code) == length:
                    invalid_cache[length].add(code)


def rewrite_invalid_file(length):
    path = invalid_file(length)
    with open(path, "w", encoding="utf-8") as f:
        for code in sorted(invalid_cache[length]):
            f.write(code + "\n")


def add_invalid(code):
    code = clean_code(code)
    length = len(code)

    if length not in TRACKED_LENGTHS:
        return False

    before = len(invalid_cache[length])
    invalid_cache[length].add(code)

    if len(invalid_cache[length]) != before:
        rewrite_invalid_file(length)
        return True

    return False


def remove_invalid(code):
    code = clean_code(code)
    length = len(code)

    if length not in TRACKED_LENGTHS:
        return False

    if code in invalid_cache[length]:
        invalid_cache[length].remove(code)
        rewrite_invalid_file(length)
        return True

    return False


async def get_channel(channel_id):
    channel = bot.get_channel(int(channel_id))

    if channel:
        return channel

    try:
        return await bot.fetch_channel(int(channel_id))
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


async def sleep_with_stop(seconds):
    waited = 0

    while waited < seconds:
        if check_state["stop_requested"]:
            return True

        await asyncio.sleep(0.5)
        waited += 0.5

    return False


async def fetch_invite_status(code):
    for attempt in range(1, MAX_RETRIES + 1):
        if check_state["stop_requested"]:
            return "stopped", None

        try:
            invite = await bot.fetch_invite(code)
            return "valid", invite

        except discord.NotFound:
            return "invalid", None

        except discord.Forbidden as e:
            return "fatal", str(e)

        except discord.HTTPException as e:
            if attempt < MAX_RETRIES:
                stopped = await sleep_with_stop(BACKOFF_SECONDS * attempt)
                if stopped:
                    return "stopped", None
                continue

            return "error", str(e)

        except Exception as e:
            return "error", str(e)

    return "error", "Unknown error"


def build_summary_embed(list_name, processed, valid, invalid, errors, added, removed, stopped, updated_at):
    embed = discord.Embed(
        title=f"{'Check Stopped' if stopped else 'Check Finished'}: {list_name}",
        color=discord.Color.orange() if stopped else discord.Color.green()
    )

    embed.add_field(name="Processed", value=str(processed), inline=True)
    embed.add_field(name="Valid", value=str(valid), inline=True)
    embed.add_field(name="Invalid", value=str(invalid), inline=True)
    embed.add_field(name="Errors", value=str(errors), inline=True)
    embed.add_field(name="Added Invalid", value=str(added), inline=True)
    embed.add_field(name="Removed Invalid", value=str(removed), inline=True)
    embed.add_field(name="List Last Updated", value=format_time(updated_at), inline=False)

    embed.set_footer(text="Vanity checker")

    return embed


async def run_list_check(list_name, list_data, manual_ctx=None):
    claim_channel = await get_channel(list_data["claim_channel_id"])
    log_channel = await get_channel(list_data["log_channel_id"])
    summary_channel = await get_channel(list_data["summary_channel_id"])

    ping_role_id = list_data.get("ping_role_id")
    words = list_data.get("words", [])

    if not claim_channel or not log_channel or not summary_channel:
        if manual_ctx:
            await manual_ctx.send(f"`{list_name}` has a missing/broken channel setup.")
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

    valid_count = 0
    invalid_count = 0
    error_count = 0
    added_count = 0
    removed_count = 0
    valid_found = []

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
            length = len(code)

            if result == "stopped":
                break

            if result == "valid":
                valid_count += 1
                valid_found.append(code)

                if code in invalid_cache[length]:
                    if remove_invalid(code):
                        removed_count += 1
                        await safe_send(
                            log_channel,
                            f"`discord.gg/{code}` became valid again and was removed from invalid files."
                        )

                await safe_send(claim_channel, f"discord.gg/{code}")

            elif result == "invalid":
                invalid_count += 1

                was_added = add_invalid(code)

                if was_added:
                    added_count += 1
                    await safe_send(log_channel, f"{length} letters | Invalid: `discord.gg/{code}`")

            else:
                error_count += 1
                await safe_send(log_channel, f"Error checking `discord.gg/{code}`: `{payload}`")

            if status_msg and (index == 1 or index % 10 == 0 or index == len(cleaned_codes)):
                try:
                    await status_msg.edit(
                        content=(
                            f"Checking `{list_name}`...\n"
                            f"Progress: `{index}/{len(cleaned_codes)}`\n"
                            f"Valid: `{valid_count}` | Invalid: `{invalid_count}` | Errors: `{error_count}`"
                        )
                    )
                except Exception:
                    pass

            if index < len(cleaned_codes):
                stopped = await sleep_with_stop(DELAY_SECONDS)
                if stopped:
                    break

    finally:
        stopped = check_state["stop_requested"]

        embed = build_summary_embed(
            list_name=list_name,
            processed=check_state["current"],
            valid=valid_count,
            invalid=invalid_count,
            errors=error_count,
            added=added_count,
            removed=removed_count,
            stopped=stopped,
            updated_at=list_data.get("updated_at")
        )

        ping_text = f"<@&{ping_role_id}> " if ping_role_id else ""

        await safe_send(summary_channel, content=ping_text, embed=embed)

        if valid_found:
            valid_file = DATA_DIR / f"{list_name}_valid_found.txt"

            with open(valid_file, "w", encoding="utf-8") as f:
                for code in valid_found:
                    f.write(f"discord.gg/{code}\n")

            await safe_send(
                claim_channel,
                content=f"Valid words found for `{list_name}`:",
                file=discord.File(str(valid_file), filename=valid_file.name)
            )

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

    load_invalid_cache()

    if not config["lists"]:
        if manual_ctx:
            await manual_ctx.send("No saved lists found.")
        return

    for list_name, list_data in config["lists"].items():
        await run_list_check(list_name, list_data, manual_ctx=manual_ctx)
        await asyncio.sleep(3)


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

    if not check_state["running"]:
        await run_all_checks()


@bot.command(name="help")
async def help_command(ctx):
    embed = discord.Embed(
        title="Vanity Checker Help",
        description="Multi-list vanity checker with claim, log, and summary channels.",
        color=discord.Color.blurple()
    )

    embed.add_field(
        name="Setup",
        value=(
            "`!addlist <name> <claim_channel> <log_channel> <summary_channel> <ping_role> <words>`\n\n"
            "Example:\n"
            "`!addlist 4letters #claims #vanity-logs #summaries @Hunters love, hate, void, glow`"
        ),
        inline=False
    )

    embed.add_field(
        name="Manage Lists",
        value=(
            "`!lists`\n"
            "`!listinfo <name>`\n"
            "`!removelist <name>`\n"
            "`!clearlists`"
        ),
        inline=False
    )

    embed.add_field(
        name="Checks",
        value=(
            "`!checklist <name>`\n"
            "`!checkall`\n"
            "`!stop`"
        ),
        inline=False
    )

    embed.add_field(
        name="Auto Checks",
        value=(
            "`!autocheck <minutes>`\n"
            "`!autostop`\n"
            "`!autostatus`"
        ),
        inline=False
    )

    await ctx.send(embed=embed)


@bot.command()
@commands.has_permissions(administrator=True)
async def addlist(
    ctx,
    name: str,
    claim_channel: discord.TextChannel,
    log_channel: discord.TextChannel,
    summary_channel: discord.TextChannel,
    ping_role: discord.Role,
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
        "log_channel_id": log_channel.id,
        "summary_channel_id": summary_channel.id,
        "ping_role_id": ping_role.id,
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
        f"Ping role: {ping_role.mention}\n"
        f"Updated: {format_time(timestamp)}"
    )


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
                f"Ping: <@&{data.get('ping_role_id')}>\n"
                f"Updated: {format_time(data.get('updated_at'))}"
            ),
            inline=False
        )

    await ctx.send(embed=embed)


@bot.command()
async def listinfo(ctx, name: str):
    name = name.lower()

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
    embed.add_field(name="Ping Role", value=f"<@&{data.get('ping_role_id')}>", inline=True)

    preview = ", ".join(data.get("words", [])[:25])
    if len(data.get("words", [])) > 25:
        preview += "..."

    embed.add_field(name="Word Preview", value=preview or "None", inline=False)

    await ctx.send(embed=embed)


@bot.command()
@commands.has_permissions(administrator=True)
async def removelist(ctx, name: str):
    name = name.lower()

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
    name = name.lower()

    if name not in config["lists"]:
        await ctx.send(f"No list named `{name}` exists.")
        return

    await ctx.send(f"Starting check for `{name}`.")
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

    await ctx.send(
        f"Stop requested for `{check_state['mode']}`. "
        f"Progress: `{check_state['current']}/{check_state['total']}`"
    )


@bot.command()
@commands.has_permissions(administrator=True)
async def autocheck(ctx, minutes: int):
    if minutes < 5:
        await ctx.send("Use at least `5` minutes to avoid rate limits.")
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
        f"Currently running: `{'Yes' if check_state['running'] else 'No'}`"
    )


@bot.command()
async def invalidcount(ctx, length: int):
    if length < 1 or length > 32:
        await ctx.send("Use a length between 1 and 32.")
        return

    load_invalid_cache()
    await ctx.send(f"{length}-letter invalid count: `{len(invalid_cache[length])}`")


@bot.command()
async def getinvalid(ctx, length: int):
    if length < 1 or length > 32:
        await ctx.send("Use a length between 1 and 32.")
        return

    load_invalid_cache()
    rewrite_invalid_file(length)

    path = invalid_file(length)

    await ctx.send(
        content=f"Invalid file for `{length}` letters:",
        file=discord.File(str(path), filename=path.name)
    )


@bot.command()
@commands.has_permissions(administrator=True)
async def clearinvalid(ctx, length: int = None):
    load_invalid_cache()

    if length is None:
        for l in TRACKED_LENGTHS:
            invalid_cache[l].clear()
            rewrite_invalid_file(l)

        await ctx.send("Cleared all invalid files.")
        return

    if length < 1 or length > 32:
        await ctx.send("Use a length between 1 and 32.")
        return

    invalid_cache[length].clear()
    rewrite_invalid_file(length)

    await ctx.send(f"Cleared invalid file for `{length}` letters.")


@bot.command()
@commands.has_permissions(manage_messages=True)
async def purge(ctx, amount: int):
    deleted = await ctx.channel.purge(limit=amount + 1)
    msg = await ctx.send(f"Deleted `{len(deleted) - 1}` messages.")
    await asyncio.sleep(3)
    await msg.delete()


@bot.event
async def on_ready():
    ensure_dirs()
    ensure_invalid_files()
    load_config()
    load_invalid_cache()

    if not auto_check_loop.is_running():
        auto_check_loop.start()

    logger.info("Logged in as %s", bot.user)


if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing from environment variables.")

bot.run(TOKEN)
import os
import json
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("BOT_PREFIX", "!")

WORD_DELAY_SECONDS = float(os.getenv("WORD_DELAY_SECONDS", "3"))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "10"))
BATCH_COOLDOWN_SECONDS = float(os.getenv("BATCH_COOLDOWN_SECONDS", "10"))
LIST_COOLDOWN_SECONDS = float(os.getenv("LIST_COOLDOWN_SECONDS", "60"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "2"))
BACKOFF_SECONDS = float(os.getenv("BACKOFF_SECONDS", "30"))
MAX_CODES_PER_LIST = int(os.getenv("MAX_CODES_PER_LIST", "2000"))
MIN_AUTO_MINUTES = int(os.getenv("MIN_AUTO_MINUTES", "5"))

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
CONFIG_FILE = DATA_DIR / "vanity_config.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("vanity_checker")

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)
check_lock = asyncio.Lock()

check_state = {"running": False, "stop_requested": False, "current": 0, "total": 0, "list": None}
config = {"auto_enabled": False, "auto_minutes": 60, "lists": {}}


def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


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
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        config["auto_enabled"] = loaded.get("auto_enabled", False)
        config["auto_minutes"] = loaded.get("auto_minutes", 60)
        config["lists"] = loaded.get("lists", {})
    except Exception:
        logger.exception("Could not load config. Backing up broken config and starting clean.")
        try:
            CONFIG_FILE.rename(DATA_DIR / "vanity_config_broken.json")
        except Exception:
            pass
        config["auto_enabled"] = False
        config["auto_minutes"] = 60
        config["lists"] = {}
        save_config()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def utc_now():
    return datetime.now(timezone.utc)


def format_time(iso_time):
    if not iso_time:
        return "Unknown"
    try:
        dt = datetime.fromisoformat(iso_time)
        unix = int(dt.timestamp())
        return f"<t:{unix}:F> • <t:{unix}:R>"
    except Exception:
        return "Unknown"


def format_duration(seconds):
    seconds = int(seconds)
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


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
    for item in words.replace("\n", ",").split(","):
        code = clean_code(item)
        if code and code not in seen:
            seen.add(code)
            cleaned.append(code)
    return cleaned


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
    except discord.HTTPException as e:
        logger.warning("Discord send HTTP error: %s", e)
        await asyncio.sleep(3)
        try:
            return await channel.send(content=content, embed=embed, file=file)
        except Exception as retry_error:
            logger.warning("Retry send failed: %s", retry_error)
            return None
    except Exception as e:
        logger.warning("Failed to send message: %s", e)
        return None


def build_long_text_chunks(text, limit=3900):
    chunks = []
    current = ""
    for part in text.split(", "):
        extra = part if not current else ", " + part
        if len(current) + len(extra) > limit:
            if current:
                chunks.append(current)
            current = part
        else:
            current += extra
    if current:
        chunks.append(current)
    return chunks


async def sleep_with_stop(seconds):
    waited = 0.0
    while waited < seconds:
        if check_state["stop_requested"]:
            return True
        step = min(0.5, seconds - waited)
        await asyncio.sleep(step)
        waited += step
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
            return "error", f"Forbidden: {e}"
        except discord.HTTPException as e:
            logger.warning("HTTP error for %s attempt %s/%s: %s", code, attempt, MAX_RETRIES, e)
            if attempt < MAX_RETRIES:
                stopped = await sleep_with_stop(BACKOFF_SECONDS)
                if stopped:
                    return "stopped", None
                continue
            return "error", f"HTTPException: {e}"
        except Exception as e:
            logger.exception("Unexpected error checking %s", code)
            if attempt < MAX_RETRIES:
                stopped = await sleep_with_stop(BACKOFF_SECONDS)
                if stopped:
                    return "stopped", None
                continue
            return "error", f"{type(e).__name__}: {e}"
    return "error", "Unknown error"


def list_ready(data):
    needed = ["valid_channel_id", "invalid_channel_id", "summary_channel_id", "log_channel_id", "words"]
    for key in needed:
        if key not in data or data[key] in (None, "", []):
            return False, f"Missing `{key}`"
    return True, "Ready"


async def send_summary_lists(summary_channel, valid_words, invalid_words):
    if valid_words:
        for i, chunk in enumerate(build_long_text_chunks(", ".join(valid_words)), start=1):
            embed = discord.Embed(title=f"Valid / On Server Words{f' Part {i}' if i > 1 else ''}", description=f"```txt\n{chunk}\n```", color=discord.Color.blurple())
            await safe_send(summary_channel, embed=embed)
    else:
        await safe_send(summary_channel, "No valid / on-server words found.")

    if invalid_words:
        for i, chunk in enumerate(build_long_text_chunks(", ".join(invalid_words)), start=1):
            embed = discord.Embed(title=f"Invalid / Not On Server Words{f' Part {i}' if i > 1 else ''}", description=f"```txt\n{chunk}\n```", color=discord.Color.dark_gray())
            await safe_send(summary_channel, embed=embed)
    else:
        await safe_send(summary_channel, "No invalid / not-on-server words found.")


async def run_list_unlocked(list_name, list_data, ctx=None):
    ready, reason = list_ready(list_data)
    if not ready:
        if ctx:
            await ctx.send(f"`{list_name}` is not ready. {reason}. Use `{PREFIX}status {list_name}`.")
        return

    valid_channel = await get_channel_safe(list_data["valid_channel_id"])
    invalid_channel = await get_channel_safe(list_data["invalid_channel_id"])
    summary_channel = await get_channel_safe(list_data["summary_channel_id"])
    log_channel = await get_channel_safe(list_data["log_channel_id"])

    if not valid_channel or not invalid_channel or not summary_channel or not log_channel:
        if ctx:
            await ctx.send(f"`{list_name}` has a channel I cannot access. Check bot permissions.")
        return

    codes = []
    seen = set()
    for word in list_data.get("words", []):
        code = clean_code(word)
        if code and code not in seen:
            seen.add(code)
            codes.append(code)

    if len(codes) > MAX_CODES_PER_LIST:
        codes = codes[:MAX_CODES_PER_LIST]
    if not codes:
        await safe_send(log_channel, f"`{list_name}` has no usable words.")
        return

    check_state.update({"running": True, "stop_requested": False, "current": 0, "total": len(codes), "list": list_name})
    valid_words, invalid_words, error_words = [], [], []
    start_dt = utc_now()
    started_at = start_dt.isoformat()

    await safe_send(log_channel, f"🔍 Started `{list_name}` with `{len(codes)}` invite(s).\nTiming: `{WORD_DELAY_SECONDS}s` between each word, `{BATCH_COOLDOWN_SECONDS}s` after every `{BATCH_SIZE}` words.")

    try:
        for index, code in enumerate(codes, start=1):
            if check_state["stop_requested"]:
                break
            check_state["current"] = index
            result, payload = await fetch_invite_status(code)
            if result == "stopped":
                break
            if result == "valid":
                valid_words.append(code)
                await safe_send(valid_channel, f"discord.gg/{code}")
            elif result == "invalid":
                invalid_words.append(code)
                await safe_send(invalid_channel, f"discord.gg/{code}")
            else:
                error_words.append(code)
                await safe_send(log_channel, f"⚠️ Error checking `{code}`: `{payload}`")

            if index < len(codes) and index % BATCH_SIZE != 0:
                if await sleep_with_stop(WORD_DELAY_SECONDS):
                    break
            if index % BATCH_SIZE == 0 and index < len(codes):
                await safe_send(log_channel, f"Progress `{index}/{len(codes)}` | Valid: `{len(valid_words)}` | Invalid: `{len(invalid_words)}` | Errors: `{len(error_words)}`\nBatch cooldown: waiting `{BATCH_COOLDOWN_SECONDS}` seconds...")
                if await sleep_with_stop(BATCH_COOLDOWN_SECONDS):
                    break
    except Exception as e:
        logger.exception("Unexpected failure in list %s", list_name)
        await safe_send(log_channel, f"⚠️ `{list_name}` had an unexpected error, but the bot is still running: `{type(e).__name__}: {e}`")
    finally:
        end_dt = utc_now()
        elapsed = format_duration((end_dt - start_dt).total_seconds())
        stopped = check_state["stop_requested"]
        summary_embed = discord.Embed(title=f"{'Stopped' if stopped else 'Done'} Checking: {list_name}", description=f"Check completed in **{elapsed}**.", color=discord.Color.orange() if stopped else discord.Color.green())
        summary_embed.add_field(name="Processed", value=f"{check_state['current']}/{len(codes)}", inline=True)
        summary_embed.add_field(name="Valid / On Server", value=str(len(valid_words)), inline=True)
        summary_embed.add_field(name="Invalid / Not On Server", value=str(len(invalid_words)), inline=True)
        summary_embed.add_field(name="Errors", value=str(len(error_words)), inline=True)
        summary_embed.add_field(name="Started", value=format_time(started_at), inline=False)
        summary_embed.add_field(name="Finished", value=format_time(end_dt.isoformat()), inline=False)
        summary_embed.add_field(name="List Last Updated", value=format_time(list_data.get("updated_at")), inline=False)
        summary_embed.set_footer(text="Valid = on a server • Invalid = not on a server")
        await safe_send(summary_channel, embed=summary_embed)
        await send_summary_lists(summary_channel, valid_words, invalid_words)
        await safe_send(log_channel, f"✅ `{list_name}` check is done.\nTime taken: `{elapsed}`\nProcessed: `{check_state['current']}/{len(codes)}` | Valid: `{len(valid_words)}` | Invalid: `{len(invalid_words)}` | Errors: `{len(error_words)}`")
        check_state.update({"running": False, "stop_requested": False, "current": 0, "total": 0, "list": None})


async def run_one_list(name, ctx=None):
    if check_lock.locked():
        if ctx:
            await ctx.send("List is already running, please wait.")
        return
    name = name.lower()
    if name not in config["lists"]:
        if ctx:
            await ctx.send(f"No list named `{name}` exists.")
        return
    async with check_lock:
        await run_list_unlocked(name, config["lists"][name], ctx=ctx)


async def run_all_lists(ctx=None):
    if check_lock.locked():
        if ctx:
            await ctx.send("List is already running, please wait.")
        return
    async with check_lock:
        if not config["lists"]:
            if ctx:
                await ctx.send("No saved lists found.")
            return
        items = list(config["lists"].items())
        for index, (name, data) in enumerate(items, start=1):
            if check_state["stop_requested"]:
                break
            try:
                await run_list_unlocked(name, data, ctx=ctx)
            except Exception as e:
                logger.exception("Failed running list %s", name)
                log_channel = await get_channel_safe(data.get("log_channel_id"))
                await safe_send(log_channel, f"⚠️ `{name}` failed but the bot continued: `{type(e).__name__}: {e}`")
            if index < len(items):
                next_name = items[index][0]
                log_channel = await get_channel_safe(data.get("log_channel_id"))
                await safe_send(log_channel, f"⏳ Waiting `{format_duration(LIST_COOLDOWN_SECONDS)}` before checking `{next_name}`...")
                if await sleep_with_stop(LIST_COOLDOWN_SECONDS):
                    break


@tasks.loop(minutes=1)
async def auto_loop():
    if not config.get("auto_enabled", False):
        return
    minutes = int(config.get("auto_minutes", 60))
    if not hasattr(auto_loop, "counter"):
        auto_loop.counter = 0
    auto_loop.counter += 1
    if auto_loop.counter < minutes:
        return
    auto_loop.counter = 0
    if check_lock.locked():
        return
    await run_all_lists()


@bot.command(name="help")
async def help_command(ctx):
    embed = discord.Embed(title="Vanity Bot Help", description=(f"`#valid` = invites that are on a server\n`#invalid` = invites that are not on a server\n`#summary` = final summary + comma-separated embeds\n`#log` = progress, cooldowns, errors\n\nTiming: `{WORD_DELAY_SECONDS}s` between words, `{BATCH_COOLDOWN_SECONDS}s` every `{BATCH_SIZE}` words, `{format_duration(LIST_COOLDOWN_SECONDS)}` between lists."), color=discord.Color.blurple())
    embed.add_field(name="Fast Setup", value=f"`{PREFIX}setup <list_name> #valid #invalid #summary #log <words>`\n\nExample:\n`{PREFIX}setup 3letters #valid #invalid #summary #log abc, lol, pmo, vip`", inline=False)
    embed.add_field(name="Run Checks", value=f"`{PREFIX}run <list_name>`\n`{PREFIX}runall`\n`{PREFIX}stop`", inline=False)
    embed.add_field(name="Manage Lists", value=f"`{PREFIX}lists`\n`{PREFIX}status <list_name>`\n`{PREFIX}words <list_name> <words>`\n`{PREFIX}append <list_name> <words>`\n`{PREFIX}remove_list <list_name>`", inline=False)
    embed.add_field(name="Auto Checks", value=f"`{PREFIX}autocheck <minutes>`\n`{PREFIX}autostop`\n`{PREFIX}autostatus`", inline=False)
    await ctx.send(embed=embed)


@bot.command()
@commands.has_permissions(administrator=True)
async def setup(ctx, name: str, valid_channel: discord.TextChannel, invalid_channel: discord.TextChannel, summary_channel: discord.TextChannel, log_channel: discord.TextChannel, *, words: str):
    cleaned = parse_words(words)
    if not cleaned:
        await ctx.send("No usable words found.")
        return
    if len(cleaned) > MAX_CODES_PER_LIST:
        await ctx.send(f"Too many words. Max is `{MAX_CODES_PER_LIST}`.")
        return
    name = name.lower()
    timestamp = now_iso()
    old_created = config["lists"].get(name, {}).get("created_at", timestamp)
    config["lists"][name] = {"valid_channel_id": valid_channel.id, "invalid_channel_id": invalid_channel.id, "summary_channel_id": summary_channel.id, "log_channel_id": log_channel.id, "words": cleaned, "created_at": old_created, "updated_at": timestamp}
    save_config()
    await ctx.send(f"✅ Setup saved for `{name}` with `{len(cleaned)}` word(s).\nValid / on-server: {valid_channel.mention}\nInvalid / not-on-server: {invalid_channel.mention}\nSummary: {summary_channel.mention}\nLogs: {log_channel.mention}")


@bot.command(name="run")
@commands.has_permissions(administrator=True)
async def run_command(ctx, name: str):
    await run_one_list(name, ctx=ctx)


@bot.command()
@commands.has_permissions(administrator=True)
async def runall(ctx):
    await run_all_lists(ctx=ctx)


@bot.command()
async def stop(ctx):
    if not check_state["running"]:
        await ctx.send("No list is currently running.")
        return
    check_state["stop_requested"] = True
    await ctx.send(f"Stop requested for `{check_state['list']}`. Progress: `{check_state['current']}/{check_state['total']}`.")


@bot.command()
async def lists(ctx):
    if not config["lists"]:
        await ctx.send("No saved lists yet.")
        return
    embed = discord.Embed(title="Saved Lists", color=discord.Color.blurple())
    for name, data in config["lists"].items():
        ready, reason = list_ready(data)
        embed.add_field(name=name, value=f"Status: `{'Ready' if ready else reason}`\nWords: `{len(data.get('words', []))}`\nValid: <#{data.get('valid_channel_id')}>\nInvalid: <#{data.get('invalid_channel_id')}>\nSummary: <#{data.get('summary_channel_id')}>\nLog: <#{data.get('log_channel_id')}>\nUpdated: {format_time(data.get('updated_at'))}", inline=False)
    await ctx.send(embed=embed)


@bot.command()
async def status(ctx, name: str):
    name = name.lower()
    if name not in config["lists"]:
        await ctx.send(f"No list named `{name}` exists.")
        return
    data = config["lists"][name]
    ready, reason = list_ready(data)
    embed = discord.Embed(title=f"Status: {name}", color=discord.Color.green() if ready else discord.Color.orange())
    embed.add_field(name="Ready", value="Yes" if ready else reason, inline=False)
    embed.add_field(name="Words", value=str(len(data.get("words", []))), inline=True)
    embed.add_field(name="Valid Channel", value=f"<#{data.get('valid_channel_id')}>", inline=True)
    embed.add_field(name="Invalid Channel", value=f"<#{data.get('invalid_channel_id')}>", inline=True)
    embed.add_field(name="Summary Channel", value=f"<#{data.get('summary_channel_id')}>", inline=True)
    embed.add_field(name="Log Channel", value=f"<#{data.get('log_channel_id')}>", inline=True)
    embed.add_field(name="Created", value=format_time(data.get("created_at")), inline=False)
    embed.add_field(name="Updated", value=format_time(data.get("updated_at")), inline=False)
    preview = ", ".join(data.get("words", [])[:30])
    if len(data.get("words", [])) > 30:
        preview += "..."
    embed.add_field(name="Word Preview", value=preview or "None", inline=False)
    await ctx.send(embed=embed)


@bot.command()
@commands.has_permissions(administrator=True)
async def words(ctx, name: str, *, words: str):
    name = name.lower()
    if name not in config["lists"]:
        await ctx.send(f"No list named `{name}` exists.")
        return
    cleaned = parse_words(words)
    if not cleaned:
        await ctx.send("No usable words found.")
        return
    if len(cleaned) > MAX_CODES_PER_LIST:
        await ctx.send(f"Too many words. Max is `{MAX_CODES_PER_LIST}`.")
        return
    config["lists"][name]["words"] = cleaned
    config["lists"][name]["updated_at"] = now_iso()
    save_config()
    await ctx.send(f"✅ Replaced `{name}` with `{len(cleaned)}` word(s).")


@bot.command()
@commands.has_permissions(administrator=True)
async def append(ctx, name: str, *, words: str):
    name = name.lower()
    if name not in config["lists"]:
        await ctx.send(f"No list named `{name}` exists.")
        return
    current = config["lists"][name].get("words", [])
    current_set = set(current)
    cleaned = parse_words(words)
    added = []
    for word in cleaned:
        if word not in current_set:
            current.append(word)
            current_set.add(word)
            added.append(word)
    if len(current) > MAX_CODES_PER_LIST:
        await ctx.send(f"This would exceed the max of `{MAX_CODES_PER_LIST}` words.")
        return
    config["lists"][name]["words"] = current
    config["lists"][name]["updated_at"] = now_iso()
    save_config()
    await ctx.send(f"✅ Added `{len(added)}` new word(s) to `{name}`.")


@bot.command()
@commands.has_permissions(administrator=True)
async def remove_list(ctx, name: str):
    name = name.lower()
    if name not in config["lists"]:
        await ctx.send(f"No list named `{name}` exists.")
        return
    del config["lists"][name]
    save_config()
    await ctx.send(f"✅ Removed `{name}`.")


@bot.command()
@commands.has_permissions(administrator=True)
async def autocheck(ctx, minutes: int):
    if minutes < MIN_AUTO_MINUTES:
        await ctx.send(f"Use at least `{MIN_AUTO_MINUTES}` minutes.")
        return
    config["auto_enabled"] = True
    config["auto_minutes"] = minutes
    save_config()
    auto_loop.counter = 0
    await ctx.send(f"✅ Auto checks enabled every `{minutes}` minute(s).\nLists run one at a time with a `{format_duration(LIST_COOLDOWN_SECONDS)}` cooldown between lists.")


@bot.command()
@commands.has_permissions(administrator=True)
async def autostop(ctx):
    config["auto_enabled"] = False
    save_config()
    await ctx.send("✅ Auto checks disabled.")


@bot.command()
async def autostatus(ctx):
    await ctx.send(f"Auto checks: `{'Enabled' if config['auto_enabled'] else 'Disabled'}`\nInterval: `{config['auto_minutes']}` minute(s)\nSaved lists: `{len(config['lists'])}`\nRunning: `{'Yes' if check_state['running'] else 'No'}`\nLocked: `{'Yes' if check_lock.locked() else 'No'}`\nWord delay: `{WORD_DELAY_SECONDS}s`\nBatch cooldown: `{BATCH_COOLDOWN_SECONDS}s every {BATCH_SIZE} words`\nList cooldown: `{format_duration(LIST_COOLDOWN_SECONDS)}`")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You need administrator permission to use that.")
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing something. Use `{PREFIX}help` for syntax.")
        return
    if isinstance(error, commands.BadArgument):
        await ctx.send(f"Bad format. Make sure you mention channels like `#valid`. Use `{PREFIX}help`.")
        return
    logger.exception("Command error: %s", error)
    await ctx.send("An error happened, but the bot is still running. Check logs or use `!help`.")


@bot.event
async def on_ready():
    ensure_dirs()
    load_config()
    if not auto_loop.is_running():
        auto_loop.start()
    logger.info("Logged in as %s", bot.user)
    logger.info("Saved lists: %s", len(config["lists"]))
    logger.info("Timing: word=%ss batch=%ss list=%ss", WORD_DELAY_SECONDS, BATCH_COOLDOWN_SECONDS, LIST_COOLDOWN_SECONDS)


if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing. Add it to Railway variables.")

bot.run(TOKEN)

import os
import asyncio
import logging
from pathlib import Path
from collections import defaultdict

import discord
from discord.ext import commands

# =========================
# CONFIG
# =========================
TOKEN = os.getenv("DISCORD_TOKEN")

SEND_CHANNEL_ID = 1491717657567690802
INVALID_LOG_CHANNEL_ID = 1491717674349367386

DELAY_SECONDS = 3
MAX_CODES_PER_RUN = 1000
BACKOFF_SECONDS = 60
MAX_RETRIES = 2

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data" / "invalid_vanities"
TRACKED_LENGTHS = range(1, 33)

# =========================
# LOGGING
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("vanity_checker")

# =========================
# BOT SETUP
# =========================
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None
)

# =========================
# RUNTIME STATE
# =========================
invalid_cache = defaultdict(set)

check_state = {
    "running": False,
    "stop_requested": False,
    "started_by": None,
    "total": 0,
    "current": 0,
}

# =========================
# FILE HELPERS
# =========================
def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def get_invalid_file_path(length: int) -> Path:
    ensure_data_dir()
    return DATA_DIR / f"invalid_{length}_letters.txt"


def ensure_invalid_file(length: int) -> Path:
    file_path = get_invalid_file_path(length)
    if not file_path.exists():
        file_path.touch(exist_ok=True)
        logger.info("Created missing file: %s", file_path)
    return file_path


def ensure_all_invalid_files() -> None:
    ensure_data_dir()
    for length in TRACKED_LENGTHS:
        ensure_invalid_file(length)


def clean_invite_code(item: str) -> str:
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
    )


def normalize_code(code: str) -> str:
    return clean_invite_code(code).strip().lower()


def load_invalid_cache() -> None:
    invalid_cache.clear()
    ensure_all_invalid_files()

    for length in TRACKED_LENGTHS:
        file_path = ensure_invalid_file(length)
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    code = normalize_code(line)
                    if code and len(code) == length:
                        invalid_cache[length].add(code)
        except Exception as e:
            logger.exception("Failed to read %s: %s", file_path, e)

    logger.info("Loaded invalid cache from files.")


def rewrite_invalid_file(length: int) -> bool:
    try:
        file_path = ensure_invalid_file(length)
        codes = sorted(invalid_cache[length])

        with open(file_path, "w", encoding="utf-8") as f:
            for code in codes:
                f.write(f"{code}\n")

        logger.info("Rewrote file %s with %s codes.", file_path, len(codes))
        return True
    except Exception as e:
        logger.exception("Failed rewriting invalid file for %s letters: %s", length, e)
        return False


def rewrite_all_invalid_files() -> None:
    ensure_all_invalid_files()
    for length in TRACKED_LENGTHS:
        rewrite_invalid_file(length)


# =========================
# CHECK STATE HELPERS
# =========================
def reset_check_state() -> None:
    check_state["running"] = False
    check_state["stop_requested"] = False
    check_state["started_by"] = None
    check_state["total"] = 0
    check_state["current"] = 0


def start_check_state(user_id: int, total: int) -> None:
    check_state["running"] = True
    check_state["stop_requested"] = False
    check_state["started_by"] = user_id
    check_state["total"] = total
    check_state["current"] = 0


def request_stop() -> bool:
    if not check_state["running"]:
        return False
    check_state["stop_requested"] = True
    return True


async def sleep_with_stop(seconds: float, chunk: float = 0.5) -> bool:
    remaining = max(0.0, float(seconds))
    while remaining > 0:
        if check_state["stop_requested"]:
            return True
        step = min(chunk, remaining)
        await asyncio.sleep(step)
        remaining -= step
    return check_state["stop_requested"]


# =========================
# DISCORD HELPERS
# =========================
async def get_channel_safe(channel_id: int):
    channel = bot.get_channel(channel_id)
    if channel is not None:
        return channel

    try:
        return await bot.fetch_channel(channel_id)
    except Exception as e:
        logger.exception("Failed to get channel %s: %s", channel_id, e)
        return None


async def safe_fetch_invite(invite_code: str):
    """
    Returns:
        ("valid", invite)
        ("invalid", None)
        ("temporary_error", error_text)
        ("fatal_error", error_text)
        ("stopped", None)
    """
    for attempt in range(1, MAX_RETRIES + 1):
        if check_state["stop_requested"]:
            return "stopped", None

        try:
            invite = await bot.fetch_invite(invite_code)
            return "valid", invite

        except discord.NotFound:
            return "invalid", None

        except discord.Forbidden as e:
            return "fatal_error", f"Forbidden: {e}"

        except discord.HTTPException as e:
            logger.warning(
                "HTTPException on %s (attempt %s/%s): %s",
                invite_code, attempt, MAX_RETRIES, e
            )
            if attempt < MAX_RETRIES:
                stopped = await sleep_with_stop(BACKOFF_SECONDS * attempt)
                if stopped:
                    return "stopped", None
                continue
            return "temporary_error", f"HTTPException: {e}"

        except Exception as e:
            logger.exception("Unexpected error on %s: %s", invite_code, e)
            if attempt < MAX_RETRIES:
                stopped = await sleep_with_stop(BACKOFF_SECONDS * attempt)
                if stopped:
                    return "stopped", None
                continue
            return "temporary_error", f"{type(e).__name__}: {e}"

    return "temporary_error", "Unknown error"


def build_help_embed(prefix: str = "!") -> discord.Embed:
    embed = discord.Embed(
        title="Vanity Checker Help",
        description=(
            "This bot checks Discord vanity invite codes, sends valid ones to your valid log channel, "
            "sends invalid ones to your invalid log channel, and stores invalid ones in txt files grouped by code length.\n\n"
            "It also lets you clear invalid files and rebuild them from scratch whenever you want."
        ),
        color=discord.Color.blurple()
    )

    embed.add_field(
        name=f"{prefix}sendcodes <codes>",
        value=(
            "Checks a comma-separated list of vanity codes or invite links.\n\n"
            "**Example:**\n"
            f"`{prefix}sendcodes abc, test, discord.gg/cool, https://discord.gg/name`\n\n"
            "**What it does:**\n"
            "• Cleans input into plain invite codes\n"
            "• Checks each code slowly to avoid rate limits\n"
            "• Sends valid codes to the valid log channel\n"
            "• Sends invalid codes to the invalid log channel\n"
            "• Rebuilds invalid txt files for the lengths in that run\n"
            "• Removes codes that are no longer invalid\n"
            "• Prevents duplicate invalid entries in the file\n"
            "• Refuses to start if another scan is already running"
        ),
        inline=False
    )

    embed.add_field(
        name=f"{prefix}clearinvalid [length]",
        value=(
            "Clears invalid txt files so you can rebuild them from scratch.\n\n"
            f"**Examples:**\n"
            f"`{prefix}clearinvalid` → clears all invalid files\n"
            f"`{prefix}clearinvalid 4` → clears only the 4-letter invalid file\n\n"
            "After clearing, run `sendcodes` again to repopulate the file with fresh invalid codes."
        ),
        inline=False
    )

    embed.add_field(
        name=f"{prefix}stop",
        value=(
            "Stops the current running `sendcodes` scan safely.\n\n"
            "The bot keeps completed progress and still rewrites synced files cleanly."
        ),
        inline=False
    )

    embed.add_field(
        name=f"{prefix}getinvalid <length>",
        value=(
            "Sends the invalid txt file for a specific code length directly in Discord.\n\n"
            f"Examples: `{prefix}getinvalid 3`, `{prefix}getinvalid 4`, `{prefix}getinvalid 5`"
        ),
        inline=False
    )

    embed.add_field(
        name=f"{prefix}invalidfiles",
        value="Shows where the invalid txt files are stored and lists the files currently available.",
        inline=False
    )

    embed.add_field(
        name=f"{prefix}remakeinvalidfiles",
        value="Recreates any missing invalid txt files and rewrites them from cache.",
        inline=False
    )

    embed.add_field(
        name=f"{prefix}purge <amount>",
        value=f"Deletes messages from the current channel. Example: `{prefix}purge 50`",
        inline=False
    )

    embed.set_footer(
        text="Tip: invalid files are stored in data/invalid_vanities next to your bot file."
    )
    return embed


# =========================
# EVENTS
# =========================
@bot.event
async def on_ready():
    ensure_all_invalid_files()
    load_invalid_cache()
    logger.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    logger.info("Storage folder: %s", DATA_DIR)
    logger.info("Bot is ready.")


# =========================
# COMMANDS
# =========================
@bot.command(name="help")
async def help_command(ctx):
    embed = build_help_embed(bot.command_prefix)
    await ctx.send(embed=embed)


@bot.command()
async def stop(ctx):
    if not check_state["running"]:
        await ctx.send("There is no active check running right now.")
        return

    request_stop()
    await ctx.send(
        f"Stop requested. The current check will stop safely after the current step. "
        f"Progress: {check_state['current']}/{check_state['total']}."
    )


@bot.command()
async def clearinvalid(ctx, length: int = None):
    """
    !clearinvalid        -> clears all invalid files
    !clearinvalid 4      -> clears only invalid_4_letters.txt
    """
    if check_state["running"]:
        await ctx.send("You cannot clear invalid files while a check is running. Use `!stop` first.")
        return

    ensure_all_invalid_files()
    load_invalid_cache()

    if length is None:
        cleared = 0
        for file_length in TRACKED_LENGTHS:
            invalid_cache[file_length].clear()
            if rewrite_invalid_file(file_length):
                cleared += 1

        await ctx.send(f"Cleared all invalid files. ({cleared} file(s) reset)")
        return

    if length < 1 or length > 32:
        await ctx.send("Use a length between 1 and 32.")
        return

    invalid_cache[length].clear()
    ok = rewrite_invalid_file(length)

    if ok:
        await ctx.send(f"Cleared the invalid file for {length} letters.")
    else:
        await ctx.send(f"Failed to clear the invalid file for {length} letters.")


@bot.command()
async def sendcodes(ctx, *, words: str):
    if check_state["running"]:
        await ctx.send(
            f"A check is already running right now. Progress: "
            f"{check_state['current']}/{check_state['total']}. Use `!stop` if you want to stop it."
        )
        return

    items = [w.strip() for w in words.split(",") if w.strip()]

    if not items:
        await ctx.send("No codes found.")
        return

    if len(items) > MAX_CODES_PER_RUN:
        await ctx.send(f"Too many codes at once. Max per run is {MAX_CODES_PER_RUN}.")
        return

    ensure_all_invalid_files()
    load_invalid_cache()

    send_channel = await get_channel_safe(SEND_CHANNEL_ID)
    invalid_log_channel = await get_channel_safe(INVALID_LOG_CHANNEL_ID)

    if send_channel is None:
        await ctx.send("Could not access the valid invite channel.")
        return

    if invalid_log_channel is None:
        await ctx.send("Could not access the invalid invite channel.")
        return

    seen_input = set()
    cleaned_codes = []
    for item in items:
        code = normalize_code(item)
        if not code:
            continue
        if code in seen_input:
            continue
        seen_input.add(code)
        cleaned_codes.append(code)

    if not cleaned_codes:
        await ctx.send("No usable codes found after cleaning.")
        return

    affected_lengths = sorted({len(code) for code in cleaned_codes})

    old_invalid_by_length = {
        length: set(invalid_cache[length])
        for length in affected_lengths
    }

    checked_codes_by_length = defaultdict(set)
    new_invalid_by_length = defaultdict(set)

    valid_count = 0
    invalid_count = 0
    error_count = 0
    removed_from_invalid_count = 0

    start_check_state(ctx.author.id, len(cleaned_codes))
    status_msg = await ctx.send(
        f"Checking {len(cleaned_codes)} code(s) slowly to avoid rate limits..."
    )

    stopped_early = False

    try:
        for index, invite_code in enumerate(cleaned_codes, start=1):
            check_state["current"] = index

            if check_state["stop_requested"]:
                stopped_early = True
                break

            length = len(invite_code)
            checked_codes_by_length[length].add(invite_code)

            logger.info("[%s/%s] Checking: %s", index, len(cleaned_codes), invite_code)

            result, payload = await safe_fetch_invite(invite_code)

            if result == "stopped":
                stopped_early = True
                break

            if result == "valid":
                valid_count += 1

                try:
                    await send_channel.send(f"discord.gg/{invite_code}")
                except Exception as e:
                    logger.exception("Failed to send valid code %s: %s", invite_code, e)
                    error_count += 1

            elif result == "invalid":
                new_invalid_by_length[length].add(invite_code)
                invalid_count += 1

                try:
                    await invalid_log_channel.send(
                        f"{length} letters | Invalid: `discord.gg/{invite_code}`"
                    )
                except Exception as e:
                    logger.exception("Failed to log invalid %s: %s", invite_code, e)

            elif result == "temporary_error":
                error_count += 1
                logger.warning("Temporary error: %s | %s", invite_code, payload)

                try:
                    await invalid_log_channel.send(
                        f"{length} letters | Temporary error, skipped for sync: `discord.gg/{invite_code}`"
                    )
                except Exception as e:
                    logger.exception("Failed to log temp error %s: %s", invite_code, e)

                stopped = await sleep_with_stop(BACKOFF_SECONDS)
                if stopped:
                    stopped_early = True
                    break

            else:
                error_count += 1
                logger.error("Fatal error: %s | %s", invite_code, payload)

                try:
                    await invalid_log_channel.send(
                        f"{length} letters | Error, skipped for sync: `discord.gg/{invite_code}`"
                    )
                except Exception as e:
                    logger.exception("Failed to log fatal error %s: %s", invite_code, e)

            if index == 1 or index % 5 == 0 or index == len(cleaned_codes):
                try:
                    await status_msg.edit(
                        content=(
                            f"Progress: {index}/{len(cleaned_codes)}\n"
                            f"Valid: {valid_count} | Invalid: {invalid_count} | Errors: {error_count}\n"
                            f"Affected lengths: {', '.join(map(str, affected_lengths))}\n"
                            f"Stop requested: {'Yes' if check_state['stop_requested'] else 'No'}"
                        )
                    )
                except Exception:
                    pass

            if index < len(cleaned_codes):
                stopped = await sleep_with_stop(DELAY_SECONDS)
                if stopped:
                    stopped_early = True
                    break

    finally:
        added_to_invalid_count = 0
        synced_lengths = []

        for length in affected_lengths:
            old_set = old_invalid_by_length.get(length, set())
            checked_set = checked_codes_by_length.get(length, set())
            new_invalid_set = new_invalid_by_length.get(length, set())

            unchanged_old_entries = old_set - checked_set
            final_invalid_set = unchanged_old_entries | new_invalid_set

            removed_codes = old_set & checked_set & (old_set - new_invalid_set)
            added_codes = new_invalid_set - old_set

            invalid_cache[length] = final_invalid_set
            rewrite_invalid_file(length)

            removed_from_invalid_count += len(removed_codes)
            added_to_invalid_count += len(added_codes)
            synced_lengths.append(length)

            for code in sorted(removed_codes):
                try:
                    await invalid_log_channel.send(
                        f"{length} letters | Removed from invalid file because it is valid now: `discord.gg/{code}`"
                    )
                except Exception as e:
                    logger.exception("Failed to log removal of %s: %s", code, e)

        summary_lines = []
        if stopped_early or check_state["stop_requested"]:
            summary_lines.append("Check stopped early.")
        else:
            summary_lines.append("Done.")

        summary_lines.extend([
            f"Processed: {check_state['current']}/{len(cleaned_codes)}",
            f"Valid: {valid_count}",
            f"Invalid: {invalid_count}",
            f"Errors: {error_count}",
            f"Added to invalid files: {added_to_invalid_count}",
            f"Removed from invalid files: {removed_from_invalid_count}",
            f"Synced lengths: {', '.join(map(str, synced_lengths)) if synced_lengths else 'None'}",
            f"Files folder: {DATA_DIR}",
        ])

        try:
            await status_msg.edit(content="\n".join(summary_lines))
        except Exception:
            pass

        await ctx.send("\n".join(summary_lines))
        reset_check_state()


@bot.command()
async def getinvalid(ctx, length: int):
    if length < 1:
        await ctx.send("Use a number above 0.")
        return

    file_path = ensure_invalid_file(length)

    try:
        await ctx.send(
            content=f"Here is your invalid vanity file for {length} letters:",
            file=discord.File(file_path)
        )
    except Exception as e:
        logger.exception("Failed to send invalid file %s: %s", file_path, e)
        await ctx.send("Failed to send that file.")


@bot.command()
async def invalidfiles(ctx):
    ensure_all_invalid_files()

    files = sorted(DATA_DIR.glob("invalid_*_letters.txt"))
    if not files:
        await ctx.send(f"No invalid files found, but the folder exists: `{DATA_DIR}`")
        return

    file_names = "\n".join(f.name for f in files[:30])
    extra = ""
    if len(files) > 30:
        extra = f"\n...and {len(files) - 30} more"

    await ctx.send(
        f"Invalid txt files are stored in:\n`{DATA_DIR}`\n\nExisting files:\n{file_names}{extra}"
    )


@bot.command()
async def remakeinvalidfiles(ctx):
    ensure_all_invalid_files()
    rewrite_all_invalid_files()
    await ctx.send(f"Recreated missing invalid txt files in `{DATA_DIR}`.")


@bot.command()
@commands.has_permissions(manage_messages=True)
async def purge(ctx, amount: int):
    if amount < 1:
        await ctx.send("Use a number above 0.")
        return

    deleted_total = 0
    remaining = amount

    while remaining > 0:
        batch_size = min(remaining, 100)
        deleted = await ctx.channel.purge(limit=batch_size + 1)
        deleted_total += max(len(deleted) - 1, 0)
        remaining -= batch_size
        await asyncio.sleep(2)

    confirm = await ctx.send(f"Deleted {deleted_total} messages.")
    await asyncio.sleep(3)
    await confirm.delete()


@purge.error
async def purge_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You need Manage Messages permission to use this command.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("Use the command like this: `!purge 500`")


if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing from environment variables.")

bot.run(TOKEN)
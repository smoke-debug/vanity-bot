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

DELAY_SECONDS = 3.5
MAX_CODES_PER_RUN = 600
BACKOFF_SECONDS = 60
MAX_RETRIES = 2

# Folder where invalid txt files will be stored
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data" / "invalid_vanities"

# Optional: pre-create these lengths on startup
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

bot = commands.Bot(command_prefix="!", intents=intents)


# =========================
# FILE HELPERS
# =========================
def ensure_data_dir() -> None:
    """
    Makes sure the storage directory exists.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def get_invalid_file_path(length: int) -> Path:
    """
    Returns the path for the invalid file for a given vanity length.
    """
    ensure_data_dir()
    return DATA_DIR / f"invalid_{length}_letters.txt"


def ensure_invalid_file(length: int) -> Path:
    """
    Makes sure a specific invalid file exists and returns its path.
    """
    file_path = get_invalid_file_path(length)
    if not file_path.exists():
        file_path.touch(exist_ok=True)
        logger.info("Created missing file: %s", file_path)
    return file_path


def ensure_all_invalid_files() -> None:
    """
    Pre-creates the common invalid files on startup.
    """
    ensure_data_dir()
    for length in TRACKED_LENGTHS:
        ensure_invalid_file(length)


def save_invalid_code(invite_code: str) -> bool:
    """
    Saves an invalid code to the length-based txt file.
    Recreates folder/file if deleted.
    Returns True on success, False on failure.
    """
    try:
        invite_code = invite_code.strip()
        if not invite_code:
            return False

        length = len(invite_code)
        file_path = ensure_invalid_file(length)

        with open(file_path, "a", encoding="utf-8") as f:
            f.write(f"{invite_code}\n")

        logger.info("Saved invalid code to %s -> %s", file_path, invite_code)
        return True

    except Exception as e:
        logger.exception("Failed to save invalid code '%s': %s", invite_code, e)
        return False


# =========================
# HELPERS
# =========================
def clean_invite_code(item: str) -> str:
    return (
        item.replace("https://discord.gg/", "")
        .replace("http://discord.gg/", "")
        .replace("discord.gg/", "")
        .replace("https://discord.com/invite/", "")
        .replace("http://discord.com/invite/", "")
        .replace("discord.com/invite/", "")
        .strip()
        .strip("/")
    )


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
    """
    for attempt in range(1, MAX_RETRIES + 1):
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
                await asyncio.sleep(BACKOFF_SECONDS * attempt)
                continue
            return "temporary_error", f"HTTPException: {e}"

        except Exception as e:
            logger.exception("Unexpected error on %s: %s", invite_code, e)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(BACKOFF_SECONDS * attempt)
                continue
            return "temporary_error", f"{type(e).__name__}: {e}"

    return "temporary_error", "Unknown error"


# =========================
# EVENTS
# =========================
@bot.event
async def on_ready():
    ensure_all_invalid_files()
    logger.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    logger.info("Storage folder: %s", DATA_DIR)
    logger.info("Bot is ready.")


# =========================
# COMMANDS
# =========================
@bot.command()
async def sendcodes(ctx, *, words: str):
    items = [w.strip() for w in words.split(",") if w.strip()]

    if not items:
        await ctx.send("No codes found.")
        return

    if len(items) > MAX_CODES_PER_RUN:
        await ctx.send(f"Too many codes at once. Max per run is {MAX_CODES_PER_RUN}.")
        return

    ensure_all_invalid_files()

    send_channel = await get_channel_safe(SEND_CHANNEL_ID)
    invalid_log_channel = await get_channel_safe(INVALID_LOG_CHANNEL_ID)

    if send_channel is None:
        await ctx.send("Could not access the valid invite channel.")
        return

    if invalid_log_channel is None:
        await ctx.send("Could not access the invalid invite channel.")
        return

    valid_count = 0
    invalid_count = 0
    error_count = 0

    invalid_by_length = defaultdict(list)
    seen_invalid = set()
    seen_input = set()

    cleaned_codes = []
    for item in items:
        code = clean_invite_code(item)
        if not code:
            continue

        lowered = code.lower()
        if lowered in seen_input:
            continue

        seen_input.add(lowered)
        cleaned_codes.append(code)

    if not cleaned_codes:
        await ctx.send("No usable codes found after cleaning.")
        return

    status_msg = await ctx.send(
        f"Checking {len(cleaned_codes)} code(s) slowly to avoid rate limits..."
    )

    for index, invite_code in enumerate(cleaned_codes, start=1):
        logger.info("[%s/%s] Checking: %s", index, len(cleaned_codes), invite_code)

        result, payload = await safe_fetch_invite(invite_code)

        if result == "valid":
            invite = payload
            guild_name = getattr(invite.guild, "name", "Unknown")
            logger.info("Valid: %s -> guild=%s", invite_code, guild_name)

            try:
                await send_channel.send(f"discord.gg/{invite_code}")
                valid_count += 1
            except Exception as e:
                logger.exception("Failed to send valid code %s: %s", invite_code, e)
                error_count += 1

        elif result == "invalid":
            logger.info("Invalid: %s", invite_code)
            lowered = invite_code.lower()

            if lowered not in seen_invalid:
                seen_invalid.add(lowered)
                invalid_by_length[len(invite_code)].append(invite_code)

                save_ok = save_invalid_code(invite_code)
                if not save_ok:
                    logger.error("Could not write invalid code to txt file: %s", invite_code)

                try:
                    await invalid_log_channel.send(
                        f"{len(invite_code)} letters | Invalid: `discord.gg/{invite_code}`"
                    )
                except Exception as e:
                    logger.exception("Failed to log invalid %s: %s", invite_code, e)

            invalid_count += 1

        elif result == "temporary_error":
            logger.warning("Temporary error: %s | %s", invite_code, payload)
            error_count += 1

            try:
                await invalid_log_channel.send(
                    f"{len(invite_code)} letters | Temporary error, skipped: `discord.gg/{invite_code}`"
                )
            except Exception as e:
                logger.exception("Failed to log temp error %s: %s", invite_code, e)

            await asyncio.sleep(BACKOFF_SECONDS)

        else:
            logger.error("Fatal error: %s | %s", invite_code, payload)
            error_count += 1

            try:
                await invalid_log_channel.send(
                    f"{len(invite_code)} letters | Error, skipped: `discord.gg/{invite_code}`"
                )
            except Exception as e:
                logger.exception("Failed to log fatal error %s: %s", invite_code, e)

        if index == 1 or index % 5 == 0 or index == len(cleaned_codes):
            try:
                await status_msg.edit(
                    content=(
                        f"Progress: {index}/{len(cleaned_codes)}\n"
                        f"Valid: {valid_count} | Invalid: {invalid_count} | Errors: {error_count}"
                    )
                )
            except Exception:
                pass

        if index < len(cleaned_codes):
            await asyncio.sleep(DELAY_SECONDS)

    summary_lines = [
        "Done.",
        f"Valid: {valid_count}",
        f"Invalid: {invalid_count}",
        f"Errors: {error_count}",
        f"Files folder: {DATA_DIR}"
    ]

    if invalid_by_length:
        grouped = ", ".join(
            f"{length} letters: {len(codes)}"
            for length, codes in sorted(invalid_by_length.items())
        )
        summary_lines.append(f"Invalid breakdown: {grouped}")

    await ctx.send("\n".join(summary_lines))


@bot.command()
async def invalidfiles(ctx):
    """
    Shows where the invalid files are stored and recreates any missing files.
    """
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
    """
    Manually recreates missing invalid txt files.
    """
    ensure_all_invalid_files()
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
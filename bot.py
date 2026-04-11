import os
import asyncio
from collections import defaultdict

import discord
from discord.ext import commands

# =========================
# CONFIG
# =========================
TOKEN = os.getenv("DISCORD_TOKEN")

SEND_CHANNEL_ID = 1491717657567690802
INVALID_LOG_CHANNEL_ID = 1491717674349367386

# Safer defaults
DELAY_SECONDS = 3.5          # normal delay between checks
MAX_CODES_PER_RUN = 300     # hard cap per command
BACKOFF_SECONDS = 60       # wait when Discord pushes back
MAX_RETRIES = 2            # retries for temporary errors

# =========================
# BOT SETUP
# =========================
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


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
        print(f"FAILED TO GET CHANNEL {channel_id}: {e}")
        return None


def save_invalid_code(invite_code: str):
    length = len(invite_code)
    file_name = f"invalid_{length}_letters.txt"

    with open(file_name, "a", encoding="utf-8") as f:
        f.write(f"{invite_code}\n")

    print(f"SAVED TO FILE: {file_name} -> {invite_code}")


async def safe_fetch_invite(invite_code: str):
    """
    Tries to fetch an invite with retries and backoff.
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
            # Could be blocked from seeing the invite, not necessarily invalid
            return "fatal_error", f"Forbidden: {e}"

        except discord.HTTPException as e:
            print(f"HTTPException on {invite_code} (attempt {attempt}/{MAX_RETRIES}): {e}")

            # Back off hard on any HTTP issue to avoid getting clapped by Cloudflare
            if attempt < MAX_RETRIES:
                await asyncio.sleep(BACKOFF_SECONDS * attempt)
                continue

            return "temporary_error", f"HTTPException: {e}"

        except Exception as e:
            print(f"Unexpected error on {invite_code}: {type(e).__name__}: {e}")
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
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("Bot is ready.")


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
        await ctx.send(
            f"Too many codes at once. Max per run is {MAX_CODES_PER_RUN}."
        )
        return

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
        print(f"\n[{index}/{len(cleaned_codes)}] CHECKING: {invite_code}")

        result, payload = await safe_fetch_invite(invite_code)

        if result == "valid":
            invite = payload
            print(f"VALID: {invite_code} -> guild={getattr(invite.guild, 'name', 'Unknown')}")
            try:
                await send_channel.send(f"discord.gg/{invite_code}")
                valid_count += 1
            except Exception as e:
                print(f"FAILED TO SEND VALID CODE {invite_code}: {e}")
                error_count += 1

        elif result == "invalid":
            print(f"INVALID: {invite_code}")
            lowered = invite_code.lower()

            if lowered not in seen_invalid:
                seen_invalid.add(lowered)
                invalid_by_length[len(invite_code)].append(invite_code)
                save_invalid_code(invite_code)

                try:
                    await invalid_log_channel.send(
                        f"{len(invite_code)} letters | Invalid: `discord.gg/{invite_code}`"
                    )
                except Exception as e:
                    print(f"FAILED TO LOG INVALID {invite_code}: {e}")

            invalid_count += 1

        elif result == "temporary_error":
            print(f"TEMP ERROR: {invite_code} | {payload}")
            error_count += 1

            try:
                await invalid_log_channel.send(
                    f"{len(invite_code)} letters | Temporary error, skipped: `discord.gg/{invite_code}`"
                )
            except Exception as e:
                print(f"FAILED TO LOG TEMP ERROR {invite_code}: {e}")

            # Extra cooldown after temporary issues
            await asyncio.sleep(BACKOFF_SECONDS)

        else:
            print(f"FATAL ERROR: {invite_code} | {payload}")
            error_count += 1

            try:
                await invalid_log_channel.send(
                    f"{len(invite_code)} letters | Error, skipped: `discord.gg/{invite_code}`"
                )
            except Exception as e:
                print(f"FAILED TO LOG FATAL ERROR {invite_code}: {e}")

        # Update progress sometimes
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

        # Main pacing delay
        if index < len(cleaned_codes):
            await asyncio.sleep(DELAY_SECONDS)

    summary_lines = [
        "Done.",
        f"Valid: {valid_count}",
        f"Invalid: {invalid_count}",
        f"Errors: {error_count}",
    ]

    if invalid_by_length:
        grouped = ", ".join(
            f"{length} letters: {len(codes)}"
            for length, codes in sorted(invalid_by_length.items())
        )
        summary_lines.append(f"Invalid breakdown: {grouped}")

    await ctx.send("\n".join(summary_lines))


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
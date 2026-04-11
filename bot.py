import discord
from discord.ext import commands
import asyncio
from collections import defaultdict

TOKEN = "MTQ5MTcwODQyMjU2NjE4Mjk4Mg.GXasrJ.m-O8DHbd905qCEU5YdyRjEhmW-ZkxHrg1JEnww"

SEND_CHANNEL_ID = 1491717657567690802
INVALID_LOG_CHANNEL_ID = 1491717674349367386

DELAY_SECONDS = 2

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


def clean_invite_code(item: str) -> str:
    return (
        item.replace("https://discord.gg/", "")
        .replace("http://discord.gg/", "")
        .replace("discord.gg/", "")
        .replace("https://discord.com/invite/", "")
        .replace("http://discord.com/invite/", "")
        .replace("discord.com/invite/", "")
        .strip()
    )


async def get_channel_safe(channel_id: int):
    channel = bot.get_channel(channel_id)
    if channel is not None:
        return channel

    try:
        channel = await bot.fetch_channel(channel_id)
        return channel
    except Exception as e:
        print(f"FAILED TO GET CHANNEL {channel_id}: {e}")
        return None


def save_invalid_code(invite_code: str):
    length = len(invite_code)
    file_name = f"invalid_{length}_letters.txt"

    with open(file_name, "a", encoding="utf-8") as f:
        f.write(f"{invite_code}\n")

    print(f"SAVED TO FILE: {file_name} -> {invite_code}")


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    print("Bot is ready.")


@bot.command()
async def sendcodes(ctx, *, words):
    items = [w.strip() for w in words.split(",") if w.strip()]

    if not items:
        await ctx.send("No words found.")
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

    await ctx.send(f"Checking {len(items)} code(s)...")

    for item in items:
        invite_code = clean_invite_code(item)

        if not invite_code:
            continue

        print(f"\nCHECKING RAW: {item}")
        print(f"CLEANED CODE: {invite_code}")

        try:
            invite = await bot.fetch_invite(invite_code)
            print(f"VALID: {invite_code} -> guild={getattr(invite.guild, 'name', 'Unknown')}")
            await send_channel.send(f"discord.gg/{invite_code}")
            valid_count += 1

        except discord.NotFound as e:
            print(f"NOTFOUND INVALID: {invite_code} | {e}")

            if invite_code.lower() not in seen_invalid:
                seen_invalid.add(invite_code.lower())
                invalid_by_length[len(invite_code)].append(invite_code)
                save_invalid_code(invite_code)

                try:
                    await invalid_log_channel.send(
                        f"{len(invite_code)} letters | Invalid: `discord.gg/{invite_code}`"
                    )
                    print(f"SENT TO INVALID CHANNEL: {invite_code}")
                except Exception as send_err:
                    print(f"FAILED TO SEND TO INVALID CHANNEL: {invite_code} | {send_err}")

            invalid_count += 1

        except discord.Forbidden as e:
            print(f"FORBIDDEN ERROR: {invite_code} | {e}")

            if invite_code.lower() not in seen_invalid:
                seen_invalid.add(invite_code.lower())
                invalid_by_length[len(invite_code)].append(invite_code)
                save_invalid_code(invite_code)

                try:
                    await invalid_log_channel.send(
                        f"{len(invite_code)} letters | Forbidden/Error: `discord.gg/{invite_code}`"
                    )
                    print(f"SENT FORBIDDEN TO INVALID CHANNEL: {invite_code}")
                except Exception as send_err:
                    print(f"FAILED TO SEND FORBIDDEN TO INVALID CHANNEL: {invite_code} | {send_err}")

            invalid_count += 1
            error_count += 1

        except discord.HTTPException as e:
            print(f"HTTP ERROR: {invite_code} | {e}")

            if invite_code.lower() not in seen_invalid:
                seen_invalid.add(invite_code.lower())
                invalid_by_length[len(invite_code)].append(invite_code)
                save_invalid_code(invite_code)

                try:
                    await invalid_log_channel.send(
                        f"{len(invite_code)} letters | HTTP Error: `discord.gg/{invite_code}`"
                    )
                    print(f"SENT HTTP ERROR TO INVALID CHANNEL: {invite_code}")
                except Exception as send_err:
                    print(f"FAILED TO SEND HTTP ERROR TO INVALID CHANNEL: {invite_code} | {send_err}")

            invalid_count += 1
            error_count += 1

        except Exception as e:
            print(f"OTHER ERROR: {invite_code} | {type(e).__name__}: {e}")

            if invite_code.lower() not in seen_invalid:
                seen_invalid.add(invite_code.lower())
                invalid_by_length[len(invite_code)].append(invite_code)
                save_invalid_code(invite_code)

                try:
                    await invalid_log_channel.send(
                        f"{len(invite_code)} letters | Other Error: `discord.gg/{invite_code}`"
                    )
                    print(f"SENT OTHER ERROR TO INVALID CHANNEL: {invite_code}")
                except Exception as send_err:
                    print(f"FAILED TO SEND OTHER ERROR TO INVALID CHANNEL: {invite_code} | {send_err}")

            invalid_count += 1
            error_count += 1

        await asyncio.sleep(DELAY_SECONDS)

    await ctx.send(
        f"Done.\nValid: {valid_count}\nInvalid: {invalid_count}\nErrors: {error_count}"
    )


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
        await asyncio.sleep(1)

    confirm = await ctx.send(f"Deleted {deleted_total} messages.")
    await asyncio.sleep(3)
    await confirm.delete()


@purge.error
async def purge_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You need Manage Messages permission to use this command.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("Use the command like this: `!purge 500`")


bot.run(TOKEN)
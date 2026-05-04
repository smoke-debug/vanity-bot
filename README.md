# Vanity Checker Bot

This bot checks Discord invite/vanity words and manages multiple saved lists automatically.

## Meaning

- **Available / Untaken** = `discord.gg/word` does not currently point to a server.
- **Taken / Unavailable** = `discord.gg/word` already points to a server.

## Install

```bash
pip install -r requirements.txt
```

Create an environment variable:

```bash
DISCORD_TOKEN=your_token_here
```

Then run:

```bash
python bot.py
```

## Required Discord Developer Portal Settings

Enable:

- Message Content Intent
- Server Members Intent is not required for this bot
- Guilds intent is handled by the code

## Easy Setup

Create a list:

```txt
!list create 4letters
```

Set channels:

```txt
!list channels 4letters #claims #copy #logs #summaries
```

Set ping role:

```txt
!list role 4letters @Hunters
```

Add words:

```txt
!list words 4letters love, hate, void, glow
```

Run one list:

```txt
!list run 4letters
```

Run all lists:

```txt
!checkall
```

## Quick Setup

You can also do everything in one command:

```txt
!addlist 4letters #claims #copy #logs #summaries @Hunters love, hate, void, glow
```

## Channels

- **Claims channel**: sends `discord.gg/word` for each available / untaken word.
- **Copy channel**: sends a plain comma-separated paragraph like `love, void, glow`.
- **Logs channel**: sends updates when words become available/taken or errors happen.
- **Summary channel**: sends progress and final summary with role ping.

## Auto Checks

Start auto checks:

```txt
!autocheck 30
```

This checks all saved lists every 30 minutes.

The bot will never run two lists at the same time. If a check is already running, manual commands will say:

```txt
List is already running, please wait.
```

Stop auto checks:

```txt
!autostop
```

Check auto status:

```txt
!autostatus
```

## List Commands

```txt
!list all
!list status 4letters
!list append 4letters aura, mine, pmo
!list remove_words 4letters love, hate
!list remove 4letters
!list clearall
```

## Available Files

The bot stores available / untaken words in:

```txt
data/available_vanities/
```

Commands:

```txt
!availablecount 4
!getavailable 4
!clearavailable 4
!clearavailable
```

Backwards compatible old commands also work:

```txt
!invalidcount 4
!getinvalid 4
!clearinvalid 4
```

They still refer to the same available / untaken files.

## Railway Setup

1. Upload this package to GitHub.
2. Create a new Railway project from the repo.
3. Add variable:
   - `DISCORD_TOKEN`
4. Start command:
   - `python bot.py`

## Notes

Discord can still rate limit or fail temporarily. This bot handles those errors and keeps going instead of crashing.

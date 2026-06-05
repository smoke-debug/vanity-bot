# Vanity Checker Bot

Discord invite vanity checker with saved lists, not-taken TXT files, countdown tracking, incremental backfill, and persistent autosaves.

## Railway Start Command

```txt
python bot.py
```

The included `Procfile` already uses:

```txt
worker: python bot.py
```

## Required Railway Variable

```txt
DISCORD_TOKEN=your_bot_token
```

Optional:

```txt
BOT_PREFIX=!
DATA_DIR=/data
```

## Important: keeping data after updates/redeploys

The bot automatically saves lists and countdowns, but Railway can wipe normal project folders during redeploys. For true update/redeploy persistence:

1. Add a Railway Volume.
2. Mount it at `/data`.
3. Set `DATA_DIR=/data` in Railway variables.
4. Restart the bot.
5. Run `!datastatus` and confirm it says Railway Volume Path: `Yes`.

If you do not use a Railway Volume, normal bot restarts may keep data, but redeploys/updates can still wipe it.

## Setup a List

```txt
!addlist <name> <available_channel> <taken_channel> <log_channel> <summary_channel> <ping_role|none> <words>
```

Example:

```txt
!addlist 8letter #available #taken #log #summary none aviation, awakened, backpack
```

## Manage Lists

```txt
!lists
!listinfo <name>
!addwords <name> <words>
!removewords <name> <words>
!setchannels <name> <available> <taken> <log> <summary>
!setpingrole <name> <role|none>
!removelist <name>
!clearlists
```

Lists save immediately to `data/vanity_config.json`.

## Run Checks

```txt
!checklist <name>
!checkall
!stop
```

## Auto Checks

```txt
!autocheck <minutes>
!autostop
!autostatus
```

## 30-Day Countdown Tracker

The countdown starts when the bot detects:

```txt
taken/on-server -> not taken/available
```

It moves completed timers into the expired list and shows the expiration time.

```txt
!setalertchannel #channel
!invalid
!invalid <vanity>
!countdown <vanity>
!invalidrecent [limit]
!invalidexpiring [limit]
!invalidexpired [limit]
!expiredinvalid [limit]
!invalidcount
!invalidexport
```

Countdowns save immediately to:

```txt
data/invalid_vanities.json
data/expired_invalid_vanities.json
```

## Backfill Old Log Messages

Scan saved list log channels:

```txt
!backfillinvalid 5000
```

Scan a specific channel you choose:

```txt
!backfillchannel #log 5000
```

Check progress:

```txt
!backfillstatus #log
```

Reset only the scan cursor for a channel:

```txt
!resetbackfill #log
```

Backfill is incremental. Running `!backfillchannel #log 5000` again skips already-scanned messages, catches newer messages, then continues older unscanned history.

## Data / Saves

```txt
!datastatus
!savedata
!exportdata
```

The bot saves immediately after important actions, autosaves every 2 minutes, saves on Discord disconnect, saves on shutdown signals, and keeps JSON backups in `data/backups`.

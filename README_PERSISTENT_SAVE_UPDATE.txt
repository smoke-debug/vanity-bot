# Persistent Save Update

This version saves all bot state automatically and more safely.

## What now saves automatically

- Saved vanity lists: `data/vanity_config.json`
- Active countdowns: `data/invalid_vanities.json`
- Expired countdowns: `data/expired_invalid_vanities.json`
- Incremental backfill cursors: `data/backfill_scan_state.json`
- Backfill transition events: `data/backfill_transition_events.json`
- Not-taken TXT files: `data/unavailable_vanities/*.txt`
- Event/audit log: `data/bot_events.log`
- Automatic backups: `data/backups/*.json`

Lists are saved immediately when you run commands like:

- `!addlist`
- `!addwords`
- `!removewords`
- `!setchannels`
- `!setpingrole`
- `!removelist`
- `!clearlists`

Countdowns are saved immediately when a vanity enters or leaves the tracker. The bot also autosaves everything every 2 minutes and saves again on disconnect/shutdown.

## Important for Railway

Code can save files, but Railway redeploys can wipe normal app folders unless you use a Volume.

Recommended Railway setup:

1. Add a Railway Volume to the service.
2. Mount it at `/data`.
3. Add this variable:

```txt
DATA_DIR=/data
```

Then run:

```txt
!datastatus
```

You should see the data folder as `/data` and Railway Volume Path as `Yes`.

## New data commands

```txt
!datastatus
```
Shows data folder, saved list count, countdown count, file sizes, and whether it is using `/data`.

```txt
!savedata
```
Forces an immediate save of all data.

```txt
!exportdata
```
Uploads a ZIP backup of all saved data.

## Backfill commands kept

```txt
!backfillchannel #log 5000
!backfillstatus #log
!resetbackfill #log
```

Backfill is still incremental and skips already-scanned messages unless you reset the cursor.

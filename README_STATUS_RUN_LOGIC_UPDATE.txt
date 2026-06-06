Status-run logic update
=======================

This build fixes backfill countdown accuracy for rerun logs.

Backfill no longer uses the newest not-taken/available log every time.
For each vanity, it groups all scanned log events and replays them oldest to newest:

- taken/on-server -> first not-taken/available = countdown start
- repeated not-taken/available logs after that = ignored, countdown is NOT reset
- not-taken/available -> taken/on-server = countdown removed/skipped
- if the newest scanned state is taken/on-server = no active countdown

This prevents rerunning a list from making old available vanities look newly available.

Recommended rebuild after deploying:

!resetcountdowns
!resetbackfill #log
!backfillchannel #log 10000
!verifycountdowns 500
!topcountdowns 100

New/updated:

- /help is now a slash command with button pages.
- !help [page] still works as a prefix fallback.
- !topcountdowns now supports up to 500 entries and sends multiple embeds when needed.
- Backfill result embeds show how many repeated available logs were ignored.

Persistence reminder for Railway:
Attach a Railway Volume and set DATA_DIR=/data so config, lists, statuses, countdowns, backfill state, and backups survive redeploys.

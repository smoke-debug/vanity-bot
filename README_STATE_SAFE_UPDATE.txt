State-safe countdown/backfill update
====================================

This update makes backfill stricter so countdowns are only kept when the newest scanned log update for that vanity says it is not taken / available.

What changed:
- Backfill now parses both transition messages:
  - discord.gg/example is not taken/available and was added to the not-taken TXT file.
  - discord.gg/example is taken/on a server and was removed from the not-taken TXT file.
- Backfill also parses normal taken status lines:
  - 8 letters | Taken/on server: discord.gg/example
- When replaying all stored backfill events, the bot only keeps countdowns for vanities whose latest scanned update is "available".
- If a newer scanned update says "taken/on-server", the bot removes/skips that vanity from active and expired countdown trackers.

Commands to use:
- !backfillchannel #log 5000
- !backfillstatus #log
- !resetbackfill #log   (only if you intentionally want to rescan that channel)

Important:
- The bot can only know about messages it has scanned. Run !backfillchannel repeatedly until older history is complete if you want the strongest historical state.
- Live checks already start a countdown only when a code is newly added to the not-taken TXT file, and remove it when it becomes taken again.

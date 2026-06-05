Vanity Bot Countdown + Incremental Channel Backfill Update
=========================================================

This package updates the countdown tracker and adds incremental backfill scanning.

Tracker behavior
----------------
The countdown starts when the bot detects this transition:

    taken / on-server (200) -> not taken / available (404)

That is the same log message shown as:

    discord.gg/example is not taken/available and was added to the not-taken TXT file.

The active countdown is saved in:

    data/invalid_vanities.json

When the 30-day timer ends, it moves into:

    data/expired_invalid_vanities.json

The expired record includes the exact timer expiration time.

If the vanity becomes taken/on-server again, the bot removes it from active and expired tracking.

Incremental backfill behavior
-----------------------------
Backfill progress is now saved, so running the command again does not rescan the same channel range.

Saved progress files:

    data/backfill_scan_state.json
    data/backfill_transition_events.json

How it works:

1. First run scans up to the latest message_limit messages.
2. Next runs skip the already-scanned range.
3. It checks messages newer than the last scan.
4. Then it uses the rest of the limit to continue farther back into older unscanned history.
5. Matching transition events are stored once, so duplicate countdowns are skipped.
6. The tracker is rebuilt from all stored transition events in chronological order.

Recommended usage:

    !backfillchannel #log 5000

Run the same command again to continue from unscanned messages.

New / updated commands
----------------------
!setalertchannel #channel
    Sets the channel where @everyone completion alerts are sent.

!invalid
    Shows active countdowns in an embed.

!invalid <vanity>
!countdown <vanity>
    Shows the exact countdown for one vanity.

!invalidrecent [limit]
    Shows recent active countdowns.

!invalidexpiring [limit]
    Shows active countdowns expiring soon.

!invalidexpired [limit]
!expiredinvalid [limit]
    Shows the separate expired countdown list and when each timer expired.

!invalidcount
    Shows total active countdowns, expired countdowns, and upcoming expirations.

!invalidexport
    Exports active and expired countdown data as JSON.

!backfillchannel #channel [message_limit]
    Incrementally scans any channel you choose for old bot log messages.
    Example:
        !backfillchannel #log 5000

!backfillinvalid [messages_per_log_channel]
    Incrementally scans saved list log channels, if your saved lists have log channels configured.
    If it says no log channels are found, use !backfillchannel instead.

!backfillstatus [#channel]
    Shows the saved scan cursor/progress for one channel or all scanned channels.

!resetbackfill #channel
    Resets only the scan cursor for a channel. Stored transition events are kept to prevent duplicate countdowns.

Backfill matching
-----------------
Backfill scans messages like:

    discord.gg/example is not taken/available and was added to the not-taken TXT file.

The Discord message timestamp becomes the countdown start time.

If the scan sees a later message like:

    discord.gg/example is taken/on a server and was removed from the not-taken TXT file.

then the countdown is removed, because the vanity became taken/on-server again.

Old timers that already expired before this update are moved into the expired list, but the bot does not @everyone ping for old backfilled expirations to avoid spam.

Data persistence / Railway notes
--------------------------------
The bot saves lists and countdowns automatically in the data folder:

    data/vanity_config.json
    data/invalid_vanities.json
    data/expired_invalid_vanities.json
    data/backfill_scan_state.json
    data/backfill_transition_events.json
    data/unavailable_vanities/*.txt

The code will not delete saved lists unless you use commands like !removelist or !clearlists.

For Railway updates/redeploys, attach a Railway Volume and set this environment variable:

    DATA_DIR=/data

Then mount the volume to /data in Railway. Without a persistent Railway volume, files written at runtime can be lost during redeploys.

The bot also creates safety backups of vanity_config.json in:

    data/backups/

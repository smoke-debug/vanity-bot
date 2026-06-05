Vanity Bot Countdown + Channel Backfill Update
=============================================

This package updates the countdown tracker and adds a manual channel backfill command.

Tracker behavior
----------------
The countdown now starts when the bot detects this transition:

    taken / on-server (200) -> not taken / available (404)

That is the same log message shown as:

    discord.gg/example is not taken/available and was added to the not-taken TXT file.

The active countdown is saved in:

    data/invalid_vanities.json

When the 30-day timer ends, it moves into:

    data/expired_invalid_vanities.json

The expired record includes the exact timer expiration time.

If the vanity becomes taken/on-server again, the bot removes it from active and expired tracking.

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
    Scans any channel you choose for old bot log messages.
    This is the fix for cases where saved log channels are missing.
    Example:
        !backfillchannel #log 5000

!backfillinvalid [messages_per_log_channel]
    Scans saved list log channels, if your saved lists have log channels configured.
    If it says no log channels are found, use !backfillchannel instead.

Backfill behavior
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
    data/unavailable_vanities/*.txt

The code will not delete saved lists unless you use commands like !removelist or !clearlists.

For Railway updates/redeploys, attach a Railway Volume and set this environment variable:

    DATA_DIR=/data

Then mount the volume to /data in Railway. Without a persistent Railway volume, files written at runtime can be lost during redeploys.

The bot also creates safety backups of vanity_config.json in:

    data/backups/

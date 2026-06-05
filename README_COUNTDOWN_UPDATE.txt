Vanity Bot Countdown Update
===========================

This package adds a 30-day countdown tracker to the existing vanity checker.

Tracker behavior
----------------
The countdown starts only when the bot detects this exact transition:

    not taken / available (404) -> taken / on-server (200)

This is detected when a vanity was already saved in the not-taken TXT files and a new check sees it as taken.

The active record is saved in:

    data/invalid_vanities.json

When the 30-day timer ends, the record is moved into:

    data/expired_invalid_vanities.json

The expired record includes when the timer expired.

If a vanity becomes not taken / available again, it is automatically removed from the active or expired tracker.

New commands
------------
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

!backfillinvalid [messages_per_log_channel]
    Scans configured log channels for old transition messages and rebuilds countdowns from Discord message timestamps.
    Default scan size is 5000 messages per log channel.

Backfill notes
--------------
Backfill uses messages like:

    discord.gg/example is taken/on a server and was removed from the not-taken TXT file.

Those messages are created only when the bot detected available -> taken, so their Discord timestamps are used as the countdown start time.

Old timers that already expired before this update are moved into the expired list, but the bot does not @everyone ping for old backfilled expirations to avoid spam.

Railway notes
-------------
Keep your existing DISCORD_TOKEN variable.
No new environment variables are required.
Use !setalertchannel after deployment to choose the alert channel.

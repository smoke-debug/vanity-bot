Reset countdowns + latest not-taken countdown fix

Changes in this build:

1. Countdown starts/resets from the newest not-taken/available transition
   - If the same vanity has multiple "discord.gg/code is not taken/available..." logs,
     the bot uses the newest scanned one as the countdown start.
   - If a newer log says the vanity is taken/on-server, the countdown is removed/skipped.
   - If a live check sees a newer not-taken transition than the saved countdown, the countdown
     resets to that newest detection time instead of keeping an older timer.

2. New admin command:
   !resetcountdowns

   This deletes:
   - active countdowns
   - expired countdowns
   - stored backfill transition events

   It does NOT delete:
   - saved vanity lists
   - list channel setup
   - backfill channel cursors

   If you also want to rescan old channel history from scratch after resetting countdowns, run:
   !resetbackfill #channel
   !backfillchannel #channel 5000

Recommended after deploying:

!verifycountdowns 200
!topcountdowns 50

If you want to fully rebuild the countdown database from a log channel:

!resetcountdowns
!resetbackfill #log
!backfillchannel #log 5000

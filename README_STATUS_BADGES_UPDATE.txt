STATUS BADGES / COUNTDOWN SUMMARY UPDATE

Added a clearer not-taken status breakdown after every list check.

The summary now marks available/not-taken vanities as:

🟢 New live countdown
- This check proved taken/on-server -> not-taken/available.
- A fresh 30-day countdown was started.

🟢 Active live countdown
- The vanity is already on countdown from a live checker transition.

🟡 Active backfill countdown
- The vanity is on countdown from Discord log backfill.
- Backfill uses the first not-taken log after the most recent taken log.

🟣 Expired timer
- The vanity already completed the 30-day timer and was moved to expired countdowns.

🔴 No countdown / unchanged
- The vanity is currently not taken, but no proven taken -> not-taken transition exists.
- This is usually a rerun, first-seen available vanity, or no reliable countdown source.

New command:
!statuslegend
Aliases:
!summarylegend
!countdownlegend
!availablelegend

The bot also attaches a full categorized TXT file if the not-taken result list is long.

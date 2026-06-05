Verified countdown tracker update
=================================

This build fixes stale/bad countdowns showing in !topcountdowns.

Changes:
- !topcountdowns now live-verifies the shortest countdown candidates against Discord before displaying.
- If Discord says a vanity is currently taken/on-server, the bot removes it from the active countdown tracker.
- Added !verifycountdowns [limit] to live-check saved countdowns manually.
- Added !setcountdownlengths <min> [max] to filter/prune countdowns by vanity length.
- Added !prunecountdowns to remove saved records outside your current length filter.
- New countdowns/backfill events are ignored if they do not match the length filter.

Recommended cleanup after deploying:
1) If you only care about 3+ character vanities:
   !setcountdownlengths 3 32

2) If you only care about 4+ character vanities:
   !setcountdownlengths 4 32

3) Then live-verify the saved tracker:
   !verifycountdowns 200

4) Then show the top list:
   !topcountdowns 50

Notes:
- !topcountdowns checks the current Discord invite status before showing the embed, but it only verifies the shortest candidates to avoid hammering Discord.
- !verifycountdowns can be run multiple times to keep cleaning more active records.
- Lists and countdowns still save to DATA_DIR. On Railway, use a Volume and set DATA_DIR=/data.

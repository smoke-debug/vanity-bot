STATUS-SAFE COUNTDOWN FIX

This build fixes the issue where every currently not-taken/available vanity could get a fresh countdown after a restart, redeploy, or lost not-taken TXT data.

New behavior:
- The bot saves a persistent last-known status for every checked vanity in data/vanity_statuses.json.
- A live countdown only starts when the previous saved status was taken/on-server and the newest check says not-taken/available.
- If the bot has no previous saved status for a vanity, it can add it to the not-taken TXT file, but it will NOT start a countdown.
- On startup, the bot seeds saved statuses from the existing not-taken TXT files as available, without creating countdowns.
- Countdowns still save to data/invalid_vanities.json and expired timers save to data/expired_invalid_vanities.json.

Recommended cleanup after deploying this fix:
1. !resetcountdowns
2. !resetbackfill #log
3. !backfillchannel #log 5000
4. !verifycountdowns 500
5. !topcountdowns 50

If you use Railway, attach a volume and set DATA_DIR=/data so data files survive redeploys.

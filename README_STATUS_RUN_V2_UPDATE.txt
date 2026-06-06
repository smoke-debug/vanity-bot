# Status-run backfill v2 update

This version makes backfill stricter and less noisy.

## Countdown start logic
Backfill now starts a countdown only when it can prove this run:

    taken/on-server -> first not-taken/available

Repeated not-taken logs from rerunning the same list are ignored and do not reset the timer.
Repeated taken logs are also ignored.

If a vanity only has not-taken/available logs and no prior taken/on-server log in scanned history, it is skipped. This prevents fake countdowns from old available logs.

## New timeline command
Use:

    !backfilltimeline <vanity> [runs]

Aliases:

    !statusruns <vanity> [runs]
    !vanitytimeline <vanity> [runs]
    !backfillruns <vanity> [runs]

The embed compresses repeated statuses like:

    taken x10
    not taken x4

so it does not flood the embed with duplicate taken/not-taken messages.

## Recommended rebuild

    !resetcountdowns
    !resetbackfill #log
    !backfillchannel #log 10000
    !backfilltimeline mean
    !topcountdowns 100

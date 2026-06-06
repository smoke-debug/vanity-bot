Alert stages + confidence update

New countdown alerts:
- Sends staged @everyone alerts in the configured alert channel when a countdown reaches:
  - 7 days left
  - 24 hours left
  - 6 hours left
  - 1 hour left
  - countdown complete
- Stage alerts are saved inside each countdown record under `stage_alerts`, so bot restarts do not duplicate them.
- Backfilled countdowns skip any alert stages that were already passed before the backfill rebuild, preventing mass pings from old data.

New commands:
- !alertstages
  Shows whether staged alerts are enabled, the alert channel, and the current stage thresholds.

- !setalertstages default
  Resets to 7d / 24h / 6h / 1h.

- !setalertstages off
  Turns off staged alerts. The final countdown-complete alert still works.

- !setalertstages on
  Turns staged alerts back on.

- !setalertstages 7d 24h 6h 1h
  Customizes stages. Supports d/h/m values.

Confidence labels:
- Countdown detail embeds now show confidence.
- High = live checker transition detected the vanity changing from taken/on-server to not-taken/available.
- Medium = timestamp was rebuilt from Discord log history/backfill.
- Low = manual/unknown source.

Recommended clean rebuild:
!resetcountdowns
!resetbackfill #log
!backfillchannel #log 10000
!verifycountdowns 500
!alertstages
!topcountdowns 100

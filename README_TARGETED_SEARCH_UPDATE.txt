Targeted Search Update
======================

Added targeted vanity log search:

!search mean
!search mean act sue
!search #log mean act sue

What it does:
- Searches configured saved log channels by default.
- If you mention a channel first, it only searches that channel.
- Scans up to 50,000 messages per searched channel.
- Stores any matching taken/not-taken transition events for only the requested vanity codes.
- Replays the strict status-run logic after searching:
  taken/on-server -> first not-taken/available = countdown start
  repeated not-taken rerun logs do not reset the timer
  newest taken/on-server log removes/skips the countdown
- Shows per-vanity results in an embed.

Helpful follow-up:
!backfilltimeline mean

Use this to inspect the compressed status runs like taken x10 / not taken x4.

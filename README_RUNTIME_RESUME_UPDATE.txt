Runtime Resume + Persistence Update
===================================

This version saves every important bot state to the data folder:

- vanity_config.json: saved lists, channels, prefix, alert config, auto-check config
- vanity_statuses.json: last known status for each vanity
- invalid_vanities.json: active countdowns
- expired_invalid_vanities.json: completed countdowns
- backfill_scan_state.json: incremental backfill cursors
- backfill_transition_events.json: parsed log status events
- runtime_state.json: currently running manual check/backfill so it can resume after restart
- bot_events.log: audit log of saves, runtime jobs, transitions, alerts, and startup/shutdown
- backups/: rotating backups of JSON files

New behavior:

- Lists save immediately when added/edited.
- Countdowns save immediately when created/removed/expired.
- Backfill progress saves automatically.
- Manual !checklist, !checkall, !backfillchannel, and !backfillinvalid save a runtime job before starting.
- If the bot restarts/redeploys during one of those jobs, it will resume it after reconnecting.
- Auto-check settings already persist through vanity_config.json, so automatic checking continues after restart if enabled.
- Resumed checks are safe because countdowns still only start on confirmed taken -> available transitions from saved vanity_statuses.

New commands:

!resumestatus
Shows the saved active runtime job and recent completed jobs.

!setautoresume on
Enables automatic resume of interrupted manual jobs.

!setautoresume off
Disables automatic resume.

!clearresume
Clears only the saved runtime job. Does not delete lists, countdowns, statuses, or backfill progress.

Railway note:

For data to survive redeploys/updates, attach a Railway Volume and set DATA_DIR=/data.
Without a persistent volume, files can still be wiped by Railway when rebuilding/redeploying the service.

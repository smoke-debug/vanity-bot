# Vanity Checker Bot - Final Slow/Safe Version

## Channels
- `#valid` = invite exists / already on a server
- `#invalid` = invite does not exist / not on a server
- `#summary` = complete summary embeds and full txt files
- `#log` = progress, cooldowns, and errors

## Setup
```txt
!setup 3letters #valid #invalid #summary #log abc, lol, pmo, vip
```

## Run
```txt
!run 3letters
!runall
```

## Add more words without replacing
```txt
!append 3letters new, more, words
```

## Replace list
```txt
!words 3letters new, full, replacement, list
```

## Get latest txt files
```txt
!gettxt valid 3
!gettxt invalid 3
!gettxt valid
!gettxt invalid
!gettxt all
```

## Auto
```txt
!autocheck 30
!autostop
!autostatus
```

Railway start command: `python bot.py`

## 30-Day Countdown Update

This version includes an active/expired countdown tracker for vanities that change from taken/on-server to not taken/available.

Main new commands:

- `!setalertchannel #channel`
- `!invalid`
- `!invalid <vanity>`
- `!countdown <vanity>`
- `!invalidrecent [limit]`
- `!invalidexpiring [limit]`
- `!invalidexpired [limit]`
- `!invalidcount`
- `!invalidexport`
- `!backfillinvalid [messages_per_log_channel]`
- `!backfillchannel #channel [message_limit]`
- `!backfillstatus [#channel]`
- `!resetbackfill #channel`

See `README_COUNTDOWN_UPDATE.txt` for details.

Countdown channel backfill update
---------------------------------
Use `!backfillchannel #log 5000` to incrementally scan a specific Discord channel for old transition messages and create countdowns from the Discord message timestamps. Running it again skips the already-scanned range, catches newer messages, then continues farther back into older unscanned history.

To keep lists after Railway redeploys, attach a Railway Volume and set `DATA_DIR=/data`.

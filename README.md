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

This version includes an active/expired countdown tracker for vanities that change from not taken/available to taken/on-server.

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

See `README_COUNTDOWN_UPDATE.txt` for details.

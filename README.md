# Vanity Checker Bot - Optimized Timing Version

## Layout
- `#valid` = invite exists / already on a server
- `#invalid` = invite does not exist / not on a server
- `#summary` = final summary + comma-separated embeds
- `#log` = progress, cooldowns, errors

## Timing
- 3 seconds between every word
- 10 seconds after every 10 words
- 60 seconds between saved lists

## Setup
```txt
!setup 3letters #valid #invalid #summary #log abc, lol, pmo, vip
```

## Run
```txt
!run 3letters
!runall
```

## Auto
```txt
!autocheck 30
```

## Useful commands
```txt
!help
!lists
!status 3letters
!words 3letters new, word, list
!append 3letters more, words
!remove_list 3letters
!stop
!autostop
!autostatus
```

## Railway
Start command:
```txt
python bot.py
```

Required variable:
```txt
DISCORD_TOKEN=your_bot_token
```

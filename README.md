# Vanity Bot - Server Layout Version

Matches your channel layout:

- #valid = vanities/invites that are already on a server
- #invalid = vanities/invites that are not on a server
- #summary = final comma-separated list embeds
- #log = progress, errors, and finished logs

## Setup

```txt
!setup 4letters #valid #invalid #summary #log love, hate, void, glow
```

## Run

```txt
!run 4letters
```

## Auto

```txt
!autocheck 30
```

## Batch speed

Default:

```txt
check 10 invites
wait 5 seconds
repeat
```

Railway variables:

```txt
BATCH_SIZE=10
BATCH_WAIT_SECONDS=5
```

## Start Command

```txt
python bot.py
```

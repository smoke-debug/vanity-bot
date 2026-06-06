Railway start fix
=================

This package is forced to run as a Python bot, not npm/node.

Included files:
- Procfile: worker: python bot.py
- railway.json: deploy startCommand = python bot.py
- nixpacks.toml: Python provider + start cmd
- Dockerfile: fallback Python container start command

If Railway still shows `/bin/bash: line 1: npm: command not found`, Railway has a manual Start Command override saved in the web UI.
Fix:
1. Railway -> your service -> Settings
2. Find Start Command / Custom Start Command
3. Delete it or set it to: python bot.py
4. Redeploy

Required variables:
- DISCORD_TOKEN = your bot token
- DATA_DIR = /data if using a Railway Volume for persistent saves

Recommended Railway Volume:
- Mount a Volume
- Set DATA_DIR=/data
This keeps lists/countdowns/backfill progress after redeploys.

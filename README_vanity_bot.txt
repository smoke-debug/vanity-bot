Vanity Bot Fixed Package

Files:
- vanity_bot_fixed.py
- requirements.txt

Railway setup:
1. Upload vanity_bot_fixed.py and requirements.txt to your GitHub repo.
2. In Railway, add a variable named DISCORD_TOKEN with your bot token.
3. Make sure your bot has Message Content Intent enabled in the Discord Developer Portal.
4. Start command can be:
   python vanity_bot_fixed.py

Main commands:
!help
!addlist <name> <claim_channel> <log_channel> <summary_channel> <ping_role|none> <words>
!addwords <name> <words>
!checklist <name>
!checkall
!stop
!setprefix <prefix>
!ratelimit <delay_seconds> <batch_size> <batch_cooldown_seconds> <list_cooldown_seconds>

Recommended safer rate limit:
!ratelimit 10 5 75 120

Important:
- Available means Discord returned Unknown Invite / 404.
- Taken means the invite exists / 200.
- If Cloudflare sends HTML, the bot stops checking instead of crashing/spamming logs.

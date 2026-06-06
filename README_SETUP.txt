EASY DISCORD UWU + VOICEMASTER + ECONOMY BOT
============================================

This version has NO src folder and NO database packages.
You only upload these root files to GitHub:

- index.js
- package.json
- railway.json
- env.example
- README_SETUP.txt

Railway Variables:

DISCORD_TOKEN = your bot token
PREFIX = *
DATA_FILE = ./bot-data.json
TEMP_VC_DELETE_DELAY_MS = 500

Optional economy variables:

STARTING_BALANCE = 500
DAILY_REWARD = 2500
WORK_MIN = 250
WORK_MAX = 1200
BEG_MIN = 25
BEG_MAX = 350
WORK_COOLDOWN_MS = 900000
BEG_COOLDOWN_MS = 300000
DONATE_DAILY_LIMIT = 250000
MAX_BET = 150000

Optional persistent Railway volume:
If you want the bot settings/economy balances to survive redeploys, add a Railway Volume with mount path:
/data

Then set:
DATA_FILE = /data/bot-data.json

Discord Developer Portal:
Enable these bot intents:
- Server Members Intent
- Message Content Intent

Recommended bot permissions while testing:
- Administrator

Main Commands:

*help

UwU Commands:
*uwuify @user
*unuwuify @user
*uwulist

VoiceMaster Commands:
*voicemaster setup
*vm setup
*vc help
*vc lock
*vc unlock
*vc hide
*vc unhide
*vc permit @user
*vc reject @user
*vc transfer @user
*vc limit 5
*vc rename new name
*vc bitrate 96
*vc claim
*vc info

Moderation:
*purge 50

Economy:
*economy help
*balance
*balance @user
*daily
*work
*beg
*donate @user amount
*pay @user amount
*leaderboard
*coinflip amount heads/tails
*slots amount
*dice amount over/under
*roulette amount red/black/green
*blackjack amount
*tictactoe @user [amount]
*ttt @user [amount]

Syntax Embeds:
If a user runs a command incorrectly, the bot replies with an Incorrect Syntax embed showing the correct command format.
Example: *uwuify -> embed showing *uwuify @user

Purge:
*purge 50 deletes the command message and quickly deletes 50 previous messages.
On success, the bot sends no confirmation message.

Smoke Bucks Economy:
- Currency is called Smoke Bucks.
- Balances and cooldowns save automatically to DATA_FILE.
- Daily, work, beg, donate/pay, leaderboard, and gambling commands are included.
- Gambling commands include coinflip, slots, dice, roulette, interactive blackjack, and Tic-Tac-Toe challenges.
- Use *economy help in Discord to show users all economy commands.

VoiceMaster channel names in this version have no emojis:
- Public Voice Channels
- Join Public VC
- Random Public VC
- Private Voice Channels
- Join Private VC
- Username's Public VC
- Username's Private VC

Empty temporary VCs delete after 0.5 seconds by default.
To change that speed, edit this Railway variable:
TEMP_VC_DELETE_DELAY_MS = 500

Notes:
- The UwU system targets only users you add with *uwuify @user.
- The bot deletes the targeted user's original message and reposts it with a webhook.
- The bot needs Manage Messages + Manage Webhooks for UwUify.
- Voice channels created by VoiceMaster have voice chat messages disabled for users.
- The bot pings the VC owner and sends an embed with control commands inside the created VC chat.


UPDATE NOTES:
- Temp voice channels now force the creator's Discord username in the channel name, like Username's Public VC or Username's Private VC.
- Blackjack now uses Hit/Stand buttons.
- Added Tic-Tac-Toe with accept/deny buttons and Smoke Bucks betting.


UPDATE v4:
- Gambling animations now use one bot message/embed per command and edit that same embed during the animation.
- Roulette, coinflip, dice, slots, and blackjack have longer anticipation animations.
- Startup log marker should show: Package: premium-games-bleed-ttt-v5


V5 update:
- Tic-Tac-Toe layout redesigned to look cleaner and more Bleed-style.
- Removed the ugly numbered/code-block board and switched to a cleaner emoji board with tile buttons.

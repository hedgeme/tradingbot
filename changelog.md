# Changelog

All notable changes and service updates for the TECBot project will be documented here.

## 2025-09-21
- Fixed `wallet.py` and `.env` (permissions + recoding)
- Restored Telegram service (see chat: "Telegram bot issue 9/21/25")
- Synced updated `wallet.py` to GitHub repo (`hedgeme/tradingbot`)
- Noted next service topics:
  - `/sanity` command not working
  - `/balances` missing some requested tokens
  - `/prices` inaccurate and not in requested format
 

## 2025-09-22
- Fixed Telegram `/balances` prompt (previously not displaying all correct assets)
- Identified that `telegram_listener.py` administers this task and updated the code
- Verified that all assets now display with correct balances
- Next session topics to address:
  - `/sanity` command issues
  - `/prices` inaccuracies

## 2025-09-23
- Fixed `/sanity` function by replacing `telegram_listener.py` with updated code
- Added **terminal server GitHub updates** to pending list (for accurate `/version` reporting)
- Created new file `Commands.md` in repo to document all Telegram bot commands
- Updated BotFather command list to include:
  - /start  
  - /help  
  - /ping  
  - /balances  
  - /sanity  
  - /prices  
  - /plan  
  - /dryrun  
  - /disable  
  - /enable  
  - /cooldowns  
  - /version  
  - /report
- Updated `preflight.py` to resolve `/sanity` issues and committed new file to GitHub
- Updated `price_feed.py` to include Coinbase ETH prices
- Updated `telegram_listener.py` to handle `/sanity` and new bot commands
- Updated `config.py` with latest changes

**Pending**
1. GitHub integration into terminal server updates (for `/version`)
2. Review `/prices` command accuracy
3. Verify all Telegram bot commands function as documented in `Commands.md`

Documented in chat: *“09/23/25 Botfather /sanity”*


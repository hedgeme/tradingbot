# Changelog

All notable changes and service updates for the TECBot project will be documented here.

This project planner is created to work through this Trading Bot project. This is our Github Repo:

https://github.com/hedgeme/tradingbot/tree/main

All other chats within this project have been documented for the support of the development of this project. Review the Github repo and all its content before giving suggestions. Before giving recommendations on modifying working programs, provide at least 5 diagnostic checks to review the issue.

Do not give recommendations without first reviewing the Github repo above and all the files attached to this project.




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


## 2025-09-28
- Improved **price feed logic** to show correct outputs and reference the correct contracts
- Fixed balance displays so that all amounts now show accurately
- Verified that price feeds and balances are working without errors

**Next Work Items**
- `/assets` command not working in Telegram
- `/sanity` command still has issues
- `/version` command not functioning

Documented in chat: *“09/26/25 Telegram Bugs”*

## 2025-10-01
- Completed fixes for `/assets`, `/sanity`, and `/version`
- Confirmed working in **Version: v0.1.0-ops @ f25a7a6**

**Pending**
- Fix remaining Telegram commands:
  - `/cooldowns`
  - `/plan`
  - `/dryrun`
- Begin implementation of the 4 trading strategies
- Add `.db` integration for data persistence and reporting
- Plan future dashboard integration (e.g., Power BI) for visualization

Documented in chat: *“10/21/25 Telegram Bugs”*



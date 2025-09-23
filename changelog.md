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


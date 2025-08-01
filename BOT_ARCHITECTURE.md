# 🏗️ BOT_ARCHITECTURE.md

## 🧩 Trading Bot Architecture & Flow

This document describes the **high-level architecture**, **main components**, and **logical flow** of the Harmony trading bots, based on our verified strategies.

---

## ✅ High-level goals
- Modular, maintainable Python (or Node) codebase.
- Each bot has:
  - Its own wallet, SQLite DB, and config.
  - Runs independently.
- Shared utilities handle common tasks (RPC, pricing, error handling, alerts).

---

## 📦 Main modules & responsibilities

| Module                 | Purpose                                                                                   |
| --------------------- | ----------------------------------------------------------------------------------------- |
| `config.py` / `.env`  | Store RPC URLs, gas caps, slippage, wallet names, external wallet, Telegram keys          |
| `wallet.py`           | Create wallets, sign & send transactions (via `hmy` CLI or Web3)                          |
| `price_feed.py`       | Fetch on-chain prices (from pool contracts) & off-chain prices (Coinbase API)             |
| `strategy_*.py`       | Each bot’s logic (e.g., arbitrage, take-profit, reinvestment)                             |
| `trade_executor.py`   | Build & send swap transactions, handle gas & slippage                                     |
| `cooldown_manager.py` | Track cooldown timers per bot                                                              |
| `db.py`               | Interact with per-bot SQLite DBs (log trades, deposits, withdrawals, profits)             |
| `alert.py`            | Send Telegram alerts (errors, TX confirmations, IP change)                                |
| `monitor.py`          | Node health, gas price, wallet balance & IP change monitors                               |
| `main.py`             | Entrypoint; runs loop for chosen strategy                                                 |

---

## 🔄 Typical bot loop (per strategy)

```mermaid
graph TD
  A[Start / Load config] --> B[Fetch wallet balance & deposits]
  B --> C[Fetch on-chain prices]
  C --> D[Fetch off-chain prices (if needed)]
  D --> E[Apply bot strategy logic]
  E -->|Trigger trade?| F[Build & send TX]
  F --> G[Update DB records]
  G --> H[Start cooldown timer]
  H --> I[Sleep until next cycle]
  I --> B

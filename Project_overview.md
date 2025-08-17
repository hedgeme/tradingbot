# Project Manager Overview — Harmony Trading Bot Suite

**Repository:** https://github.com/hedgeme/tradingbot

---

## 🗂 Purpose

This file centralizes the **current status**, **pending tasks**, and **architecture overview** for the Harmony Trading Bot project.

It acts as the main project management document to coordinate development efforts, track progress, and guide coding.

---

## ✅ Completed / Verified (Summary)

- Detailed bot trading strategies for 4 Harmony bots (arbitrage, take profit, dip buys, pool arbitrage)
- Verified contract addresses, pool info, fee tiers
- Database schema for SQLite with comprehensive trade data fields (including profit, total trade cost)
- Risk controls: max slippage (0.3%), gas price cap (150 gwei), cooldown timers
- Coinbase API scoped as price oracle only (no trading logic yet)
- Telegram alert design for trades, errors, IP changes, idle states
- SwapRouter02 ABI (minimal) verified for use
- Node & backup RPC endpoints set and verified
- Key & Wallet Management     | Secure storage for private keys & tx signing
- Updated GitHub Repo to Private     | ChatGPT has connector access | Completed 8/10/2025                       

---

## 🏗️ Bot Architecture Overview (Draft)

All four bots run separately but share common infrastructure:

- **Separate wallets**: one per bot for asset segregation
- **Deposit detection**: monitor blockchain transfers in
- **Strategy execution**: follow logic (arbitrage, take profit, dip buying, pool arbitrage)
- **Swap execution**: SwapRouter02 on Harmony
- **Cooldown timers**: prevent repeated trades too quickly
- **SQLite DB per bot**: logs all deposits, trades, profit/loss, withdrawals
- **Telegram alerts**: updates for trades, errors, manual withdrawals, IP change
- **Gas & node monitoring**: optimize costs, detect downtime
- **Coinbase ETH price**: as off-chain oracle, currently read-only

---

## 🗄️ Database Schema Overview

| Field               | Type       | Purpose                                                                                             |
| ------------------- | ---------- | --------------------------------------------------------------------------------------------------- |
| `id`                | INTEGER PK | Auto-increment ID                                                                                   |
| `asset`             | TEXT       | Asset traded or deposited (e.g., wONE, ETH, TEC, 1sDAI, 1USDC)                                      |
| `amount`            | REAL       | Amount deposited or acquired                                                                        |
| `deposit_price`     | REAL       | Price at deposit or buy-back time                                                                   |
| `sale_price`        | REAL       | Price when sold/swapped (if applicable)                                                             |
| `total_trade_cost`  | REAL       | Net cost incl. fees/gas                                                                            |
| `profit`            | REAL       | Net gain/loss from sale vs. deposit price                                                           |
| `trade_type`        | TEXT       | ‘buy’, ‘sell’, ‘withdraw’, ‘transfer’, etc.                                                         |
| `timestamp`         | DATETIME   | When trade/deposit happened                                                                         |
| `wallet`            | TEXT       | Bot wallet (strategy)                                                                               |
| `status`            | TEXT       | ‘pending’, ‘sold’, etc.                                                                             |
| `tx_hash`           | TEXT       | Transaction hash                                                                                    |
| `external_transfer` | INTEGER    | 0 / 1, if asset was sent externally                                                                  |
| `external_wallet`   | TEXT       | e.g., always `0x360c48a44f513b5781854588d2f1A40E90093c60`                                           |

---

## 📌 Next Steps

- Finalize architecture doc & coding plan
- Implement deposit detection & trade execution modules
- Build DB & Telegram modules
- Add monitoring & error handling

---

**Reference files:**
- [`README.md`](https://github.com/hedgeme/tradingbot/blob/main/README.md)
- [`verified_info.md`](https://github.com/hedgeme/tradingbot/blob/main/verified_info.md)

*Last updated: 2025-07-31*

Project Manager Overview — Harmony Trading Bot Suite (Status: 2025-08-17)
Repo: hedgeme/tradingbot
Chain: Harmony ONE (Chain ID 1666600000)
Primary RPC: https://api.s0.t.hmny.io (backup: https://api.harmony.one)
Router (Sushi/UniswapV2-compatible): 0x85495f44768ccbb584d9380Cc29149fDAA445F69

✅ Completed / Verified
Infrastructure & Security
Server: Lenovo ThinkCentre M920 • Ubuntu • 32GB RAM • 1TB NVMe
Partitions: / (~100GB), /bot (~50GB), /monero (~800GB reserved)

Firewall / SSH: UFW configured, SSH on non-default port; key-only login.

Harmony CLI: hmy installed at /usr/local/bin/hmy.

Wallets (CLI Keystore): 4 encrypted wallets created and tested

tecbot_eth → one1gntquz9lvm9mh3aedgx7dsqsshkzarg83mjxph

tecbot_sdai → one1z5tgar3skdwmvk8puf2p0w9nav4utaf94jfhjs

tecbot_tec → one1n60pjk3y2c4wrlcezhtsxyxj2hpuymufgqjnd3

tecbot_usdc → one1shsrvmepp2pllgjsxxaqqf4kgrvgazpu6lg9ww

Key protection: Each wallet passphrase stored as GPG-encrypted file in /home/tecviva/.secrets/
(*.pass.gpg, chmod 600).

GPG agent: Long-TTL configured (1 year) for non-interactive runtime; helpers added to .bashrc:

export GPG_TTY=$(tty)

gpgunlock function to unlock all bot wallets on login.

Runtime dirs: /bot/logs and /bot/db created (chmod 700), owned by tecviva.

Code & Config
.env (server-only, not in git) created at /home/tecviva/.env with:
RPC/chain params, wallet names + ONE addresses, paths, gas cap, Telegram token + chat ID.

wallet.py (added):

Loads .env and validates runtime directories.

Preflight GPG: clean exit with instructions if GPG locked.

Passphrase decrypt with gpg --decrypt (uses gpg-agent, keeps secret in memory briefly).

ETH→ONE conversion (hmy utility bech32) for any CLI transfer target.

Native ONE transfer via hmy transfer with shard/chain/node flags.

Minimal, masked logging; per-wallet locking to avoid races.

Verified on-chain addresses (ETH-format) present in repo (tokens/pools/router) for EVM/web3 use.

Operational Tests
.env loading verified with python-dotenv (installed via apt).

wallet.py preflight passes:

csharp
Copy
[wallet] Preflight OK. Ready.
🧭 Scope & Architecture (recap)
Bots (4 processes/strategies): Arbitrage, dip-buy, reinvest variants for 1ETH, TEC, 1USDC, 1sDAI; each with isolated wallet & SQLite DB.

Shared components:

price_feed.py (Coinbase ETH; add Harmony DEX/aggregator fallback)

trade_executor.py (web3 + router ABI; slippage, gas cap, retries)

db.py (SQLite: trades, deposits, withdrawals, PnL, external transfers)

monitor.py (node health, public IP change, withdrawal detection -> pause)

alert.py (Telegram)

cooldown_manager per bot

Data model notes: explicit fields for total trade cost, realized profit, and external transfers (external_wallet, hash, amount, reason).

📋 Pending Development Tasks (next up)
High Priority
Finish Telegram alerts (alert.py) using .env (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)

Send on: preflight failure, trade success/fail, withdrawal detection, low balance.

trade_executor.py

Load router ABI (SwapRouter02_minimal.json).

Swap functions (amountOutMin protection, slippage), gas cap (≤ 150 gwei), nonce handling.

Structured receipts; retry/backoff on transient RPC errors.

price_feed.py

Coinbase ETH baseline + Harmony DEX price (WONE pairs) for cross-check.

Optional TWAP per pool.

db.py

Finalize schema, migrations, and write helpers (log_trade, log_deposit, log_withdrawal, log_external_transfer).

monitor.py

RPC heartbeat, IP change check (hourly), withdraw detection (if any outgoing tx not initiated by bot → set trading pause flag).

Medium Priority
Strategy modules (4 files) wired to trade_executor.py, price_feed.py, db.py.

Cooldown manager policy and persistence.

Systemd services for each bot with autorestart + journal rotation.

Basic CLI tooling (send dust, check balances, toggle pause).

Prometheus-style metrics or lightweight dashboard later.

🔧 Deployment / Ops Checklist
Server one-time setup (done): hmy, GPG, .env, /bot/logs, /bot/db.

Per-update deployment:

Pull/rsync repo changes to server (or copy files listed below).

Ensure .env on server is correct (never commit to git).

Unlock GPG once per long run:

nginx
Copy
gpgunlock
Preflight:

bash
Copy
python3 /bot/wallet.py
Start bots (systemd or screen/tmux).

📦 Files that must be on the server (runtime)
Copy or git pull these into your working dir (e.g., /bot/src or keep in repo path).
Bold = already mentioned / essential now.

wallet.py (you added)

SwapRouter02_minimal.json (router ABI)

verified_info.md (addresses used by code—ETH format)

trade_executor.py (calls router; builds/sends swaps via web3/hmy)

price_feed.py (Coinbase + DEX fallback)

db.py (SQLite helpers + migrations)

monitor.py (RPC/IP/withdraw detection)

alert.py (Telegram alerts)

Strategy files (4): e.g., strategy_eth.py, strategy_sdai.py, strategy_tec.py, strategy_usdc.py

config.py (shared constants; gas cap, slippage defaults, cooldown settings)

README.md (optional but helpful)

Server-only (never in git):

/home/tecviva/.env

/home/tecviva/.secrets/*.pass.gpg (4 wallet passphrase files)

If you prefer a “single app folder” layout, use /bot/app/ and place all code there, keeping /bot/db and /bot/logs separate.

Optional but recommended
requirements.txt (pin versions: web3, requests, python-dotenv, etc.)

start_bot.sh / stop_bot.sh helper scripts

systemd unit files (one per strategy), e.g., /etc/systemd/system/tecbot-eth.service

logging.conf (if using Python logging config)

Healthcheck script (returns non-zero if preflight fails → systemd restart)

---


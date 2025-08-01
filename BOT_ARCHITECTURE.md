# 🏗️ BOT_ARCHITECTURE.md

## 📌 Project Overview & Purpose
This document is the **central technical blueprint & project guide** for developing the Harmony trading bots.

It covers:
- The why & high-level goals
- Detailed bot architecture & logic flow
- Explanation of each module & shared logic
- Database design
- Reference tables for verified contracts & APIs
- Task checklist & next steps

This document evolves alongside the code as tasks are completed.

---

## ✅ High-level goals
- Modular, maintainable Python or Node codebase.
- Each of the 4 trading bots:
  - Own wallet & strategy logic.
  - Own SQLite DB for logging.
- Shared utilities handle: price feeds, trade execution, monitoring, alerts.
- Supports **manual deposits** and automatically handles:
  - Trade execution.
  - Withdraw detection.
  - Cooldowns & reinvestment logic.
- Telegram alerts for visibility.
- Gas & risk management.

---

## ⚙️ Architecture Summary

| Layer                 | Responsibility                                                                 |
| -------------------- | ------------------------------------------------------------------------------ |
| Strategy Layer       | Unique trade logic per bot. Implements buy, sell, arbitrage, reinvestment.    |
| Data Layer           | Price feeds, wallet balances, deposit detection, trade execution.              |
| Database Layer       | Per bot SQLite DB: logs trades, deposits, sales, profit/loss.                  |
| Monitoring Layer     | Node health, RPC errors, gas cap, IP changes, withdraw detection.              |
| Notification Layer  | Telegram alerts for deposits, trades, errors, IP changes.                      |

---

## 🔄 Typical bot loop (per strategy)

  A[Start & load config] --> B[Detect new deposits / check balance]
  B --> C[Fetch on-chain prices]
  C --> D[Fetch Coinbase price if needed]
  D --> E[Apply bot logic]
  E -->|Trigger trade?| F[Build & send TX]
  F --> G[Update DB (log trade)]
  G --> H[Set cooldown]
  H --> I[Sleep & repeat]


Main module & files:

| File/module                        | Purpose                                                                   |
| ---------------------------------- | ------------------------------------------------------------------------- |
| `config.py` / `.env`               | Store RPC URLs, gas caps, slippage, wallets, Telegram tokens.             |
| `wallet.py`                        | Create wallets, load private keys securely, sign/send transactions.       |
| `price_feed.py`                    | Get prices from on-chain pools & Coinbase API.                            |
| `strategy_1_eth_arbitrage.py`      | ETH price arbitrage (Harmony vs Coinbase).                                |
| `strategy_2_wone_takeprofit.py`    | Take profit on wONE; reinvest when price dips.                            |
| `strategy_3_usdc_buy_dips.py`      | Buy ETH or TEC if price dips after 1USDC deposit; reinvest on price rise. |
| `strategy_4_tec_pool_arbitrage.py` | Arbitrage TEC price between TEC/wONE & TEC/1sDAI pools.                   |
| `trade_executor.py`                | Build swap transactions using SwapRouter02; apply slippage, check gas.    |
| `cooldown_manager.py`              | Manage in-memory cooldown timers per bot.                                 |
| `db.py`                            | SQLite logging: trades, deposits, sales, profit/loss.                     |
| `monitor.py`                       | Node health, IP changes, gas price cap, withdraw detection.               |
| `alert.py`                         | Telegram alerts: deposits, trades, errors, IP changes.                    |
| `main.py`                          | Entrypoint: runs each bot loop.                                           |




Database design (one.db per bot)

| Field               | Type       | Purpose                                                        |
| ------------------- | ---------- | -------------------------------------------------------------- |
| `id`                | INTEGER PK | Unique auto-increment ID                                       |
| `asset`             | TEXT       | Asset traded or deposited (e.g., wONE, ETH, TEC, 1sDAI, 1USDC) |
| `amount`            | REAL       | Amount deposited or acquired                                   |
| `deposit_price`     | REAL       | Price of asset at deposit or buy-back time                     |
| `sale_price`        | REAL       | Price asset was sold/swapped (if applicable)                   |
| `total_cost`        | REAL       | Total cost incl. gas etc.                                      |
| `profit`            | REAL       | Profit/loss vs last trade. Only populated when sale triggered. |
| `trade_type`        | TEXT       | ‘buy’, ‘sell’, ‘withdraw’, ‘transfer’, ‘fee\_transfer’ etc.    |
| `timestamp`         | DATETIME   | Timestamp                                                      |
| `wallet`            | TEXT       | Bot wallet address                                             |
| `status`            | TEXT       | ‘pending’, ‘sold’, etc.                                        |
| `tx_hash`           | TEXT       | Transaction hash                                               |
| `external_transfer` | INTEGER    | 0 / 1 → use 1 if record is external transfer                   |
| `external_wallet`   | TEXT       | e.g., `0x360c48a44f513b5781854588d2f1A40E90093c60`             |

📦 Shared features
- Node health monitor & retry failed RPC calls (max 5 tries).

- IP change monitor (hourly check; Telegram alert).

- Withdraw detection: pause trades after withdraw unless enough balance remains.

- Gas cap: skip trades if gas price >150 gwei.

- Cooldown manager: avoid repeated trades.

- Telegram alerts: deposits, trades, errors, IP change.

| Service                                                            | Purpose                            |
| ------------------------------------------------------------------ | ---------------------------------- |
| Harmony RPC ([https://api.s0.t.hmny.io](https://api.s0.t.hmny.io)) | Node to send TXs, get gas price.   |
| Coinbase API                                                       | Fetch ETH price.                   |
| Telegram Bot API                                                   | Alerts & notifications.            |
| Harmony `hmy` CLI                                                  | Wallets & TXs (if used on server). |

| Task                                    | Purpose                                     | Priority |
| --------------------------------------- | ------------------------------------------- | -------- |
| Key & wallet mgmt                       | Use `hmy` CLI to create wallets & sign TXs  | High     |
| Node health & IP change monitor         | Detect downtime or IP change; alert         | High     |
| SQLite logging                          | Implement .db per bot                       | High     |
| Dashboard                               | Visual query of .db data                    | Medium   |
| Build `main.py`                         | Loop & orchestrate strategy modules         | High     |
| Implement cooldown & withdraw detection | Stability                                   | High     |
| Trade executor                          | Use SwapRouter02 & apply gas/slippage logic | High     |
| Error handling                          | Retry failed TXs; alert on error            | High     |

📋 Next steps
- Confirm language & framework.

- Create folder structure & empty modules.

- Add config & secrets.

- Start with price feeds & DB logging.

- Build & test each strategy independently.

- Add monitors & alerts.

- Final end-to-end test.





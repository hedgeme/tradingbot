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

## 📦 Project Folder Structure

```text
tradingbot/
├── config.py
├── .env
├── price_feed.py
├── db.py
├── cooldown_manager.py
├── monitor.py
├── alert.py
├── trade_executor.py
├── wallet.py
├── strategy_1_eth_arbitrage.py
├── strategy_2_wone_takeprofit.py
├── strategy_3_usdc_buy_dips.py
├── strategy_4_tec_pool_arbitrage.py
├── main.py
├── SwapRouter02_minimal.json
├── requirements.txt
├── BOT_ARCHITECTURE.md
├── README.md
└── verified_info.md

🧰 Tools / Libraries to Prepare
Python 3.9+

web3 (for JSON-RPC & contracts)

requests or aiohttp (for Coinbase API)

sqlite3 or sqlalchemy

python-dotenv (load .env)

python-telegram-bot or simple webhook calls

schedule / asyncio or similar (for loops & timing)


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


Step 1
| File / Module | Purpose                                                          | Needed Before | Dependencies    |
| ------------- | ---------------------------------------------------------------- | ------------- | --------------- |
| `.env`        | Store secrets: private key(s), RPC, Telegram token, Coinbase key | Everything    | `python-dotenv` |
| `config.py`   | Load env vars; define constants (slippage, gas cap, RPC URLs)    | Everything    | `os`, `dotenv`  |


Step 2
| File / Module | Purpose                                                          | Needed Before      | Dependencies                   |
| ------------- | ---------------------------------------------------------------- | ------------------ | ------------------------------ |
| `wallet.py`   | Load private key or use Harmony CLI; get balances; sign/send txs | trade\_executor.py | `web3`, `subprocess` (for CLI) |

Step 3
| File / Module   | Purpose                                                     | Needed Before        | Dependencies       |
| --------------- | ----------------------------------------------------------- | -------------------- | ------------------ |
| `price_feed.py` | Read on-chain prices (via Web3) & Coinbase price (via REST) | strategy\_1, 2, 3, 4 | `web3`, `requests` |

Step 4
| File / Module       | Purpose                                        | Needed Before         | Dependencies |
| ------------------- | ---------------------------------------------- | --------------------- | ------------ |
| `trade_executor.py` | Build & send swap txs; apply slippage, gas cap | After wallet & config | `web3`       |

Step 5
| File / Module                      | Purpose                                          | Needed Before                   | Dependencies |
| ---------------------------------- | ------------------------------------------------ | ------------------------------- | ------------ |
| `strategy_1_eth_arbitrage.py`      | Compare Harmony vs Coinbase ETH; buy             | trade\_executor.py, price\_feed | `web3`       |
| `strategy_2_wone_takeprofit.py`    | Sell wONE after price rise; reinvest on dip      | trade\_executor.py, price\_feed |              |
| `strategy_3_usdc_buy_dips.py`      | Buy ETH/TEC on dips after USDC deposit; reinvest | trade\_executor.py, price\_feed |              |
| `strategy_4_tec_pool_arbitrage.py` | Arbitrage TEC price between pools                | trade\_executor.py, price\_feed |              |

Step 6
| File / Module         | Purpose                                                 | Needed Before | Dependencies |
| --------------------- | ------------------------------------------------------- | ------------- | ------------ |
| `cooldown_manager.py` | Track cooldowns per bot; in-memory or lightweight store | strategies    |              |

Step 7
| File / Module | Purpose                                       | Needed Before                | Dependencies       |
| ------------- | --------------------------------------------- | ---------------------------- | ------------------ |
| `db.py`       | SQLite logging: deposits, trades, profit/loss | strategies & trade\_executor | `sqlite3`          |
| `monitor.py`  | Node health, IP monitor, withdraw detection   | optional but recommended     | `requests`, `web3` |

Step 8
| File / Module | Purpose                                          | Needed Before | Dependencies                        |
| ------------- | ------------------------------------------------ | ------------- | ----------------------------------- |
| `alert.py`    | Send Telegram alerts: trades, errors, IP changes | strategies    | `requests` or `python-telegram-bot` |

Step 9
| File / Module | Purpose                                         | Needed Before     | Dependencies            |
| ------------- | ----------------------------------------------- | ----------------- | ----------------------- |
| `main.py`     | Orchestrate loops, run strategies, catch errors | after all modules | `schedule` or `asyncio` |

Dependences:
python-dotenv
web3
requests
sqlite3  # built-in with Python
schedule  # or use asyncio / APScheduler
python-telegram-bot






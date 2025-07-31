# 📦 Harmony Trading Bots - Strategy & Database Overview

This document captures the **full, final design** of four Harmony trading bots, each with its own wallet, strategy, cooldown, reinvestment logic, error handling, and shared SQLite database structure.

It is intended as a living blueprint for development, audit, and future improvements.

---

## ✅ Overview of the four Harmony bots

### 🧩 Bot #1 — ETH Price Arbitrage (Harmony vs Coinbase)
| Step | Details |
|----|------|
| Deposit detection | wONE deposit detection (≥ 200 wONE required to activate bot). New deposits record new deposit price |
| Price scan | Every 3 minutes, fetch ETH price from Harmony (via ETH/WONE pool) and Coinbase |
| Trigger | If Harmony ETH price is ≥ 5% lower than Coinbase price |
| Action | Buy ETH with 25% of available wONE |
| Post-trade | Transfer acquired ETH to external wallet `0x360c48a44f513b5781854588d2f1A40E90093c60` |
| Cooldown | Skip trades for 60 minutes after a successful trade |
| Withdraw detection | Manual withdrawal lowers balance; if still ≥ 200 wONE, continue trading; else pause |

---

### 🧩 Bot #2 — wONE Take Profit
| Step | Details |
|----|------|
| Deposit detection | wONE deposit detection (≥ 200 wONE to activate). New deposits record new deposit price |
| Price scan | Every 3 minutes |
| Trigger | If wONE price rises ≥ 5% above deposit price |
| Action | Sell 25% of available wONE for 1USDC |
| Post-trade | Send 2% of acquired 1USDC to external wallet |
| Reinvestment | If wONE price drops ≥ 5% below last sale price, buy wONE with all available 1USDC; send 2% of new wONE to external wallet |
| Cooldown | Trigger again only after new deposit or price change |
| Withdraw detection | If balance drops below 200 wONE, pause; else keep trading |

---

### 🧩 Bot #3 — USDC Acquisition & Reinvestment
| Step | Details |
|----|------|
| Deposit detection | 1USDC deposit detection (≥ 5 1USDC to activate). New deposits record price of ETH & TEC (average across pools) |
| Price scan | Every 3 minutes |
| Trigger | ETH or TEC price drops ≥ 5% from deposit price |
| Action | Use 25% of available 1USDC to buy the asset with the biggest drop |
| Post-trade | Send 2% of acquired asset to external wallet |
| Reinvestment | If asset price rises ≥ 5% above last buy price, sell all for 1USDC; send 2% of new 1USDC to external wallet |
| Cooldown | 3 minutes |
| Withdraw detection | If balance drops below 5 1USDC, pause; else keep trading |

---

### 🧩 Bot #4 — TEC Arbitrage between Pools
| Step | Details |
|----|------|
| Deposit detection | TEC deposit detection (≥ 10 TEC to activate). Capture average TEC price across TEC/WONE and TEC/1sDAI pools |
| Price scan | Every 30 minutes |
| Trigger | TEC price difference ≥ 2% between pools **and** price above deposit price by ≥ 0.25% |
| Action | Sell 25% of TEC (min 10 TEC) in higher price pool; receive wONE or 1sDAI |
| Post-trade | Send 1% of acquired asset to external wallet |
| Step 2 | Swap remaining acquired asset into other LP |
| Step 3 | Buy TEC back in the lower price pool |
| Reinvestment | After buyback, repeat after 30 min cooldown |
| Withdraw detection | If balance drops below 10 TEC, pause; else keep trading |

---

## 🛠 Shared features & safeguards
| Feature | Details |
|----|----|
| Separate wallets per bot | Keeps liquidity, logic, deposits clean |
| Deposit detection | Detect on-chain incoming txs; every new transfer is captured, records deposit price |
| Withdraw detection | After manual withdrawal, if balance is still above min → keep trading; else pause |
| Cooldown timers | Stored in memory, per bot; avoid spamming trades |
| Gas cap | Read dynamically from RPC (`eth_gasPrice`); max cap at 150 gwei |
| Error handling | Retry failed txs/RPC max 5 times; if still fails, send Telegram alert |
| Telegram reporting | All trades, deposits, withdrawals, errors |
| Reinvestment logic | Dynamic; bots can re-buy or re-sell based on strategy |
| SQLite database | Persistent trade logs for dashboards & audits |

---

## 🗂 Database: SQLite structure (per bot)
| Field               | Type       | Purpose                                                                                             |
| ------------------- | ---------- | --------------------------------------------------------------------------------------------------- |
| `id`                | INTEGER PK | Unique auto-increment ID                                                                            |
| `asset`             | TEXT       | Asset traded or deposited (wONE, ETH, TEC, 1sDAI, 1USDC)                                            |
| `amount`            | REAL       | Amount traded or deposited                                                                          |
| `deposit_price`     | REAL       | Price at deposit or buy-back                                                                        |
| `sale_price`        | REAL       | Price sold at (if applicable)                                                                       |
| `trade_type`        | TEXT       | ‘buy’, ‘sell’, ‘transfer’, ‘fee_transfer’, ‘withdraw’                                               |
| `timestamp`         | DATETIME   | When the event occurred                                                                             |
| `wallet`            | TEXT       | Bot wallet address                                                                                  |
| `status`            | TEXT       | ‘pending’, ‘sold’, etc.                                                                             |
| `tx_hash`           | TEXT       | Transaction hash                                                                                    |
| `external_transfer` | INTEGER    | 0/1 flag if sent externally                                                                         |
| `external_wallet`   | TEXT       | External destination wallet (usually `0x360c48a44f513b5781854588d2f1A40E90093c60`)                  |
| `total_trade_cost`  | REAL       | Sum incl. gas / fees (optional)                                                                     |
| `profit`            | REAL       | Net profit/loss if applicable                                                                       |

---

## ✅ Why this design works
* Clean separation per bot → predictable trading & risk control
* Clear deposit detection & reinvestment logic
* Robust error handling, Telegram alerts
* Historical reporting in SQLite → easy dashboards (Metabase / Superset)
* Dynamic gas price & cooldown → safer trading in volatile network conditions

---

## 📌 With this file, you (or future devs) can:
* Rebuild bot logic prompts
* Recreate wallets & logic
* Use as blueprint to scale or add strategies

---

**Built for**: Harmony network (chain ID: `1666600000`)  
**RPC endpoint**: `https://api.s0.t.hmny.io` (backup: `https://api.harmony.one`)

> External fee wallet: `0x360c48a44f513b5781854588d2f1A40E90093c60`

---



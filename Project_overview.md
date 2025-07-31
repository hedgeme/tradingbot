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

---

## ⚙️ Pending Development Tasks

| Task                             | Description                                                                                   | Priority |
|---------------------------------|-----------------------------------------------------------------------------------------------|----------|
| **Bot Architecture Draft**       | Document and finalize modular architecture, flow, components                                   | High     |
| **Trade Execution Logic**        | Implement swaps via SwapRouter02 ABI, multi-hop support                                       | High     |
| **Price Feeds Integration**      | On-chain price reading + Coinbase ETH price API for comparison                                | High     |
| **Cooldown Management**          | Implement cooldown timers to prevent rapid retriggers                                         | Medium   |
| **Error Handling & Retry Logic** | Robust handling for RPC/tx failures; up to 5 retries + Telegram alerts                        | High     |
| **Node Health Monitor**          | Monitor RPC health; alert on errors, retry if node fails                                      | High     |
| **IP Change Monitor**            | Hourly IP check, alert if changed                                                              | Medium   |
| **Gas Price Management**         | Dynamic gas read with cap at 150 gwei; halt trades if above                                   | High     |
| **Database Integration**         | SQLite DB per bot; record deposits, trades, profits                                           | High     |
| **Reporting Dashboard**          | CLI or web dashboard to view SQLite data                                                      | Medium   |
| **Key & Wallet Management**      | Secure storage for private keys & tx signing                                                  | High     |
| **Telegram Alerts Integration** | For trades, errors, withdrawals, IP change, idle                                              | High     |
| **Withdrawal Detection Logic**   | Pause trading on manual withdrawal; resume on new deposits if above min balance               | Medium   |
| **Bot Deployment Plan**          | Setup live deployment (systemd, Docker, etc.)                                                 | Medium   |

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

---


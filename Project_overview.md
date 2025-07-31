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
- Database schema for SQLite with comprehensive trade data fields
- Risk controls designed: max slippage (0.3%), gas price cap (150 gwei), cooldown timers
- Coinbase API integration scoped as price oracle only (no trades on Coinbase yet)
- Telegram alert design scoped for trades, errors, IP changes, idle states

---

## ⚙️ Pending Development Tasks

| Task                             | Description                                                                                   | Priority |
|---------------------------------|-----------------------------------------------------------------------------------------------|----------|
| **Bot Architecture Draft**       | Document and finalize the modular architecture, interaction flow, and components.             | High     |
| **Trade Execution Logic**        | Implement swaps using SwapRouter02 ABI including multi-hop and batch swaps                    | High     |
| **Price Feeds Integration**      | Implement on-chain price read methods and Coinbase ETH price API fetch for price comparison  | High     |
| **Cooldown Management**          | Implement cooldown timers in bot logic to avoid repeated rapid trades                        | Medium   |
| **Error Handling & Retry Logic** | Robust handling for RPC failures, transaction failures, with up to 5 retries and alerts      | High     |
| **Node Health Monitor**          | Monitor RPC node health with error detection, retry logic, and Telegram alerts               | High     |
| **IP Change Monitor**            | Hourly IP check on server, alert via Telegram if IP whitelist changes                         | Medium   |
| **Gas Price Management**         | Dynamic gas price fetching with max cap at 150 gwei, halt trades if gas exceeds threshold    | High     |
| **Database Integration**         | Implement SQLite DB usage per bot; ensure accurate recording of deposits, trades, profits    | High     |
| **Reporting Dashboard**          | Create a CLI or web dashboard to query SQLite DB and visualize trade history and status      | Medium   |
| **Key & Wallet Management**      | Secure private key storage and signing infrastructure                                        | High     |
| **Telegram Alerts Integration** | Implement alerts for trades, errors, idle states, IP changes, and withdrawal detection       | High     |
| **Withdrawal Detection Logic**   | Pause trading on manual external withdrawal; resume on new deposits if minimum balances exist| Medium   |
| **Bot Deployment Plan**          | Determine deployment method (systemd, Docker, etc.) and setup for live operation             | Medium   |

---

## 🏗️ Bot Architecture Overview (Draft)

The bot suite consists of four independent Harmony trading bots, each with:

- **Dedicated wallet** for asset segregation and independent operation
- **Deposit detection** via blockchain event monitoring for relevant assets
- **Strategy execution** according to predefined logic per bot (arbitrage, take profit, dip buying)
- **Trade execution** via SwapRouter02 contract on Harmony (Uniswap V3 fork)
- **Cooldown and state management** to prevent rapid retriggers
- **SQLite local database** per bot recording all trade, deposit, withdrawal data with profit/loss
- **Telegram alert system** for trade confirmations, error notifications, and operational monitoring
- **Off-chain ETH price source** from Coinbase API to compare prices (no trading on Coinbase currently)
- **Gas price & node health monitoring** for optimized transaction costs and reliability
- **Manual withdrawal detection** pausing bot until liquidity replenished

---

## 📝 Notes

- All core smart contract addresses and pool details are documented in `verified_info.md`.
- Detailed bot logic and strategy descriptions are in `README.md`.
- This file is intended to evolve as a live project manager tool.
- Please update task status here regularly to reflect progress and blockers.

---

If you have questions or need clarifications on any pending item, please open an issue or DM.

---

*Last updated: 2025-07-31*


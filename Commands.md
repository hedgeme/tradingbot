# TECBot Commands Guide

This guide explains how to operate TECBot via Telegram. It covers each command, what it does, expected outputs, and best practices.

> **Roles**
> - **Admin**: Can execute trades and change runtime settings.
> - **Viewer**: Can read status and reports but cannot execute.

---

## Core Health

### `/ping`
Quick health check. Returns `pong` if the bot loop and Telegram listener are alive.

### `/sanity`
Quick balance gate. Confirms minimum balances exist in each bot wallet (ONE, USDC, sDAI, TEC as applicable). If any threshold is below target, it flags ❌.

**Example**
```
/sanity
✅ tecbot_eth: ONE 250 (need ≥200)
✅ tecbot_usdc: ONE 230 (need ≥200)
✅ tecbot_sdai: USDC 6 (need ≥5)
✅ tecbot_tec: TEC 200 (need ≥10)
```

### `/balances`
Shows current token balances per wallet the bot manages.

### `/prices`
Displays tracked token prices from the internal price feed.

---

## Planning & Execution

### `/plan [all|<strategy>]`
**What it does:** Preview strategy intentions if a tick were to run now. It does not execute.

**Examples**
```
/plan
[sdai-arb] READY — would buy TEC (edge +1.9%, slip 0.25%)
[eth-arb] NOT READY — edge +0.3% < 1.0% target

/plan sdai-arb
[sdai-arb] READY — size 1,000 sDAI → ~990 TEC
```

**When to use:** Understand *why* a strategy is or isn’t about to trade.

---

### `/dryrun [all|<strategy>]`  — *tap-to-execute flow*
**What it does:** Full simulation (quote, slippage est, gas est). If a strategy is simulatable, the bot replies with an **Execute now** button for that specific strategy.

- Tapping **Execute now** **forces** that one trade using the cached plan (bypasses strategy criteria & cooldown).  
- Plans auto-expire (default 60s) and can be used **once**.

**Examples**
```
/dryrun all

[sdai-arb] ✓ DRYRUN OK
• Sim: 1,000 sDAI → ~990 TEC • Slip 0.25% • Gas ~180k
• Would broadcast: YES
• plan_id: 9f2a1c36 (valid 60s)

[eth-arb] ✗ DRYRUN FAIL
• Reason: Edge +0.3% < 1.0% target

[ Execute now (sdai-arb) ]
```

**Execution button result**
```
[sdai-arb] FORCED EXECUTION
• Using plan_id 9f2a1c36 (age 12s)
• Broadcasting: 1,000 sDAI → ~990 TEC (max slippage 0.50%)
• Tx: 0xabc123…
• Result: ✅ success
```

> **Why no `/execute` command?**  
> To prevent mistakes. Execution only happens by tapping the button after a fresh `/dryrun`.

---

## Strategy Controls

### `/disable <strategy>`  
Disable a single strategy (stops it from participating in ticks).

### `/enable <strategy>`  
Enable a single strategy.

### `/disable all`  
Disable **all** strategies at once.

### `/enable all`  
Enable **all** strategies.

**Examples**
```
/disable sdai-arb
✅ sdai-arb disabled

/disable all
✅ All strategies disabled

/enable sdai-arb
✅ sdai-arb enabled
```

---

## Cooldowns

### `/cooldowns`
Show current cooldowns per strategy and the time remaining until the next allowed trade.

### `/cooldowns set <strategy> <seconds>`  *(Admin)*
Set the cooldown for a strategy (e.g., `300` seconds).

### `/cooldowns off <strategy>`  *(Admin)*
Disable cooldown enforcement for a specific strategy.

**Examples**
```
/cooldowns
sdai-arb: 300s (next in 120s)
eth-arb: 120s (ready now)

/cooldowns set sdai-arb 300
✅ Cooldown for sdai-arb set to 300s

/cooldowns off sdai-arb
✅ Cooldown for sdai-arb disabled
```

---

## Version & Reporting

### `/version`
Shows the running version and a config checksum (detects drift in key files).

**Example**
```
/version
tecbot 0.6.0 (2025-09-23)
Config checksum: 3a9c7e11
Python 3.10.12 • PTB 13.15 • web3 6.11
```

> **Maintainer tip:** keep a `VERSION` file at repo root; update it with each change.

### `/report`
Summarizes performance for the last 24h (default). Includes **per strategy** and **cumulative** stats.

**Example**
```
/report (last 24h)

[sdai-arb]
• Trades: 3 (win 3 / loss 0)
• Volume: 3,000 sDAI
• Realized PnL: +78.40 USDC
• Gas: 0.42 ONE
• Failures: 0

[eth-arb]
• Trades: 2 (win 1 / loss 1)
• Volume: 2.2 ETH
• Realized PnL: +41.10 USDC
• Gas: 0.31 ONE
• Failures: 1 (revert at router)

———————————————
CUMULATIVE (all bots)
• Trades: 5
• Realized PnL: +119.50 USDC  (+2.1% vs start-of-day equity)
• Total Gas: 0.73 ONE
• Worst slip: 0.34%
• Max DD: -0.6%
• Current balances:
    tecbot_eth: 3.20 ETH, 250 ONE
    tecbot_usdc: 2,200 USDC
    tecbot_sdai: 5,100 sDAI
    tecbot_tec: 1,450 TEC
```

---

## Best Practices

- Always **`/plan` → `/dryrun` → tap *Execute now***.
- Re-run `/dryrun` if more than 60s pass (price drift).
- Keep **cooldowns** enabled to avoid pool hammering.
- Use **`/disable`** a misbehaving strategy instead of stopping the whole bot.
- Check **`/version`** after updates to confirm the right build.
- Review **`/report`** daily to evaluate performance and slippage.

---

## Troubleshooting

- **No button under `/dryrun`** → simulation failed; read the reason line.
- **“Plan expired” on tap** → redo `/dryrun`.
- **“Admin only”** → your Telegram ID is not whitelisted.
- **Silent chat** → check service logs:
  ```
  journalctl -u tecbot-telegram -f
  ```
- **Balances missing** → re-check `.env` permissions and RPC connectivity.

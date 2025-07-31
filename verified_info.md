# ✅ VERIFIED_INFO.md

## 📦 Contracts & Assets used by the Harmony trading bot

| Asset        | Symbol | Contract Address                                      |
| ------------ | ------ | ---------------------------------------------------- |
| Wrapped ONE  | WONE   | `0xcF664087a5bB0237a0BAd6742852ec6c8d69A27a`         |
| ETH (1ETH)   | 1ETH   | `0x4cc435d7b9557d54d6ef02d69bbf72634905bf11`         |
| USD Coin     | 1USDC  | `0xbc594cabd205bd993e7ffa6f3e9cea75c1110da5`         |
| TEC          | TEC    | `0x0deb9a1998aae32daacf6de21161c3e942ace074`         |
| 1sDAI        | 1sDAI  | `0xedeb95d51dbc4116039435379bd58472a2c09b1f`         |

---

## 🏊 Verified Pools & Fee Tiers

| Pool             | Address                                      | Fee    |
| ---------------- | -------------------------------------------- | ----- |
| 1ETH / WONE      | `0xe0566c122bdbb29beb5ff2148a6a547df814a246` | 0.3%  |
| 1USDC / WONE     | `0x6e543b707693492a2d14d729ac10a9d03b4c9383` | 0.3%  |
| TEC / WONE       | `0xfac981a64ecedf1be8722125fe776bde2f746ff2` | 1%    |
| 1USDC / 1sDAI    | `0xc28f4b97aa9a983da81326f7fb4b9cf84a9703a2` | 0.05% |
| TEC / 1sDAI      | `0x90bfca0ee66ca53cddfc0f6ee5217b6f2acde4ee` | 1%    |

---

## 🔗 Swap Router contract

| Name          | Address                                      |
| ------------- | -------------------------------------------- |
| SwapRouter02  | `0x85495f44768ccbb584d9380Cc29149fDAA445F69` |

---

## 📜 ABI Reference

✅ We are using the **pruned** ABI saved in the repo:  
[`SwapRouter02_minimal.json`](https://github.com/hedgeme/tradingbot/blob/main/SwapRouter02_minimal.json)

> This minimal ABI keeps only trading functions:
> - single-hop & multi-hop swaps
> - batch transactions via multicall
> - unwrap WONE → ONE
> - exact input/output swaps
>
> ⚠ The full ABI also exists (not used by bot, kept for reference).  
> Keeping the minimal ABI keeps code lighter, safer, and easier to maintain.

---

## 🌐 Network Info

| Item              | Value                                    |
| ----------------- | ---------------------------------------- |
| Harmony Chain ID  | `1666600000`                             |
| Primary RPC       | `https://api.s0.t.hmny.io`               |
| Backup RPC        | `https://api.harmony.one`                |

---

## ⚙️ Bot settings & parameters

| Setting            | Value                                                   |
| ----------------- | ------------------------------------------------------ |
| Max slippage      | 0.3%                                                   |
| Gas price cap     | 150 gwei                                               |
| Telegram alerts   | Enabled: tx confirmations & errors                     |
| Cooldown timers   | Stored after each trade (in-memory or lightweight db)  |
| Withdraw detection| Detect manual withdraw; pause trading if balance < min |
| Database          | One SQLite `.db` per bot strategy                      |

---

✅ **This file is the verified reference**  
for contract addresses, pools, fees, ABI, and operational parameters  
needed to recreate or redeploy the Harmony trading bot at any time.

# ✅ VERIFIED_INFO.md

## 📦 Assets used by the Harmony trading bot

This document lists verified contracts used by TECBot on Harmony (Shard 0).  
Keep this file updated when adding or upgrading contracts.

| Asset        | Symbol | Contract Address                                      |
| ------------ | ------ | ---------------------------------------------------- |
| Wrapped ONE  | WONE   | `0xcF664087a5bB0237a0BAd6742852ec6c8d69A27a`         |
| ETH (1ETH)   | 1ETH   | `0x4cc435d7b9557d54d6ef02d69bbf72634905bf11`         |
| USD Coin     | 1USDC  | `0xbc594cabd205bd993e7ffa6f3e9cea75c1110da5`         |
| TEC          | TEC    | `0x0deb9a1998aae32daacf6de21161c3e942ace074`         |
| 1sDAI        | 1sDAI  | `0xedeb95d51dbc4116039435379bd58472a2c09b1f`         |

---

## 🏊 Verified Liquidity Pools & Fee Tiers

| Pool             | Address                                      | Fee    |
| ---------------- | -------------------------------------------- | ----- |
| 1ETH / WONE      | `0xe0566c122bdbb29beb5ff2148a6a547df814a246` | 0.3%  |
| 1USDC / WONE     | `0x6e543b707693492a2d14d729ac10a9d03b4c9383` | 0.3%  |
| TEC / WONE       | `0xfac981a64ecedf1be8722125fe776bde2f746ff2` | 1%    |
| 1USDC / 1sDAI    | `0xc28f4b97aa9a983da81326f7fb4b9cf84a9703a2` | 0.05% |
| TEC / 1sDAI      | `0x90bfca0ee66ca53cddfc0f6ee5217b6f2acde4ee` | 1%    |

---

## 🛠️ Verified Core & Periphery Contracts

These are **official Uniswap V3 contracts**, deployed on Harmony:

| Contract | Address | Notes |
|----------|---------|-------|
| Factory V3 | `0x12d21f5d0Ab768c312E19653Bf3f89917866B8e8` | Pool factory |
| SwapRouter02 | `0x85495f44768ccbb584d9380Cc29149fDAA445F69` | Primary router |
| TickLens | `0x2D7B3ae07fE5E1d9da7c2C79F953339D0450a017` | Tick data helper |
| NonfungiblePositionManager | `0xE4E259BE9c84260FDC7C9a3629A0410b1Fb3C114` | LP NFT positions |
| **QuoterV2** | `0x314456E8F5efaa3dD1F036eD5900508da8A3B382` | Used for on-chain quoting |


**Official contract repositories** (for verification & upgrades):
- [Uniswap v3-core](https://github.com/Uniswap/v3-core)
- [Uniswap v3-periphery](https://github.com/Uniswap/v3-periphery)
- [Uniswap swap-router-contracts](https://github.com/Uniswap/swap-router-contracts)

Contracts were deployed **without modification** to official Uniswap code.

## Notes

- All addresses are checksummed (`Web3.toChecksumAddress`).
- TECBot uses **QuoterV2** for quoting V3 swaps (single and multi-hop).
- Keep this file in sync with deployed contracts — mismatches here will cause failed trades.
- When adding a new token, ensure decimals are verified on-chain and update fallback mapping in `/bot/app/trade_executor.py`.

---

## 📜 ABI Reference

✅ We use a **pruned minimal ABI** to keep the bot lightweight:
[`SwapRouter02_minimal.json`](https://github.com/hedgeme/tradingbot/blob/main/SwapRouter02_minimal.json)

> Contains only:
> - single & multi-hop swaps
> - batch transactions via multicall
> - unwrap WONE → ONE
> - exact input/output swaps

The full ABI exists separately if needed (not used by bot directly).

**SwapRouter02 on Harmony is compiled with IV3SwapRouter.ExactInputParams
(bytes path, address recipient, uint256 amountIn, uint256 amountOutMinimum)**

Verified core and most of the periphery contracts in the new explorer:
- FactoryV3: 0x12d21f5d0Ab768c312E19653Bf3f89917866B8e8 (https://explorer.harmony.one/address/0x12d21f5d0Ab768c312E19653Bf3f89917866B8e8)
- TickLens: 0x2D7B3ae07fE5E1d9da7c2C79F953339D0450a017 (https://explorer.harmony.one/address/0x2d7b3ae07fe5e1d9da7c2c79f953339d0450a017)
- NonfungiblePositionManager: 0xE4E259BE9c84260FDC7C9a3629A0410b1Fb3C114 (https://explorer.harmony.one/address/0xE4E259BE9c84260FDC7C9a3629A0410b1Fb3C114)
- SwapRouter02: 0x85495f44768ccbb584d9380Cc29149fDAA445F69 (https://explorer.harmony.one/address/0x85495f44768ccbb584d9380Cc29149fDAA445F69) 

Contracts were deployed using official Uniswap code, nothing was changed in it.
Main contract repositories: 
https://github.com/Uniswap/v3-core 
https://github.com/Uniswap/v3-periphery 
https://github.com/Uniswap/swap-router-contracts

---

## 🌐 Network Info

| Item              | Value                                    |
| ----------------- | ---------------------------------------- |
| Harmony Chain ID  | `1666600000`                             |
| Primary RPC       | `https://api.s0.t.hmny.io`               |
| Backup RPC        | `https://api.harmony.one`                |

---

## ⚙️ Bot Settings & Risk Controls

| Setting                | Value                                                   |
| --------------------- | ------------------------------------------------------ |
| Max slippage          | 0.3%                                                   |
| Gas price cap         | 150 gwei                                               |
| Cooldown timers       | After trade; stored in-memory or lightweight db        |
| Withdraw detection    | Manual withdrawals detected; trading pauses if below min |
| Telegram alerts      | Enabled for tx confirmations & errors                   |
| Database             | One SQLite `.db` per bot strategy; keeps detailed trade logs |

---


✅ **This file keeps track of all verified contracts, addresses, pools, ABIs, and bot settings**  
to ensure transparency, reproducibility, and easy redeployment.


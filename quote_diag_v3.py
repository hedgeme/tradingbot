# /bot/app/quote_diag_v3.py
# -*- coding: utf-8 -*-
"""
Harmony V3 fork diagnostics:
- Verifies pools via FactoryV3.getPool(tokenA, tokenB, fee)
- Quotes with QuoterV2.quoteExactInputSingle (struct), chaining for multihop

Run:
  source ~/tecbot-venv/bin/activate
  python /bot/app/quote_diag_v3.py

Optional: to test the other RPC quickly (used by your earlier PASS),
set environment before running:
  export HMY_RPC=https://api.s0.t.hmny.io
"""

from __future__ import annotations
from typing import Dict, Any, List, Optional, Tuple
from decimal import Decimal
import os, json, sys

def _imp(modname: str):
    try:
        return __import__(modname, fromlist=['*'])
    except Exception:
        return __import__(f"app.{modname}", fromlist=['*'])

config = _imp("config")
try:
    wallet = _imp("wallet")
except Exception:
    wallet = None

from web3 import Web3, HTTPProvider

HTTP_TIMEOUT = 5

# Uniswap V3-like addresses from your repo (verified_info.md)
FACTORY_V3 = Web3.to_checksum_address("0x12d21f5d0Ab768c312E19653Bf3f89917866B8e8")
QUOTER_V2  = Web3.to_checksum_address("0x314456E8F5efaa3dD1F036eD5900508da8A3B382")

# ABIs
FACTORY_V3_ABI = [
    {
        "inputs": [
            {"internalType":"address","name":"tokenA","type":"address"},
            {"internalType":"address","name":"tokenB","type":"address"},
            {"internalType":"uint24","name":"fee","type":"uint24"}
        ],
        "name":"getPool",
        "outputs":[{"internalType":"address","name":"pool","type":"address"}],
        "stateMutability":"view",
        "type":"function"
    }
]

# Some forks expose only quoteExactInputSingle with a struct parameter.
# struct QuoteExactInputSingleParams {
#   address tokenIn;
#   address tokenOut;
#   uint256 amountIn;
#   uint24 fee;
#   uint160 sqrtPriceLimitX96; // set to 0 for no limit
# }
QUOTER_V2_ABI_SINGLE = [
    {
        "inputs":[
            {
                "components":[
                    {"internalType":"address","name":"tokenIn","type":"address"},
                    {"internalType":"address","name":"tokenOut","type":"address"},
                    {"internalType":"uint256","name":"amountIn","type":"uint256"},
                    {"internalType":"uint24","name":"fee","type":"uint24"},
                    {"internalType":"uint160","name":"sqrtPriceLimitX96","type":"uint160"}
                ],
                "internalType":"struct IQuoterV2.QuoteExactInputSingleParams",
                "name":"params",
                "type":"tuple"
            }
        ],
        "name":"quoteExactInputSingle",
        "outputs":[
            {"internalType":"uint256","name":"amountOut","type":"uint256"},
            {"internalType":"uint160","name":"sqrtPriceX96After","type":"uint160"},
            {"internalType":"uint32","name":"initializedTicksCrossed","type":"uint32"},
            {"internalType":"uint256","name":"gasEstimate","type":"uint256"}
        ],
        "stateMutability":"nonpayable",
        "type":"function"
    }
]

def _w3() -> Web3:
    rpc = os.environ.get("HMY_RPC") or getattr(config, "RPC_URL", "https://api.harmony.one")
    if wallet and hasattr(wallet, "get_w3"):
        # let wallet get_w3 decide (it may also change endpoint)
        return wallet.get_w3()
    return Web3(HTTPProvider(rpc, request_kwargs={"timeout": HTTP_TIMEOUT}))

def _tok(sym: str) -> str:
    addr = getattr(config, "TOKENS", {}).get(sym)
    if not addr:
        raise RuntimeError(f"config.TOKENS missing {sym}")
    return Web3.to_checksum_address(addr)

def _dec(sym: str) -> int:
    decs = getattr(config, "DECIMALS", {})
    return int(decs.get(sym, 6 if sym == "1USDC" else 18))

def factory_get_pool(tokenA: str, tokenB: str, fee: int) -> str:
    w3 = _w3()
    f = w3.eth.contract(address=FACTORY_V3, abi=FACTORY_V3_ABI)
    # Factory uses token order: token0 < token1 (by address), but getPool sorts internally.
    return f.functions.getPool(tokenA, tokenB, int(fee)).call()

def quote_single(tokenIn: str, tokenOut: str, fee: int, amount_in: int) -> Tuple[Optional[int], str]:
    w3 = _w3()
    q = w3.eth.contract(address=QUOTER_V2, abi=QUOTER_V2_ABI_SINGLE)
    params = (tokenIn, tokenOut, int(amount_in), int(fee), 0)  # sqrtPriceLimitX96=0
    try:
        out, _, _, _ = q.functions.quoteExactInputSingle(params).call()
        return int(out), ""
    except Exception as e:
        return None, str(e)

def usd_per_one(sym: str) -> None:
    print(f"\n=== {sym} ===")
    WONE  = _tok("WONE")
    USDC  = _tok("1USDC")

    if sym == "ONE":
        # ONE price == 1 WONE -> USDC
        hops = [(WONE, USDC, 500)]
        amount_in = 10 ** _dec("WONE")

    elif sym == "1ETH":
        ETH = _tok("1ETH")
        # 1ETH -> WONE (3000) -> USDC (500)
        hops = [(ETH, WONE, 3000), (WONE, USDC, 500)]
        amount_in = 10 ** _dec("1ETH")

    elif sym == "TEC":
        TEC = _tok("TEC")
        hops = [(TEC, WONE, 10000), (WONE, USDC, 500)]
        amount_in = 10 ** _dec("TEC")

    elif sym == "1sDAI":
        SDAI = _tok("1sDAI")
        # direct first, then fallback through WONE if needed
        hops = [(SDAI, USDC, 500)]
        amount_in = 10 ** _dec("1sDAI")

    elif sym == "1USDC":
        print("1USDC := $1.00")
        return

    else:
        print(f"{sym} not supported.")
        return

    # 1) Check pools exist
    print("Pools (Factory.getPool):")
    for (a, b, fee) in hops:
        try:
            pool = factory_get_pool(a, b, fee)
            print(f"  {a} -> {b} fee {fee}: {pool}")
        except Exception as e:
            print(f"  {a} -> {b} fee {fee}: ERROR getPool: {e}")

    # 2) Quote via single-hop(s)
    amt = amount_in
    print(f"\nStart amountIn (raw): {amt}")
    for (a, b, fee) in hops:
        out, err = quote_single(a, b, fee, amt)
        if out is None:
            print(f"  QUOTE FAIL {a} -> {b} fee {fee}: {err[:300]}{'...' if len(err)>300 else ''}")
            return
        print(f"  QUOTE OK   {a} -> {b} fee {fee}: out={out}")
        amt = out  # chain for multihop

    usd = Decimal(amt) / Decimal(10 ** _dec("1USDC"))
    print(f"\nPRICE: 1 {sym} = ${usd:.6f} (via {len(hops)} hop{'s' if len(hops)>1 else ''})")

def main():
    w3 = _w3()
    print("RPC:", w3.provider.endpoint_uri)
    try:
        print("Chain ID:", w3.eth.chain_id, "Block:", w3.eth.block_number)
    except Exception as e:
        print("RPC ERROR:", e)
        sys.exit(1)

    print("\nFACTORY_V3:", FACTORY_V3)
    print("QUOTER_V2 :", QUOTER_V2)

    print("\nTOKENS:", getattr(config, "TOKENS", {}))
    print("DECIMALS:", getattr(config, "DECIMALS", {}))

    for s in ["ONE", "1USDC", "1sDAI", "TEC", "1ETH"]:
        usd_per_one(s)

if __name__ == "__main__":
    main()

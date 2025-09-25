# /bot/app/quote_diag.py
# -*- coding: utf-8 -*-
"""
QuoterV2 price diagnostics for Harmony (Uniswap V3)

What this does:
- Loads token addresses/decimals from app.config
- Builds Web3 with a short timeout from config.RPC_URL
- Quotes via QuoterV2:
    ONE   -> 1USDC      (primary: 500; fallback: 3000)
    1ETH  -> WONE -> USDC  (primary: 3000,500; fallback: 10000,500)
    TEC   -> WONE -> USDC  (primary: 10000,500; fallback: 3000,500)
    1sDAI -> 1USDC      (primary: 500; fallback: 3000)
- Prints addresses, fee path, encoded path (hex), amountOut raw, and USD price
- Also fetches Coinbase ETH spot for side-by-side comparison

Run:
    source ~/tecbot-venv/bin/activate
    python /bot/app/quote_diag.py
"""

from __future__ import annotations
from typing import List, Tuple, Optional
from decimal import Decimal
import json
import sys
import time

# import app.config and wallet using the "app." prefix (so PYTHONPATH isn't needed in shell)
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
from urllib.request import urlopen, Request

# ---------- constants ----------
HTTP_TIMEOUT = 4
QUOTER_V2 = Web3.to_checksum_address("0x314456E8F5efaa3dD1F036eD5900508da8A3B382")

QUOTER_V2_ABI = [
    {
        "inputs": [
            {"internalType": "bytes", "name": "path", "type": "bytes"},
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
        ],
        "name": "quoteExactInput",
        "outputs": [
            {"internalType": "uint256", "name": "amountOut", "type": "uint256"},
            {"internalType": "uint160[]", "name": "sqrtPriceX96AfterList", "type": "uint160[]"},
            {"internalType": "uint32[]",  "name": "initializedTicksCrossedList", "type": "uint32[]"},
            {"internalType": "uint256", "name": "gasEstimate", "type": "uint256"},
        ],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

# ---------- helpers ----------
def w3() -> Web3:
    if wallet and hasattr(wallet, "get_w3"):
        return wallet.get_w3()
    rpc = getattr(config, "RPC_URL", "https://api.harmony.one")
    return Web3(HTTPProvider(rpc, request_kwargs={"timeout": HTTP_TIMEOUT}))

def tok(sym: str) -> str:
    addr = getattr(config, "TOKENS", {}).get(sym)
    if not addr:
        raise RuntimeError(f"config.TOKENS missing symbol {sym}")
    return Web3.to_checksum_address(addr)

def dec(sym: str) -> int:
    decs = getattr(config, "DECIMALS", {})
    return int(decs.get(sym, 6 if sym == "1USDC" else 18))

def encode_path(tokens: List[str], fees: List[int]) -> bytes:
    """
    Uniswap V3 path encoding: address(20) + fee(3) + address(20) [+ fee + address ...]
    tokens: [tokenIn, mid, ..., tokenOut]
    fees:   [fee between tokens[0]->tokens[1], ..., between tokens[-2]->tokens[-1]]
    """
    if len(tokens) < 2 or len(fees) != len(tokens) - 1:
        raise ValueError("encode_path: mismatched tokens/fees")
    out = b""
    for i in range(len(tokens) - 1):
        out += bytes.fromhex(tokens[i][2:].lower())      # address
        out += int(fees[i]).to_bytes(3, "big")           # fee uint24
    out += bytes.fromhex(tokens[-1][2:].lower())         # last address
    return out

def coinbase_eth_spot() -> Optional[float]:
    try:
        req = Request("https://api.coinbase.com/v2/prices/ETH-USD/spot", headers={"User-Agent":"tecbot/diag"})
        with urlopen(req, timeout=3) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        amt = d.get("data", {}).get("amount")
        return float(amt) if amt else None
    except Exception:
        return None

def quote_exact_input(path_tokens: List[str], fees: List[int], amount_in: int) -> Tuple[Optional[int], str]:
    """
    Calls QuoterV2.quoteExactInput. Returns (amountOut, error_msg).
    error_msg is "" if success.
    """
    try:
        c = w3().eth.contract(address=QUOTER_V2, abi=QUOTER_V2_ABI)
        path = encode_path(path_tokens, fees)
        out, _, _, _ = c.functions.quoteExactInput(path, int(amount_in)).call()
        return int(out), ""
    except Exception as e:
        return None, str(e)

def try_paths_for(sym: str) -> None:
    print(f"\n=== {sym} ===")
    # Build canonical + fallback paths
    WONE = tok("WONE")
    USDC = tok("1USDC")

    if sym == "1USDC":
        print("1USDC assumed = $1.00 by definition.")
        return

    if sym == "ONE":
        amount_in = 10 ** dec("WONE")
        candidates = [
            ([WONE, USDC], [500], "WONE->USDC @500"),
            ([WONE, USDC], [3000], "WONE->USDC @3000"),
        ]
    elif sym == "1ETH":
        ETH = tok("1ETH")
        amount_in = 10 ** dec("1ETH")
        candidates = [
            ([ETH, WONE, USDC], [3000, 500], "1ETH->WONE @3000 -> USDC @500"),
            ([ETH, WONE, USDC], [10000, 500], "1ETH->WONE @10000 -> USDC @500"),
            ([ETH, USDC], [3000], "1ETH->USDC @3000 (direct)"),
        ]
    elif sym == "TEC":
        TEC = tok("TEC")
        amount_in = 10 ** dec("TEC")
        candidates = [
            ([TEC, WONE, USDC], [10000, 500], "TEC->WONE @10000 -> USDC @500"),
            ([TEC, WONE, USDC], [3000, 500],  "TEC->WONE @3000  -> USDC @500"),
            ([TEC, USDC], [3000],             "TEC->USDC @3000 (direct)"),
        ]
    elif sym == "1sDAI":
        SDAI = tok("1sDAI")
        amount_in = 10 ** dec("1sDAI")
        candidates = [
            ([SDAI, USDC], [500],  "1sDAI->USDC @500"),
            ([SDAI, USDC], [3000], "1sDAI->USDC @3000"),
            ([SDAI, WONE, USDC], [500, 500], "1sDAI->WONE @500 -> USDC @500"),
        ]
    else:
        print(f"{sym}: not supported in diag.")
        return

    # Print token addresses
    print("Tokens:")
    try:
        print("  WONE :", WONE)
    except Exception:
        pass
    if sym in ("1ETH",):
        print("  1ETH :", tok("1ETH"))
    if sym in ("TEC",):
        print("  TEC  :", tok("TEC"))
    if sym in ("1sDAI",):
        print("  1sDAI:", tok("1sDAI"))
    print("  1USDC:", USDC)
    print("Decimals:", {s: dec(s) for s in ["WONE","1ETH","TEC","1sDAI","1USDC"] if s in getattr(config,"TOKENS",{})})

    # Try each candidate path
    for tokens, fees, label in candidates:
        path_hex = encode_path(tokens, fees).hex()
        print(f"\nTrying path: {label}")
        print("  addresses:", " -> ".join(tokens))
        print("  fees     :", fees)
        print(f"  path(hex): 0x{path_hex}")
        print(f"  amountIn : {amount_in} (1 * 10^{dec(sym if sym!='ONE' else 'WONE')})")
        out, err = quote_exact_input(tokens, fees, amount_in)
        if out is None:
            print("  RESULT   : FAIL")
            print("  error    :", err[:300] + ("..." if len(err) > 300 else ""))
        else:
            usd = Decimal(out) / Decimal(10 ** dec("1USDC"))
            print("  RESULT   : OK")
            print("  amountOut:", out, f"(USDC units, 10^{dec('1USDC')})")
            print(f"  PRICE    : ${usd:.6f} per 1 {sym}")
            return  # stop on first success

    print("  => All candidate paths failed for", sym)

def main():
    print("RPC_URL:", getattr(config, "RPC_URL", None))
    print("CHAIN_ID:", getattr(config, "CHAIN_ID", None))
    print("TOKENS:", getattr(config, "TOKENS", {}))
    print("DECIMALS:", getattr(config, "DECIMALS", {}))
    print("\nQuoterV2:", QUOTER_V2)
    print("----")

    # Ping chain
    _w3 = w3()
    try:
        print("Chain ID:", _w3.eth.chain_id, " Latest block:", _w3.eth.block_number)
    except Exception as e:
        print("RPC ERROR:", e)
        sys.exit(1)

    # Coinbase spot
    cb = coinbase_eth_spot()
    print("\nCoinbase ETH spot:", cb if cb is not None else "N/A")

    # Run diagnostics
    for s in ["ONE", "1USDC", "1sDAI", "TEC", "1ETH"]:
        try_paths_for(s)

if __name__ == "__main__":
    main()

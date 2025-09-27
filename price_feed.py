# /bot/app/price_feed.py
# -*- coding: utf-8 -*-
"""
Price feed using Uniswap V3 QuoterV2 on Harmony + Coinbase ETH spot.

Symbols:
  ONE, 1USDC, 1sDAI, TEC, 1ETH

Rules:
  - 1USDC is 1.00 by definition.
  - 1sDAI is QUOTED via QuoterV2 (NOT hard-coded).
  - ONE priced via WONE/ONE -> 1USDC single-hop (fee 500).
  - 1ETH priced via 1ETH->WONE/ONE (3000) -> 1USDC (500) multihop.
  - TEC  priced via TEC->WONE/ONE (10000) -> 1USDC (500) multihop.
  - Coinbase ETH spot fetched via public endpoint (3s timeout).
"""

from __future__ import annotations
from typing import Dict, Any, List, Optional
import json
import logging
from decimal import Decimal
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

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

from web3 import Web3

log = logging.getLogger("price_feed")
log.setLevel(logging.INFO)

# ----------------- Uniswap V3 constants -----------------
# Use the address from config (do NOT hardcode), defaults to the verified one if missing.
QUOTER_V2 = Web3.to_checksum_address(
    getattr(config, "QUOTER_ADDR", "0x314456E8F5efaa3dD1F036eD5900508da8A3B382")
)

# Harmony RPC preference: use config.HARMONY_RPC if available
def _w3() -> Web3:
    if wallet and hasattr(wallet, "get_w3"):
        return wallet.get_w3()
    rpc = getattr(config, "HARMONY_RPC", None) or getattr(config, "RPC_URL", "https://api.harmony.one")
    return Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 8}))

# Minimal ABI for your QuoterV2 variant: quoteExactInput(bytes path, uint256 amountIn)
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

# ----------------- Helpers -----------------
def _tok(sym: str) -> str:
    """
    Return a checksum address for the given symbol.
    Special handling: 'WONE' falls back to 'ONE' and vice versa.
    """
    toks = getattr(config, "TOKENS", {}) or {}
    addr = toks.get(sym)
    if not addr:
        if sym == "WONE" and "ONE" in toks:
            addr = toks["ONE"]
        elif sym == "ONE" and "WONE" in toks:
            addr = toks["WONE"]
    if not addr:
        raise RuntimeError(f"config.TOKENS missing symbol {sym} (and no alias fallback)")
    return Web3.to_checksum_address(addr)

def _dec(sym: str) -> int:
    """
    Decimals map (optional). Defaults: 1USDC=6, others=18.
    """
    decs = getattr(config, "DECIMALS", {}) or {}
    if sym in decs:
        return int(decs[sym])
    if sym == "1USDC":
        return 6
    return 18

def _encode_path(tokens: List[str], fees: List[int]) -> bytes:
    """
    Uniswap V3 path encoding: address(20) + fee(3) + address(20) [+ fee + address ...]
    tokens: list of checksum addresses, e.g. [tokenIn, mid, tokenOut]
    fees:   list of fee tiers between hops, e.g. [3000, 500]
    """
    if len(tokens) < 2 or len(fees) != len(tokens) - 1:
        raise ValueError("encode_path: mismatched tokens/fees")
    out = b""
    for i in range(len(tokens) - 1):
        out += bytes.fromhex(tokens[i][2:].lower())           # 20 bytes
        out += int(fees[i]).to_bytes(3, byteorder="big")      # 3 bytes
    out += bytes.fromhex(tokens[-1][2:].lower())              # last token (20 bytes)
    return out

def _quote_usd_for_1_token(sym: str, errors: List[str]) -> Optional[Decimal]:
    """
    Returns USD value for 1.0 unit of `sym` using your QuoterV2 variant on Harmony.
    1USDC = 1.0 (fixed), 1sDAI is quoted.
    """
    if sym == "1USDC":
        return Decimal("1.0")

    w3 = _w3()
    q = w3.eth.contract(address=QUOTER_V2, abi=QUOTER_V2_ABI)

    WONE  = _tok("WONE")
    ONE   = _tok("ONE")   # resolved to same address as WONE if only one exists
    USDC  = _tok("1USDC")

    if sym == "ONE":
        amt_in = 10 ** _dec("WONE")
        path = _encode_path([WONE, USDC], [500])

    elif sym == "1ETH":
        ETH = _tok("1ETH")
        amt_in = 10 ** _dec("1ETH")
        path = _encode_path([ETH, WONE, USDC], [3000, 500])

    elif sym == "TEC":
        TEC = _tok("TEC")
        amt_in = 10 ** _dec("TEC")
        path = _encode_path([TEC, WONE, USDC], [10000, 500])

    elif sym == "1sDAI":
        SDAI = _tok("1sDAI")
        amt_in = 10 ** _dec("1sDAI")
        path = _encode_path([SDAI, USDC], [500])

    else:
        errors.append(f"{sym}: unsupported symbol in price_feed")
        return None

    try:
        amount_out, _, _, _ = q.functions.quoteExactInput(path, int(amt_in)).call()
        usd = Decimal(amount_out) / Decimal(10 ** _dec("1USDC"))  # USDC has 6 decimals
        return usd
    except Exception as e:
        errors.append(f"{sym}: QuoterV2 quote failed ({e})")
        return None

def _coinbase_eth_usd(errors: List[str]) -> Optional[float]:
    url = "https://api.coinbase.com/v2/prices/ETH-USD/spot"
    try:
        req = Request(url, headers={"User-Agent": "tecbot/1.0"})
        with urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        amt = data.get("data", {}).get("amount")
        return float(amt) if amt is not None else None
    except (URLError, HTTPError) as e:
        errors.append(f"Coinbase spot: network error ({e})")
    except Exception as e:
        errors.append(f"Coinbase spot: {e}")
    return None

# ----------------- Public API -----------------
def get_eth_prices_lp_vs_cb() -> Dict[str, float]:
    errors: List[str] = []
    lp = None
    try:
        v = _quote_usd_for_1_token("1ETH", errors)
        if v is not None:
            lp = float(v)
    except Exception as e:
        errors.append(f"LP ETH quote: {e}")

    cb = _coinbase_eth_usd(errors)
    diff = float("nan")
    if lp is not None and cb is not None and cb != 0.0:
        diff = (cb - lp) / cb * 100.0
    return {"lp_eth_usd": lp if lp is not None else float("nan"),
            "cb_eth_usd": cb if cb is not None else float("nan"),
            "diff_pct": diff,
            "errors": errors}

def get_prices() -> Dict[str, Any]:
    out: Dict[str, Any] = {"errors": []}
    for sym in ["ONE", "1USDC", "1sDAI", "TEC", "1ETH"]:
        try:
            v = _quote_usd_for_1_token(sym, out["errors"])
            out[sym] = float(v) if v is not None else None
        except Exception as e:
            out[sym] = None
            out["errors"].append(f"{sym}: {e}")

    cmp_ = get_eth_prices_lp_vs_cb()
    if cmp_.get("errors"):
        out["errors"].extend(cmp_["errors"])
    out["ETH_COMPARE"] = {k: cmp_[k] for k in ("lp_eth_usd","cb_eth_usd","diff_pct")}
    return out

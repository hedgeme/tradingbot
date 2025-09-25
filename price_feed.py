# /bot/app/price_feed.py
# -*- coding: utf-8 -*-
"""
Price feed using Uniswap V3 QuoterV2 on Harmony + Coinbase ETH spot.

Symbols:
  ONE, 1USDC, 1sDAI, TEC, 1ETH

Rules:
  - 1USDC and 1sDAI are 1.00 by definition.
  - ONE priced via WONE->1USDC single-hop (fee 500).
  - 1ETH priced via 1ETH->WONE (3000) -> 1USDC (500) multihop.
  - TEC  priced via TEC->WONE (10000) -> 1USDC (500) multihop.
  - Coinbase ETH spot fetched via public endpoint (3s timeout).

Addresses (from verified_info.md):
  SwapRouter02: 0x85495f44768ccbb584d9380Cc29149fDAA445F69  (not used here)
  QuoterV2:     0x314456E8F5efaa3dD1F036eD5900508da8A3B382
"""

from __future__ import annotations
from typing import Dict, Any, List, Optional
import json
import logging
from decimal import Decimal
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

# local imports (flat or app.*)
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
QUOTER_V2 = Web3.to_checksum_address("0x314456E8F5efaa3dD1F036eD5900508da8A3B382")

# Minimal ABI for QuoterV2
# We only need quoteExactInput(bytes path, uint256 amountIn) -> (uint256 amountOut, ...extras)
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
def _w3() -> Web3:
    if wallet and hasattr(wallet, "get_w3"):
        return wallet.get_w3()
    return Web3(Web3.HTTPProvider(getattr(config, "RPC_URL", "https://api.harmony.one")))

def _tok(sym: str) -> str:
    addr = getattr(config, "TOKENS", {}).get(sym)
    if not addr:
        raise RuntimeError(f"config.TOKENS missing symbol {sym}")
    return Web3.to_checksum_address(addr)

def _dec(sym: str) -> int:
    decs = getattr(config, "DECIMALS", {})
    return int(decs.get(sym, 6 if sym == "1USDC" else 18))

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
    Returns USD value for 1.0 unit of `sym` using QuoterV2 on Harmony.
    ONE is priced via WONE.
    Stables (1USDC, 1sDAI) return 1.0.
    """
    if sym in ("1USDC", "1sDAI"):
        return Decimal("1.0")

    w3 = _w3()
    q = w3.eth.contract(address=QUOTER_V2, abi=QUOTER_V2_ABI)

    # Map symbols to concrete multihops and fee tiers
    # (based on your working preflight output)
    WONE  = _tok("WONE")
    USDC  = _tok("1USDC")

    if sym == "ONE":
        # Price 1 WONE → USDC (single hop 500)
        amt_in = 10 ** _dec("WONE")
        path = _encode_path([WONE, USDC], [500])
    elif sym == "1ETH":
        ETH = _tok("1ETH")
        amt_in = 10 ** _dec("1ETH")
        # 1ETH -> WONE (3000) -> 1USDC (500)
        path = _encode_path([ETH, WONE, USDC], [3000, 500])
    elif sym == "TEC":
        TEC = _tok("TEC")
        amt_in = 10 ** _dec("TEC")
        # TEC -> WONE (10000) -> 1USDC (500)
        path = _encode_path([TEC, WONE, USDC], [10000, 500])
    else:
        errors.append(f"{sym}: unsupported symbol in price_feed")
        return None

    try:
        amount_out, _, _, _ = q.functions.quoteExactInput(path, int(amt_in)).call()
        # amount_out is in USDC (6 decimals)
        usd = Decimal(amount_out) / Decimal(10 ** _dec("1USDC"))
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
    # merge any comparison errors into overall errors
    if cmp_.get("errors"):
        out["errors"].extend(cmp_["errors"])
    out["ETH_COMPARE"] = {k: cmp_[k] for k in ("lp_eth_usd","cb_eth_usd","diff_pct")}
    return out

# /bot/app/price_feed.py
# -*- coding: utf-8 -*-
"""
Price feed + slippage diagnostics using Uniswap V3 QuoterV2 on Harmony + Coinbase ETH spot.

Symbols:
  ONE, 1USDC, 1sDAI, TEC, 1ETH

Rules:
  - USDC is the USD anchor (1USDC = $1).
  - 1sDAI is QUOTED via QuoterV2 (NOT hard-coded) against USDC.
  - ONE priced via WONE -> 1USDC (fee 3000 per your SWAP pool — see config.POOLS_V3).
  - 1ETH via 1ETH -> WONE (3000) -> 1USDC (3000).
  - TEC  via TEC  -> WONE (10000) -> 1USDC (3000).
  - Coinbase ETH spot fetched via public endpoint (3s timeout).

Also exposes:
  - get_slippage_curve(sym, targets_usdc=[10,100,1000,10000])
  - mid price (from slot0) along the verified v3 pools in config.POOLS_V3.
"""

from __future__ import annotations
from typing import Dict, Any, List, Optional, Tuple
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
QUOTER_V2 = Web3.to_checksum_address(
    getattr(config, "QUOTER_ADDR", "0x314456E8F5efaa3dD1F036eD5900508da8A3B382")
)

def _w3() -> Web3:
    if wallet and hasattr(wallet, "get_w3"):
        return wallet.get_w3()
    rpc = getattr(config, "HARMONY_RPC", None) or getattr(config, "RPC_URL", "https://api.harmony.one")
    return Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 8}))

# Minimal ABIs
QUOTER_V2_ABI_QEI = [{
    "inputs": [{"internalType": "bytes", "name": "path", "type": "bytes"},
               {"internalType": "uint256", "name": "amountIn", "type": "uint256"}],
    "name": "quoteExactInput",
    "outputs": [{"internalType": "uint256", "name": "amountOut", "type": "uint256"},
                {"internalType": "uint160[]", "name": "sqrtPriceX96AfterList", "type": "uint160[]"},
                {"internalType": "uint32[]", "name": "initializedTicksCrossedList", "type": "uint32[]"},
                {"internalType": "uint256", "name": "gasEstimate", "type": "uint256"}],
    "stateMutability": "nonpayable", "type": "function"}]

QUOTER_V2_ABI_QEO = [{
    "inputs": [{"internalType": "bytes", "name": "path", "type": "bytes"},
               {"internalType": "uint256", "name": "amountOut", "type": "uint256"}],
    "name": "quoteExactOutput",
    "outputs": [{"internalType": "uint256", "name": "amountIn", "type": "uint256"},
                {"internalType": "uint160[]", "name": "sqrtPriceX96AfterList", "type": "uint160[]"},
                {"internalType": "uint32[]", "name": "initializedTicksCrossedList", "type": "uint32[]"},
                {"internalType": "uint256", "name": "gasEstimate", "type": "uint256"}],
    "stateMutability": "nonpayable", "type": "function"}]

POOL_ABI = [
  {"inputs":[],"name":"token0","outputs":[{"type":"address","name":""}],"stateMutability":"view","type":"function"},
  {"inputs":[],"name":"token1","outputs":[{"type":"address","name":""}],"stateMutability":"view","type":"function"},
  {"inputs":[],"name":"fee","outputs":[{"type":"uint24","name":""}],"stateMutability":"view","type":"function"},
  {"inputs":[],"name":"slot0","outputs":[
      {"type":"uint160","name":"sqrtPriceX96"},
      {"type":"int24","name":"tick"},
      {"type":"uint16","name":"observationIndex"},
      {"type":"uint16","name":"observationCardinality"},
      {"type":"uint16","name":"observationCardinalityNext"},
      {"type":"uint8","name":"feeProtocol"},
      {"type":"bool","name":"unlocked"}],
   "stateMutability":"view","type":"function"},
]

ERC20_MIN = [
    {"constant":True,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"},
    {"constant":True,"inputs":[],"name":"symbol","outputs":[{"name":"","type":"string"}],"type":"function"},
]

# ----------------- Helpers -----------------
def _tok(sym: str) -> str:
    toks = getattr(config, "TOKENS", {}) or {}
    addr = toks.get(sym)
    if not addr:
        if sym == "WONE" and "ONE" in toks: addr = toks["ONE"]
        elif sym == "ONE" and "WONE" in toks: addr = toks["WONE"]
    if not addr:
        raise RuntimeError(f"config.TOKENS missing symbol {sym}")
    return Web3.to_checksum_address(addr)

def _dec(sym: str) -> int:
    decs = getattr(config, "DECIMALS", {}) or {}
    if sym in decs: return int(decs[sym])
    return 6 if sym == "1USDC" else 18

def _encode_path(tokens: List[str], fees: List[int]) -> bytes:
    if len(tokens) < 2 or len(fees) != len(tokens) - 1:
        raise ValueError("encode_path: mismatched tokens/fees")
    out = b""
    for i in range(len(tokens) - 1):
        out += bytes.fromhex(tokens[i][2:].lower())
        out += int(fees[i]).to_bytes(3, "big")
    out += bytes.fromhex(tokens[-1][2:].lower())
    return out

def _qe_input(path: bytes, amount_in: int) -> Optional[int]:
    try:
        q = _w3().eth.contract(address=QUOTER_V2, abi=QUOTER_V2_ABI_QEI)
        amount_out, *_ = q.functions.quoteExactInput(path, int(amount_in)).call()
        return int(amount_out)
    except Exception:
        return None

def _qe_output(path: bytes, amount_out: int) -> Optional[int]:
    try:
        q = _w3().eth.contract(address=QUOTER_V2, abi=QUOTER_V2_ABI_QEO)
        amount_in, *_ = q.functions.quoteExactOutput(path, int(amount_out)).call()
        return int(amount_in)
    except Exception:
        return None

def _path_for(sym: str) -> Tuple[List[str], List[int]]:
    """Forward path sym -> USDC using your verified SWAP fees."""
    WONE = _tok("WONE")
    USDC = _tok("1USDC")
    if sym == "ONE":
        return [WONE, USDC], [3000]
    if sym == "1ETH":
        return [_tok("1ETH"), WONE, USDC], [3000, 3000]
    if sym == "TEC":
        return [_tok("TEC"), WONE, USDC], [10000, 3000]
    if sym == "1sDAI":
        return [_tok("1sDAI"), USDC], [500]
    raise RuntimeError(f"{sym}: unsupported symbol in price_feed")

def _lp_usd_forward(sym: str) -> Optional[Decimal]:
    tokens, fees = _path_for(sym)
    path = _encode_path(tokens, fees)
    amt_in = 10 ** _dec(sym)
    out = _qe_input(path, amt_in)
    if out is None: return None
    return Decimal(out) / Decimal(10 ** _dec("1USDC"))

def _lp_usd_reverse(sym: str) -> Optional[Decimal]:
    tokens, fees = _path_for(sym)
    rev_tokens = list(reversed(tokens))
    rev_fees   = list(reversed(fees))
    path_rev = _encode_path(rev_tokens, rev_fees)
    want_out = 10 ** _dec("1USDC")  # 1.0 USDC
    amt_in_sym = _qe_output(path_rev, want_out)
    if not amt_in_sym: return None
    tokens_for_1_usd = Decimal(amt_in_sym) / Decimal(10 ** _dec(sym))
    if tokens_for_1_usd == 0: return None
    return Decimal(1) / tokens_for_1_usd

def _quote_usd_for_1_token(sym: str, errors: List[str]) -> Optional[Decimal]:
    if sym == "1USDC":
        return Decimal("1.0")
    try:
        fwd = _lp_usd_forward(sym)
        rev = _lp_usd_reverse(sym)
        if fwd is None and rev is None:
            errors.append(f"{sym}: both forward/reverse LP quotes failed")
            return None
        if fwd is None: return rev
        if rev is None: return fwd
        if fwd == 0 or rev == 0: return max(fwd, rev)
        diff = abs((fwd - rev) / rev)
        if diff > Decimal("0.20"):
            errors.append(f"{sym}: forward {fwd:.6f} vs reverse {rev:.6f} diverged; using reverse")
            return rev
        return (fwd + rev) / 2
    except Exception as e:
        errors.append(f"{sym}: quote failed ({e})")
        return None

# ---------- mid price (slot0) helpers via config.POOLS_V3 ----------
def _pool_addr(label: str) -> Optional[str]:
    ent = getattr(config, "POOLS_V3", {}).get(label)
    if not ent: return None
    return Web3.to_checksum_address(ent["address"])

def _slot0_price_token0_in_token1(pool_addr: str, dec0: int, dec1: int) -> Decimal:
    p = _w3().eth.contract(address=Web3.to_checksum_address(pool_addr), abi=POOL_ABI)
    sqrtPriceX96 = p.functions.slot0().call()[0]
    num = Decimal(sqrtPriceX96) * Decimal(sqrtPriceX96)
    # P = (sqrt^2 / 2^192) * 10^dec0 / 10^dec1
    return (num / Decimal(2**192)) * Decimal(10**dec0) / Decimal(10**dec1)

def _token_meta(addr: str) -> Tuple[str,int]:
    c = _w3().eth.contract(address=Web3.to_checksum_address(addr), abi=ERC20_MIN)
    try: d = c.functions.decimals().call()
    except: d = 18
    try: s = c.functions.symbol().call()
    except: s = "<?>"
    return s, int(d)

def _mid_usdc_per_sym(sym: str) -> Optional[Decimal]:
    """Compute USDC per 1 sym from slot0 mids along verified pools."""
    WONE = _tok("WONE"); USDC = _tok("1USDC")
    if sym == "ONE":
        # 1 WONE -> USDC using 1USDC/WONE pool where token0=USDC, token1=WONE
        lab = "1USDC/WONE@3000"; addr = _pool_addr(lab)
        if not addr: return None
        # price token0 in token1 → invert to get WONE in USDC
        # safer: compute 1 USDC in WONE then invert
        # token0=USDC (6), token1=WONE (18)
        _, d0 = _token_meta(_tok("1USDC"))
        _, d1 = _token_meta(WONE)
        usdc_in_wone = _slot0_price_token0_in_token1(addr, d0, d1)  # USDC in WONE
        if usdc_in_wone == 0: return None
        wone_in_usdc = Decimal(1) / usdc_in_wone
        return wone_in_usdc

    if sym == "1sDAI":
        lab = "1USDC/1sDAI@500"; addr = _pool_addr(lab)
        if not addr: return None
        # token0=USDC (6), token1=sDAI (18) → price token1 in token0 = 1/px0in1
        _, d0 = _token_meta(_tok("1USDC"))
        _, d1 = _token_meta(_tok("1sDAI"))
        usdc_per_sdai = Decimal(1) / _slot0_price_token0_in_token1(addr, d0, d1)
        return usdc_per_sdai

    if sym == "1ETH":
        # 1ETH->WONE mid * WONE->USDC mid
        lab1 = "1ETH/WONE@3000"; lab2 = "1USDC/WONE@3000"
        a1 = _pool_addr(lab1); a2 = _pool_addr(lab2)
        if not (a1 and a2): return None
        _, dETH = _token_meta(_tok("1ETH"))
        _, dW   = _token_meta(WONE)
        # figure out ordering in 1ETH/WONE pool → token0=1ETH, token1=WONE in your listing
        eth_in_wone = _slot0_price_token0_in_token1(a1, dETH, dW)  # 1 ETH in WONE
        # WONE→USDC as above:
        _, d0 = _token_meta(_tok("1USDC"))
        _, d1 = _token_meta(WONE)
        usdc_in_wone = _slot0_price_token0_in_token1(a2, d0, d1)   # 1 USDC in WONE
        if usdc_in_wone == 0: return None
        wone_in_usdc = Decimal(1) / usdc_in_wone
        return eth_in_wone * wone_in_usdc

    if sym == "TEC":
        lab1 = "TEC/WONE@10000"; lab2 = "1USDC/WONE@3000"
        a1 = _pool_addr(lab1); a2 = _pool_addr(lab2)
        if not (a1 and a2): return None
        _, dTEC = _token_meta(_tok("TEC"))
        _, dW   = _token_meta(WONE)
        tec_in_wone = _slot0_price_token0_in_token1(a1, dTEC, dW)  # 1 TEC in WONE
        _, d0 = _token_meta(_tok("1USDC"))
        _, d1 = _token_meta(WONE)
        usdc_in_wone = _slot0_price_token0_in_token1(a2, d0, d1)
        if usdc_in_wone == 0: return None
        wone_in_usdc = Decimal(1) / usdc_in_wone
        return tec_in_wone * wone_in_usdc

    if sym == "1USDC":
        return Decimal(1)

    return None

# ----------------- Public API -----------------
def get_eth_prices_lp_vs_cb() -> Dict[str, float]:
    errors: List[str] = []
    lp_val = None
    try:
        v = _quote_usd_for_1_token("1ETH", errors)
        if v is not None:
            lp_val = float(v)
    except Exception as e:
        errors.append(f"LP ETH quote: {e}")

    cb = _coinbase_eth_usd(errors)
    diff = float("nan")
    if lp_val is not None and cb is not None and cb != 0.0:
        diff = (cb - lp_val) / cb * 100.0
    return {"lp_eth_usd": lp_val if lp_val is not None else float("nan"),
            "cb_eth_usd": cb if cb is not None else float("nan"),
            "diff_pct": diff,
            "errors": errors}

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

# -------- Slippage curve (Quoter-based) --------
def get_slippage_curve(sym: str, targets_usdc: List[float]) -> Dict[str, Any]:
    """
    For sym->USDC, for each target USDC size (e.g. 10,100,1000), compute:
      - amount_in_sym via exactOutput on reversed path
      - effective price (USDC per 1 sym) = target_usdc / amount_in_sym
      - slippage % vs mid (from slot0 path composition)
    """
    errors: List[str] = []
    try:
        mid = _mid_usdc_per_sym(sym)
    except Exception as e:
        mid = None
        errors.append(f"mid: {e}")

    tokens, fees = _path_for(sym)
    rev_tokens = list(reversed(tokens))
    rev_fees   = list(reversed(fees))
    path_rev = _encode_path(rev_tokens, rev_fees)

    rows = []
    for usd in targets_usdc:
        want_out = int(Decimal(usd) * Decimal(10 ** _dec("1USDC")))
        amt_in = _qe_output(path_rev, want_out)
        if not amt_in:
            rows.append({"usdc": float(usd), "amount_in_sym": None,
                         "px_eff": None, "slippage_pct": None, "note": "quote failed"})
            continue
        qty_sym = Decimal(amt_in) / Decimal(10 ** _dec(sym))
        if qty_sym == 0:
            rows.append({"usdc": float(usd), "amount_in_sym": 0.0,
                         "px_eff": None, "slippage_pct": None, "note": "zero qty"})
            continue
        px_eff = Decimal(usd) / qty_sym  # USDC per 1 sym, effective
        slip = None if (mid is None or mid == 0) else (px_eff - mid) / mid * Decimal(100)
        rows.append({"usdc": float(usd),
                     "amount_in_sym": float(qty_sym),
                     "px_eff": float(px_eff),
                     "slippage_pct": (float(slip) if slip is not None else None)})

    return {"symbol": sym, "mid_usdc_per_sym": (float(mid) if mid is not None else None),
            "rows": rows, "errors": errors}

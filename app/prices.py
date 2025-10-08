# /bot/app/prices.py
from __future__ import annotations
from decimal import Decimal, getcontext
from typing import Dict, Tuple, Optional, List
from web3 import Web3

# tolerant config import
try:
    from app import config as C
except Exception:
    import config as C

from app.chain import get_ctx

getcontext().prec = 40

# ---------- symbol helpers (case-insensitive) ----------
def _canon(sym: str) -> str:
    s = (sym or "").strip()
    if not s:
        raise KeyError("empty symbol")
    up = s.upper()
    for k in C.TOKENS.keys():
        if k.upper() == up:
            return k
    if up in ("ONE(NATIVE)", "ONE_NATIVE", "NATIVE_ONE"):
        return "ONE(native)"
    raise KeyError(s)

def _dec(symbol: str) -> int:
    key = _canon(symbol) if symbol != "ONE(native)" else "ONE(native)"
    return int(C.DECIMALS.get(key, 18)) if key != "ONE(native)" else 18

def _addr(symbol: str) -> str:
    key = _canon(symbol)
    return C.TOKENS[key]

# ---------- pool helpers (case-insensitive, order-agnostic) ----------
def _parse_label_pair(label: str):
    if "@" not in label or "/" not in label: return None
    pair, fee_str = label.split("@", 1)
    a, b = pair.split("/", 1)
    try:
        fee = int(fee_str)
    except Exception:
        return None
    return (a, b, fee)

def _find_pool(sym_in: str, sym_out: str) -> Optional[int]:
    A = _canon(sym_in).upper(); B = _canon(sym_out).upper()
    for label, meta in C.POOLS_V3.items():
        parsed = _parse_label_pair(label)
        if not parsed: continue
        x, y, _ = parsed
        if {x.upper(), y.upper()} == {A, B}:
            try:
                return int(meta["fee"])
            except Exception:
                pass
    return None

# ---------- quoting ----------
def _quote_single(symbol_in: str, symbol_out: str, amount_in_wei: int) -> Optional[int]:
    """
    Quote via QuoterV2 single-hop. Returns amountOut (int) or None.
    """
    fee = _find_pool(symbol_in, symbol_out)
    if fee is None:
        return None

    ctx = get_ctx(C.HARMONY_RPC)
    quoter = ctx.quoter(C.QUOTER_ADDR)

    token_in = Web3.to_checksum_address(_addr(symbol_in))
    token_out = Web3.to_checksum_address(_addr(symbol_out))

    # ✅ Correct V2 param order: (tokenIn, tokenOut, fee, amountIn, sqrtPriceLimitX96)
    params = (
        token_in,
        token_out,
        int(fee),
        int(amount_in_wei),
        0,  # sqrtPriceLimitX96
    )
    try:
        amount_out, *_ = quoter.functions.quoteExactInputSingle(params).call()
        return int(amount_out)
    except Exception:
        return None

def _quote_two_hop(symbol_in: str, inter: str, symbol_out: str, amount_in_wei: int) -> Optional[int]:
    mid = _quote_single(symbol_in, inter, amount_in_wei)
    if mid is None:
        return None
    return _quote_single(inter, symbol_out, mid)

# ---------- public API ----------
def price_usd(symbol: str, amount: Decimal = Decimal("1")) -> Optional[Tuple[Decimal, Decimal]]:
    """
    Returns (unit_price_in_USDC, total_out_USDC) for `amount` of `symbol`.
    Prefers direct pool to 1USDC; if absent, tries two-hop via common bridges.
    Bridges attempted: WONE, 1sDAI (based on your verified pools).
    """
    key = _canon(symbol)
    if key.upper() == "1USDC":
        return (Decimal(1), amount)

    dec_in  = _dec(key)
    dec_out = _dec("1USDC")
    amt_wei = int(amount * (Decimal(10) ** dec_in))

    # Direct
    out_wei = _quote_single(key, "1USDC", amt_wei)

    # Two-hop via bridges you actually have on-chain
    if out_wei is None:
        for bridge in ("WONE", "1sDAI"):
            if _find_pool(key, bridge) and _find_pool(bridge, "1USDC"):
                out_wei = _quote_two_hop(key, bridge, "1USDC", amt_wei)
                if out_wei is not None:
                    break

    if out_wei is None:
        return None

    out = Decimal(out_wei) / (Decimal(10) ** dec_out)
    price = (out / amount).quantize(Decimal("0.00000001"))
    return (price, out)

def batch_prices_usd(symbols: List[str]) -> Dict[str, Optional[Decimal]]:
    res: Dict[str, Optional[Decimal]] = {}
    for s in symbols:
        try:
            q = price_usd(s, Decimal("1"))
            res[_canon(s)] = (q[0] if q else None)
        except Exception:
            res[str(s)] = None
    return res

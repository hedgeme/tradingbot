# app/prices.py
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
    """Return the canonical symbol key as defined in config, case-insensitive."""
    s = (sym or "").strip()
    if not s:
        raise KeyError("empty symbol")
    up = s.upper()
    for k in C.TOKENS.keys():
        if k.upper() == up:
            return k
    # allow 'ONE(native)' passthrough for balances (has no ERC20 address)
    if up in ("ONE(NATIVE)", "ONE_NATIVE", "NATIVE_ONE"):
        return "ONE(native)"
    raise KeyError(s)

def _dec(symbol: str) -> int:
    """Decimals by canonical key; default to 18 if unknown."""
    key = _canon(symbol) if symbol != "ONE(native)" else "ONE(native)"
    return int(C.DECIMALS.get(key, 18)) if key != "ONE(native)" else 18

def _addr(symbol: str) -> str:
    key = _canon(symbol)
    return C.TOKENS[key]

# ---------- pool helpers (case-insensitive, order-agnostic) ----------
def _parse_label_pair(label: str) -> Optional[Tuple[str, str, int]]:
    """
    Given label like '1USDC/WONE@3000', return ('1USDC','WONE',3000) in original case.
    """
    if "@" not in label or "/" not in label:
        return None
    pair, fee_str = label.split("@", 1)
    a, b = pair.split("/", 1)
    try:
        fee = int(fee_str)
    except Exception:
        return None
    return (a, b, fee)

def _find_pool(sym_in: str, sym_out: str) -> Optional[int]:
    """Return fee tier if any label in POOLS_V3 matches the unordered pair, case-insensitive."""
    A = _canon(sym_in).upper()
    B = _canon(sym_out).upper()
    for label, meta in C.POOLS_V3.items():
        parsed = _parse_label_pair(label)
        if not parsed:
            continue
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
    Quote via a single pool using QuoterV2. Returns amountOut (int) or None.
    """
    fee = _find_pool(symbol_in, symbol_out)
    if fee is None:
        return None

    ctx = get_ctx(C.HARMONY_RPC)
    quoter = ctx.quoter(C.QUOTER_ADDR)

    token_in = Web3.to_checksum_address(_addr(symbol_in))
    token_out = Web3.to_checksum_address(_addr(symbol_out))

    # IQuoterV2.QuoteExactInputSingleParams tuple
    params = (
        token_in,
        token_out,
        int(fee),
        "0x0000000000000000000000000000000000000000",  # recipient (ignored)
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
    If direct pool to 1USDC is missing, tries two-hop via WONE.
    """
    key = _canon(symbol)
    if key.upper() == "1USDC":
        return (Decimal(1), amount)

    dec_in  = _dec(key)
    dec_out = _dec("1USDC")
    amt_wei = int(amount * (Decimal(10) ** dec_in))

    # direct
    out_wei = _quote_single(key, "1USDC", amt_wei)

    # two-hop via WONE
    if out_wei is None and _find_pool(key, "WONE") and _find_pool("WONE", "1USDC"):
        out_wei = _quote_two_hop(key, "WONE", "1USDC", amt_wei)

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

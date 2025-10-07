# app/prices.py
from __future__ import annotations
from decimal import Decimal, getcontext
from typing import Dict, Tuple, Optional, List
from web3 import Web3

# tolerant import: app.config or root config
try:
    from app import config as C
except Exception:
    import config as C

from app.chain import get_ctx

getcontext().prec = 40

def _dec(symbol: str) -> int:
    return int(C.DECIMALS.get(symbol, 18))

def _addr(symbol: str) -> str:
    return C.TOKENS[symbol]

def _find_pool(sym_in: str, sym_out: str) -> Optional[int]:
    key1 = f"{sym_in}/{sym_out}"
    key2 = f"{sym_out}/{sym_in}"
    for label, meta in C.POOLS_V3.items():
        if label.startswith(key1 + "@") or label.startswith(key2 + "@"):
            return int(meta["fee"])
    return None

def _quote_single(symbol_in: str, symbol_out: str, amount_in_wei: int) -> Optional[int]:
    ctx = get_ctx(C.HARMONY_RPC)
    quoter = ctx.quoter(C.QUOTER_ADDR)
    token_in = Web3.to_checksum_address(_addr(symbol_in))
    token_out = Web3.to_checksum_address(_addr(symbol_out))
    fee = _find_pool(symbol_in, symbol_out)
    if fee is None:
        return None
    params = (token_in, token_out, fee,
              "0x0000000000000000000000000000000000000000",
              amount_in_wei, 0)
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

def price_usd(symbol: str, amount: Decimal = Decimal("1")) -> Optional[Tuple[Decimal, Decimal]]:
    sym = symbol.upper()
    if sym == "1USDC":
        return (Decimal(1), amount)
    dec_in  = _dec(sym)
    dec_out = _dec("1USDC")
    amt_wei = int(amount * (Decimal(10) ** dec_in))

    out_wei = _quote_single(sym, "1USDC", amt_wei)
    if out_wei is None and _find_pool(sym, "WONE") and _find_pool("WONE", "1USDC"):
        out_wei = _quote_two_hop(sym, "WONE", "1USDC", amt_wei)
    if out_wei is None:
        return None

    out = Decimal(out_wei) / (Decimal(10) ** dec_out)
    price = (out / amount).quantize(Decimal("0.00000001"))
    return (price, out)

def batch_prices_usd(symbols: List[str]) -> Dict[str, Optional[Decimal]]:
    res = {}
    for s in symbols:
        q = price_usd(s, Decimal("1"))
        res[s.upper()] = (q[0] if q else None)
    return res

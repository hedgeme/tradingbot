# On-chain prices via Uniswap V3 QuoterV2 (Harmony)
# Exposes: price_usd(sym: str, amount: Decimal) -> Decimal|None
# Helpers used by slippage.py: _addr, _dec, _find_pool, _quote_path

from decimal import Decimal
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

# ----- tolerant imports (config & chain) -----
try:
    from app import config as C
except Exception:
    import config as C  # type: ignore

try:
    from app.chain import get_ctx
except Exception:
    from chain import get_ctx  # type: ignore

from web3 import Web3
from web3.contract import Contract

# ------------ constants / helpers ------------
_QUOTER_V2_ABI = [{
    "inputs":[
        {"internalType":"bytes","name":"path","type":"bytes"},
        {"internalType":"uint256","name":"amountIn","type":"uint256"}
    ],
    "name":"quoteExactInput",
    "outputs":[
        {"internalType":"uint256","name":"amountOut","type":"uint256"},
        {"internalType":"uint160","name":"sqrtPriceX96After","type":"uint160"},
        {"internalType":"uint32","name":"initializedTicksCrossed","type":"uint32"},
        {"internalType":"uint256","name":"gasEstimate","type":"uint256"}],
    "stateMutability":"nonpayable","type":"function"
}]

@lru_cache(maxsize=1)
def _ctx():
    return get_ctx(getattr(C, "HARMONY_RPC", "https://api.s0.t.hmny.io"))

@lru_cache(maxsize=1)
def _quoter() -> Contract:
    return _ctx().w3.eth.contract(
        address=Web3.to_checksum_address(C.QUOTER_ADDR),
        abi=_QUOTER_V2_ABI
    )

def _norm(sym: str) -> str:
    return sym.upper().strip()

def _tokens() -> Dict[str, str]:
    # normalize keys (upper)
    return {k.upper(): v for k, v in getattr(C, "TOKENS", {}).items()}

def _pools() -> Dict[str, Dict[str, int]]:
    # keys like "1ETH/WONE@3000" (any order is fine; we parse)
    return {k: {"address": v["address"], "fee": int(v["fee"])} for k, v in getattr(C, "POOLS_V3", {}).items()}

def _addr(sym: str) -> str:
    symN = _norm(sym)
    t = _tokens()
    if symN not in t:
        raise KeyError(f"missing address for {symN}")
    return t[symN]

# cached ERC20 decimals lookup
_ERC20_DECIMALS_ABI = [{"inputs":[],"name":"decimals","outputs":[{"internalType":"uint8","name":"","type":"uint8"}],"stateMutability":"view","type":"function"}]

@lru_cache(maxsize=64)
def _dec(sym: str) -> int:
    symN = _norm(sym)
    # First: optional local hint (if you maintain one in config, e.g., C.DECIMALS)
    dec_map = {k.upper(): v for k, v in getattr(C, "DECIMALS", {}).items()}
    if symN in dec_map:
        return int(dec_map[symN])
    # Otherwise ask chain
    w3 = _ctx().w3
    token = w3.eth.contract(address=Web3.to_checksum_address(_addr(symN)), abi=_ERC20_DECIMALS_ABI)
    return int(token.functions.decimals().call())

def _fee3(fee: int) -> bytes:
    return int(fee).to_bytes(3, "big")

def _find_pool(a: str, b: str) -> Optional[int]:
    """Return fee tier between tokens if a known pool exists (either direction)."""
    aN, bN = _norm(a), _norm(b)
    for key, info in _pools().items():
        # key like "TOKEN0/TOKEN1@3000"
        try:
            pair, fee_txt = key.split("@", 1)
            x, y = pair.split("/", 1)
            fee = int(fee_txt)
        except Exception:
            # guard against any bad keys
            continue
        if {aN, bN} == {x.upper(), y.upper()}:
            return fee
    return None

def _build_path(hops: List[Tuple[str, int, str]]) -> bytes:
    """ABI-encoded path for Quoter/Router: tokenIn (20) + fee (3) + tokenOut (20) [+ ...]"""
    out = b""
    for i, (src, fee, dst) in enumerate(hops):
        if i == 0:
            out += Web3.to_bytes(hexstr=Web3.to_checksum_address(_addr(src)))
        out += _fee3(fee) + Web3.to_bytes(hexstr=Web3.to_checksum_address(_addr(dst)))
    return out

def _quote_path(hops: List[Tuple[str, int, str]], amount_in: int) -> Optional[int]:
    if not hops:
        return None
    try:
        q = _quoter()
        out = q.functions.quoteExactInput(_build_path(hops), int(amount_in)).call()
        return int(out[0])
    except Exception:
        return None

def _auto_route_to_usdc(sym: str) -> Optional[List[Tuple[str, int, str]]]:
    """Return best-known route from sym -> 1USDC using only verified pools."""
    s = _norm(sym)
    if s == "1USDC":
        return []
    # direct
    f = _find_pool(s, "1USDC")
    if f:
        return [(s, f, "1USDC")]
    # via WONE
    f1 = _find_pool(s, "WONE")
    f2 = _find_pool("WONE", "1USDC")
    if f1 and f2:
        return [(s, f1, "WONE"), ("WONE", f2, "1USDC")]
    # via 1sDAI
    f1 = _find_pool(s, "1sDAI")
    f2 = _find_pool("1sDAI", "1USDC")
    if f1 and f2:
        return [(s, f1, "1sDAI"), ("1sDAI", f2, "1USDC")]
    return None

# ------------ public API ------------
def price_usd(sym: str, amount: Decimal) -> Optional[Decimal]:
    """Return USD value (in 1USDC units) for `amount` of `sym` using QuoterV2 path quoting."""
    s = _norm(sym)
    amt = Decimal(amount)

    # stable bases
    if s == "1USDC":
        return amt
    if s == "1SDAI":
        # quote through 1sDAI/1USDC pool to be robust; fallback to 1:1
        route = _auto_route_to_usdc("1sDAI")
        if not route:
            return amt  # assume 1:1 if pool not present
        dec_in = _dec("1sDAI")
        wei_in = int(amt * (Decimal(10) ** dec_in))
        out = _quote_path(route, wei_in)
        if out is None:
            return amt
        return Decimal(out) / (Decimal(10) ** _dec("1USDC"))

    # general case: route token -> 1USDC
    route = _auto_route_to_usdc(s)
    if route is None:
        return None
    dec_in = _dec(s)
    wei_in = int(amt * (Decimal(10) ** dec_in))
    wei_out = _quote_path(route, wei_in)
    if wei_out is None:
        return None
    return Decimal(wei_out) / (Decimal(10) ** _dec("1USDC"))

# expose helpers for slippage.py
__all__ = ["price_usd", "_addr", "_dec", "_find_pool", "_quote_path"]

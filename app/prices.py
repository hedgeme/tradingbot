# On-chain prices via Uniswap V3 QuoterV2 (Harmony)
# Public API: price_usd(sym: str, amount: Decimal) -> Decimal|None
# Helpers reused by slippage.py: _addr, _dec, _find_pool, _quote_path, _auto_route_to_usdc

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

# ------------ QuoterV2 ABI (quoteExactInput path) ------------
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

# ------------ cached context / quoter ------------
@lru_cache(maxsize=1)
def _ctx():
    return get_ctx(getattr(C, "HARMONY_RPC", "https://api.s0.t.hmny.io"))

@lru_cache(maxsize=1)
def _quoter() -> Contract:
    return _ctx().w3.eth.contract(
        address=Web3.to_checksum_address(C.QUOTER_ADDR),
        abi=_QUOTER_V2_ABI
    )

# ------------ config helpers ------------
def _norm(sym: str) -> str:
    return sym.upper().strip()

def _tokens() -> Dict[str, str]:
    # normalize keys (upper)
    return {k.upper(): v for k, v in getattr(C, "TOKENS", {}).items()}

def _pools() -> Dict[str, Dict[str, int]]:
    # keys like "1ETH/WONE@3000"
    pools = {}
    for k, v in getattr(C, "POOLS_V3", {}).items():
        try:
            fee = int(v["fee"])
            addr = v["address"]
        except Exception:
            continue
        pools[k] = {"address": addr, "fee": fee}
    return pools

def _addr(sym: str) -> str:
    symN = _norm(sym)
    t = _tokens()
    if symN not in t:
        raise KeyError(f"missing address for {symN}")
    return t[symN]

# ERC20 decimals (cached)
_ERC20_DECIMALS_ABI = [{
    "inputs":[],
    "name":"decimals",
    "outputs":[{"internalType":"uint8","name":"","type":"uint8"}],
    "stateMutability":"view","type":"function"
}]

@lru_cache(maxsize=64)
def _dec(sym: str) -> int:
    symN = _norm(sym)
    # Optional hints (if present in config)
    dec_map = {k.upper(): v for k, v in getattr(C, "DECIMALS", {}).items()}
    if symN in dec_map:
        return int(dec_map[symN])
    # Native ONE is treated as WONE
    if symN == "ONE":
        return _dec("WONE")
    # Ask chain
    w3 = _ctx().w3
    token = w3.eth.contract(address=Web3.to_checksum_address(_addr(symN)), abi=_ERC20_DECIMALS_ABI)
    return int(token.functions.decimals().call())

def _fee3(fee: int) -> bytes:
    return int(fee).to_bytes(3, "big")

# ------------ pool/route helpers ------------
def _find_pool(a: str, b: str) -> Optional[int]:
    """Return fee tier between tokens if a known pool exists (either direction)."""
    aN, bN = _norm(a), _norm(b)
    for key, info in _pools().items():
        try:
            pair, fee_txt = key.split("@", 1)
            x, y = (p.strip().upper() for p in pair.split("/", 1))
            fee = int(fee_txt)
        except Exception:
            continue
        if {aN, bN} == {x, y}:
            return fee
    return None

def _build_path(hops: List[Tuple[str, int, str]]) -> bytes:
    """ABI-encoded path: tokenIn (20) + fee (3) + tokenOut (20) [+ ...]"""
    out = b""
    for i, (src, fee, dst) in enumerate(hops):
        if i == 0:
            out += Web3.to_bytes(hexstr=Web3.to_checksum_address(_addr(src)))
        out += _fee3(fee) + Web3.to_bytes(hexstr=Web3.to_checksum_address(_addr(dst)))
    return out

def _quote_path(hops: List[Tuple[str, int, str]], amount_in_wei: int) -> Optional[int]:
    if not hops:
        return None
    try:
        out = _quoter().functions.quoteExactInput(_build_path(hops), int(amount_in_wei)).call()
        return int(out[0])  # amountOut
    except Exception:
        return None

def _candidate_routes_to_usdc(sym: str) -> List[List[Tuple[str, int, str]]]:
    """All viable routes from sym to 1USDC (direct, via WONE, via 1sDAI)."""
    s = _norm(sym)
    if s == "1USDC":
        return [[]]
    routes: List[List[Tuple[str,int,str]]] = []
    # direct
    f = _find_pool(s, "1USDC")
    if f:
        routes.append([(s, f, "1USDC")])
    # via WONE
    f1 = _find_pool(s, "WONE")
    f2 = _find_pool("WONE", "1USDC")
    if f1 and f2:
        routes.append([(s, f1, "WONE"), ("WONE", f2, "1USDC")])
    # via 1sDAI
    f1 = _find_pool(s, "1sDAI")
    f2 = _find_pool("1sDAI", "1USDC")
    if f1 and f2:
        routes.append([(s, f1, "1sDAI"), ("1sDAI", f2, "1USDC")])
    return routes

def _auto_route_to_usdc(sym: str) -> Optional[List[Tuple[str, int, str]]]:
    """
    Pick the best of (direct), via WONE, or via 1sDAI by probing each with a tiny amount.
    This avoids depegged/illiquid bridges.
    """
    s = _norm(sym)
    if s == "1USDC":
        return []

    cands = _candidate_routes_to_usdc(s)
    if not cands:
        return None

    dec_in = _dec(s)
    probe_wei = int(Decimal("0.01") * (Decimal(10) ** dec_in)) or 1  # 0.01 unit
    best = None
    best_out_usdc = Decimal("-1")

    for route in cands:
        wei_out = _quote_path(route, probe_wei)
        if wei_out is None:
            continue
        out_usdc = Decimal(wei_out) / (Decimal(10) ** _dec("1USDC"))
        if out_usdc > best_out_usdc:
            best_out_usdc = out_usdc
            best = route
    return best

# ------------ public API ------------
def price_usd(sym: str, amount: Decimal) -> Optional[Decimal]:
    """
    Return USD value (1USDC units) for `amount` of `sym` using QuoterV2 path quoting.
    - ONE is priced via WONE 1:1.
    - 1sDAI is routed to 1USDC (not assumed 1:1).
    - Chooses best bridge dynamically.
    """
    s = _norm(sym)
    amt = Decimal(amount)

    # Native ONE → treat as WONE
    if s == "ONE":
        s = "WONE"

    # Stable cases
    if s == "1USDC":
        return amt

    # General route to USDC
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
__all__ = ["price_usd", "_addr", "_dec", "_find_pool", "_quote_path", "_auto_route_to_usdc"]

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

# ------------ QuoterV2 ABI ------------
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

# ------------ context / quoter ------------
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
    # Optional local hints
    dec_map = {k.upper(): v for k, v in getattr(C, "DECIMALS", {}).items()}
    if symN in dec_map:
        return int(dec_map[symN])
    # Otherwise ask chain
    w3 = _ctx().w3
    token = w3.eth.contract(address=Web3.to_checksum_address(_addr(symN)), abi=_ERC20_DECIMALS_ABI)
    return int(token.functions.decimals().call())

def _fee3(fee: int) -> bytes:
    return int(fee).to_bytes(3, "big")

# ------------ pool lookup ------------
def _find_pool(a: str, b: str) -> Optional[int]:
    """Return fee tier between tokens if a known pool exists (either direction)."""
    aN, bN = _norm(a), _norm(b)
    for key, info in _pools().items():
        try:
            pair, fee_txt = key.split("@", 1)
            x, y = pair.split("/", 1)
            fee = int(fee_txt)
        except Exception:
            continue
        if {aN, bN} == {x.upper(), y.upper()}:
            return fee
    return None

# ------------ path build / quote ------------
def _build_path(hops: List[Tuple[str, int, str]]) -> bytes:
    """ABI-encoded path for Quoter/Router: tokenIn (20) + fee (3) + tokenOut (20) [+ ...]"""
    out = b""
    for i, (src, fee, dst) in enumerate(hops):
        if i == 0:
            out += Web3.to_bytes(hexstr=Web3.to_checksum_address(_addr(src)))
        out += _fee3(fee) + Web3.to_bytes(hexstr=Web3.to_checksum_address(_addr(dst)))
    return out

def _quote_path(hops: List[Tuple[str, int, str]], amount_in_wei: int) -> Optional[int]:
    """Quote exact input along the given path, return amountOut (wei) or None."""
    if not hops or amount_in_wei <= 0:
        return None
    try:
        q = _quoter()
        out = q.functions.quoteExactInput(_build_path(hops), int(amount_in_wei)).call()
        return int(out[0])
    except Exception:
        return None

# ------------ route candidates ------------
def _route_candidates_to_usdc(sym: str) -> List[List[Tuple[str,int,str]]]:
    """All allowed forward candidates: direct, via WONE, via 1sDAI (where pools exist)."""
    s = _norm(sym)
    if s == "1USDC":
        return [[]]
    out: List[List[Tuple[str,int,str]]] = []

    # direct
    f = _find_pool(s, "1USDC")
    if f:
        out.append([(s, f, "1USDC")])

    # via WONE
    f1 = _find_pool(s, "WONE")
    f2 = _find_pool("WONE", "1USDC")
    if f1 and f2:
        out.append([(s, f1, "WONE"), ("WONE", f2, "1USDC")])

    # via 1sDAI
    f1 = _find_pool(s, "1sDAI")
    f2 = _find_pool("1sDAI", "1USDC")
    if f1 and f2:
        out.append([(s, f1, "1sDAI"), ("1sDAI", f2, "1USDC")])

    return out

def _best_forward_route_to_usdc(sym: str) -> Optional[List[Tuple[str,int,str]]]:
    """Probe tiny amount on each candidate and pick the one with highest USDC out."""
    s = _norm(sym)
    if s == "1USDC":
        return []
    cands = _route_candidates_to_usdc(s)
    if not cands:
        return None
    # probe with 0.01 unit (or 1 wei if decimals produce <1)
    dec_in = _dec(s)
    probe_wei = int(Decimal("0.01") * (Decimal(10) ** dec_in)) or 1
    best = None
    best_out = Decimal("-1")
    for r in cands:
        out = _quote_path(r, probe_wei)
        if out is None:
            continue
        out_usdc = Decimal(out) / (Decimal(10) ** _dec("1USDC"))
        if out_usdc > best_out:
            best_out = out_usdc
            best = r
    return best

# ------------ pricing primitives ------------
def _price_forward_usdc(sym: str, amount: Decimal) -> Optional[Decimal]:
    """Exact-input forward pricing: sym -> ... -> 1USDC"""
    s = _norm(sym)
    if s == "1USDC":
        return Decimal(amount)
    route = _best_forward_route_to_usdc(s)
    if not route:
        return None
    dec_in = _dec(s)
    wei_in = int(Decimal(amount) * (Decimal(10) ** dec_in))
    wei_out = _quote_path(route, wei_in)
    if wei_out is None:
        return None
    return Decimal(wei_out) / (Decimal(10) ** _dec("1USDC"))

def _invert_route(route: List[Tuple[str,int,str]]) -> List[Tuple[str,int,str]]:
    """Turn [A-f->B, B-g->C] into [USDC-g->B, B-f->A] form is NOT what we want.
       For reverse quoting, we need USDC->...->sym path; given a forward route
       A->...->1USDC, reverse path is 1USDC->...->A with same fees reversed."""
    rev: List[Tuple[str,int,str]] = []
    for hop in reversed(route):
        src, fee, dst = hop
        rev.append((dst, fee, src))
    return rev

def _price_reverse_usdc(sym: str, amount: Decimal) -> Optional[Decimal]:
    """
    Reverse pricing: quote USDC -> ... -> sym for a *guess* amount of USDC,
    then invert to USDC per 1 sym. We scale USDC_in to roughly match `amount`.
    """
    s = _norm(sym)
    if s == "1USDC":
        return Decimal(amount)
    forward = _best_forward_route_to_usdc(s)
    if not forward:
        return None
    reverse = _invert_route(forward)

    # tiny mid (forward on 0.01) to estimate size
    tiny = _price_forward_usdc(s, Decimal("0.01"))
    if (tiny is None) or (tiny <= 0):
        return None
    # USDC to feed into reverse so that sym_out ~= amount
    # If forward(0.01 sym) → X usdc, then 1 sym ~ X/0.01 usdc.
    usdc_per_one = tiny / Decimal("0.01")
    usdc_in = (usdc_per_one * Decimal(amount))

    # quote reverse exact-input with USDC_in
    dec_u = _dec("1USDC")
    wei_in = int(usdc_in * (Decimal(10) ** dec_u))
    wei_out = _quote_path(reverse, wei_in)
    if not wei_out:
        return None
    # wei_out is sym (in wei). Invert to get USDC per sym
    dec_s = _dec(s)
    sym_out = Decimal(wei_out) / (Decimal(10) ** dec_s)
    if sym_out <= 0:
        return None
    return Decimal(usdc_in) / sym_out

# ------------ public API ------------
# Assets where we always want forward pricing (thin/mid-cap style)
_FORWARD_ONLY = {"TEC", "ONE", "WONE", "1SDAI"}

def price_usd(sym: str, amount: Decimal) -> Optional[Decimal]:
    """
    Return USD value (in 1USDC units) for `amount` of `sym`.
    Policy:
      - 1USDC: 1:1
      - Forward-only for TEC/ONE/WONE/1sDAI
      - ETH tries both: choose reverse if it materially differs and is healthier.
    """
    s = _norm(sym)
    amt = Decimal(amount)

    if s == "1USDC":
        return amt

    if s in _FORWARD_ONLY:
        return _price_forward_usdc(s, amt)

    # Try both for majors (1ETH), prefer reverse if both succeed and differ > 3%
    fwd = _price_forward_usdc(s, amt)
    rev = _price_reverse_usdc(s, amt)

    if fwd is None and rev is None:
        return None
    if rev is None:
        return fwd
    if fwd is None:
        return rev

    # both present
    if fwd > 0:
        diff = abs(rev - fwd) / fwd
        if diff > Decimal("0.03"):
            return rev
    # default to forward if close
    return rev if rev > 0 and rev < fwd*Decimal("10") and rev > fwd*Decimal("0.1") else fwd

# Expose helpers for slippage.py
__all__ = ["price_usd", "_addr", "_dec", "_find_pool", "_quote_path"]

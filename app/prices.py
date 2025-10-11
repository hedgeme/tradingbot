# On-chain prices via Uniswap V3 QuoterV2 (Harmony)
# Public:
#   price_usd(sym: str, amount: Decimal) -> Optional[Decimal]
#   batch_prices_usd(syms: List[str]) -> Dict[str, Optional[Decimal]]
#
# Helpers (used by slippage.py):
#   _addr, _dec, _find_pool, _quote_path
#
# Design:
# - Route only through verified pools in config (direct, via WONE, via 1sDAI).
# - Probe BOTH forward (sym->...->1USDC) and reverse (1USDC->...->sym) with tiny notionals.
#   Use the side that produces the more realistic USDC-per-sym (reverse-inverted often wins
#   for thin pools / directionality issues). Threshold is adjustable via REV_BIAS_BPS.
# - ONE is treated as an alias of WONE for pricing.

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

# ------------ config knobs ------------
# How aggressively to prefer reverse-inverted over forward if they differ.
# If |reverse/forward - 1| * 10_000 >= REV_BIAS_BPS we pick reverse.
REV_BIAS_BPS = int(getattr(C, "PRICE_REVERSE_BIAS_BPS", 1000))  # default 1000 = 10%

# Probe sizes (do not move markets, but large enough to avoid dust / rounding)
FWD_PROBE_UNITS: Dict[str, Decimal] = {
    "1ETH": Decimal("0.02"),
    "TEC": Decimal("100"),
    "WONE": Decimal("2000"),
    "ONE":  Decimal("2000"),
    "1SDAI": Decimal("10"),
}
REV_PROBE_USDC = Decimal(getattr(C, "PRICE_USDC_PROBE", "200"))  # 200 USDC

# ------------ cached context/handles ------------
@lru_cache(maxsize=1)
def _ctx():
    return get_ctx(getattr(C, "HARMONY_RPC", "https://api.s0.t.hmny.io"))

@lru_cache(maxsize=1)
def _quoter() -> Contract:
    return _ctx().w3.eth.contract(
        address=Web3.to_checksum_address(C.QUOTER_ADDR),
        abi=_QUOTER_V2_ABI
    )

# ------------ token/pool helpers ------------
def _norm(sym: str) -> str:
    return sym.upper().strip()

def _tokens() -> Dict[str, str]:
    # normalize keys (upper)
    return {k.upper(): v for k, v in getattr(C, "TOKENS", {}).items()}

def _addr(sym: str) -> str:
    """Return checksum address for symbol (ONE is treated as WONE)."""
    s = _norm(sym)
    if s == "ONE":
        s = "WONE"
    t = _tokens()
    if s not in t:
        raise KeyError(f"missing address for {s}")
    return t[s]

# cached ERC20 decimals lookup
_ERC20_DECIMALS_ABI = [{
    "inputs":[], "name":"decimals",
    "outputs":[{"internalType":"uint8","name":"","type":"uint8"}],
    "stateMutability":"view","type":"function"
}]

@lru_cache(maxsize=64)
def _dec(sym: str) -> int:
    """Return decimals; ONE treated as 18 via WONE contract."""
    s = _norm(sym)
    if s == "ONE":
        s = "WONE"
    # Optional local hint
    dec_map = {k.upper(): v for k, v in getattr(C, "DECIMALS", {}).items()}
    if s in dec_map:
        return int(dec_map[s])
    # Else fetch from chain
    w3 = _ctx().w3
    token = w3.eth.contract(address=Web3.to_checksum_address(_addr(s)), abi=_ERC20_DECIMALS_ABI)
    return int(token.functions.decimals().call())

def _pools() -> Dict[str, Dict[str, int]]:
    # keys like "1ETH/WONE@3000"
    out: Dict[str, Dict[str, int]] = {}
    for k, v in getattr(C, "POOLS_V3", {}).items():
        try:
            fee = int(v["fee"])
            addr = Web3.to_checksum_address(v["address"])
            out[k.upper()] = {"address": addr, "fee": fee}
        except Exception:
            continue
    return out

def _fee3(fee: int) -> bytes:
    return int(fee).to_bytes(3, "big")

def _find_pool(a: str, b: str) -> Optional[int]:
    """Return fee tier between tokens if a known pool exists (either direction)."""
    aN, bN = _norm(a), _norm(b)
    if aN == "ONE":
        aN = "WONE"
    if bN == "ONE":
        bN = "WONE"
    for key, info in _pools().items():
        # parse "X/Y@fee"
        try:
            pair, fee_txt = key.split("@", 1)
            x, y = pair.split("/", 1)
            fee = int(fee_txt)
        except Exception:
            continue
        if {aN, bN} == {x.upper(), y.upper()}:
            return fee
    return None

def _build_path(hops: List[Tuple[str, int, str]]) -> bytes:
    """ABI-encoded path: tokenIn(20) + fee(3) + tokenOut(20) [+ ...]."""
    out = b""
    for i, (src, fee, dst) in enumerate(hops):
        s = "WONE" if _norm(src) == "ONE" else _norm(src)
        d = "WONE" if _norm(dst) == "ONE" else _norm(dst)
        if i == 0:
            out += Web3.to_bytes(hexstr=Web3.to_checksum_address(_addr(s)))
        out += _fee3(fee) + Web3.to_bytes(hexstr=Web3.to_checksum_address(_addr(d)))
    return out

def _quote_path(hops: List[Tuple[str, int, str]], amount_in_wei: int) -> Optional[int]:
    if not hops or amount_in_wei <= 0:
        return None
    try:
        out = _quoter().functions.quoteExactInput(_build_path(hops), int(amount_in_wei)).call()
        return int(out[0])
    except Exception:
        return None

# ------------ routing (only verified bridges) ------------
def _routes_to_usdc(sym: str) -> List[List[Tuple[str, int, str]]]:
    """Return candidate routes from sym -> 1USDC using verified pools."""
    s = _norm(sym)
    if s == "ONE":
        s = "WONE"
    if s == "1USDC":
        return [[]]

    routes: List[List[Tuple[str, int, str]]] = []

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

def _routes_from_usdc(sym: str) -> List[List[Tuple[str, int, str]]]:
    """Return candidate routes from 1USDC -> sym (reverse direction of the verified graph)."""
    s = _norm(sym)
    if s == "ONE":
        s = "WONE"
    if s == "1USDC":
        return [[]]

    routes: List[List[Tuple[str, int, str]]] = []

    # direct
    f = _find_pool("1USDC", s)
    if f:
        routes.append([("1USDC", f, s)])

    # via WONE
    f1 = _find_pool("1USDC", "WONE")
    f2 = _find_pool("WONE", s)
    if f1 and f2:
        routes.append([("1USDC", f1, "WONE"), ("WONE", f2, s)])

    # via 1sDAI
    f1 = _find_pool("1USDC", "1sDAI")
    f2 = _find_pool("1sDAI", s)
    if f1 and f2:
        routes.append([("1USDC", f1, "1sDAI"), ("1sDAI", f2, s)])

    return routes

# ------------ forward & reverse probes ------------
def _best_usdc_per_unit(sym: str) -> Tuple[Optional[Decimal], List[str]]:
    """
    Returns (best_price_usdc_per_1_sym, notes)
    - Forward: use small amount in sym (from FWD_PROBE_UNITS or 0.01).
    - Reverse: use REV_PROBE_USDC and invert to USDC per sym.
    - If both exist and differ by >= REV_BIAS_BPS, prefer reverse.
    """
    s = _norm(sym)
    if s == "ONE":
        s = "WONE"

    notes: List[str] = []
    dec_in = _dec(s)
    dec_usdc = _dec("1USDC")

    # Forward probe
    fwd_routes = _routes_to_usdc(s)
    fwd_px: Optional[Decimal] = None
    if fwd_routes:
        probe = FWD_PROBE_UNITS.get(s, Decimal("0.01"))
        wei_in = int(probe * (Decimal(10) ** dec_in))
        best_fwd = None
        for r in fwd_routes:
            out = _quote_path(r, wei_in)
            if out is None:
                continue
            px = (Decimal(out) / (Decimal(10) ** dec_usdc)) / probe
            if best_fwd is None or px > best_fwd:
                best_fwd = px
        fwd_px = best_fwd

    # Reverse probe (invert)
    rev_routes = _routes_from_usdc(s)
    rev_px: Optional[Decimal] = None
    if rev_routes:
        usdc_in = REV_PROBE_USDC
        wei_usdc = int(usdc_in * (Decimal(10) ** dec_usdc))
        best_rev_sym_out = None
        for r in rev_routes:
            out = _quote_path(r, wei_usdc)
            if out is None:
                continue
            sym_out = Decimal(out) / (Decimal(10) ** dec_in)
            if sym_out > 0 and (best_rev_sym_out is None or sym_out > best_rev_sym_out):
                best_rev_sym_out = sym_out
        if best_rev_sym_out and best_rev_sym_out > 0:
            rev_px = usdc_in / best_rev_sym_out  # USDC per 1 sym

    # Choose
    if fwd_px is None and rev_px is None:
        return None, notes
    if fwd_px is None:
        notes.append(f"{s}: using reverse-only")
        return rev_px, notes
    if rev_px is None:
        notes.append(f"{s}: using forward-only")
        return fwd_px, notes

    # Both exist — prefer reverse if diverge more than threshold
    try:
        diff_bps = abs((rev_px / fwd_px) - Decimal(1)) * Decimal(10000)
        if diff_bps >= Decimal(REV_BIAS_BPS):
            notes.append(f"{s}: forward {fwd_px:,.6f} vs reverse {rev_px:,.6f} diverged; using reverse")
            return rev_px, notes
        else:
            notes.append(f"{s}: forward {fwd_px:,.6f} vs reverse {rev_px:,.6f} close; using forward")
            return fwd_px, notes
    except Exception:
        return rev_px, notes  # conservative

# ------------ public API ------------
def price_usd(sym: str, amount: Decimal) -> Optional[Decimal]:
    """
    Return USD (1USDC) value for `amount` of `sym`.
    Uses best of forward and reverse-inverted probes through verified pools.
    """
    s = _norm(sym)

    # Bases
    if s == "1USDC":
        return Decimal(amount)
    if s == "ONE":
        s = "WONE"

    # Special case: 1sDAI close to 1 USDC but still quoted on-chain via verified pool if present
    if s == "1SDAI":
        # Prefer direct/bridge quoting to catch real on-chain peg
        px, _ = _best_usdc_per_unit("1sDAI")
        if px is None:
            return Decimal(amount)  # fallback 1:1 if pool missing
        return Decimal(amount) * px

    px, _ = _best_usdc_per_unit(s)
    if px is None:
        return None
    return Decimal(amount) * px

def batch_prices_usd(syms: List[str]) -> Dict[str, Optional[Decimal]]:
    out: Dict[str, Optional[Decimal]] = {}
    for s in syms:
        try:
            v = price_usd(s, Decimal("1"))
        except Exception:
            v = None
        out[_norm(s)] = v
    return out

# expose helpers for slippage.py
__all__ = ["price_usd", "batch_prices_usd", "_addr", "_dec", "_find_pool", "_quote_path"]

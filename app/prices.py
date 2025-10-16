# On-chain prices via Uniswap V3 QuoterV2 (Harmony)
# Public (unchanged): price_usd(sym: str, amount: Decimal) -> Optional[Decimal]
# Helpers (unchanged): _addr, _dec, _find_pool, _quote_path
# Added for /prices table: unit_quote(sym) -> dict with per-unit price, basis, slippage bps, route

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

# ------------ Quoter ABI (V2) ------------
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
    return {k.upper(): v for k, v in getattr(C, "TOKENS", {}).items()}

def _pools() -> Dict[str, Dict[str, int]]:
    # keys like "1ETH/WONE@3000"
    return {k: {"address": v["address"], "fee": int(v["fee"])}
            for k, v in getattr(C, "POOLS_V3", {}).items()}

def _addr(sym: str) -> str:
    symN = _norm(sym)
    t = _tokens()
    if symN not in t:
        raise KeyError(f"missing address for {symN}")
    return t[symN]

# --- ERC20 decimals (cached) ---
_ERC20_DECIMALS_ABI = [{"inputs":[],"name":"decimals","outputs":[{"internalType":"uint8","name":"","type":"uint8"}],"stateMutability":"view","type":"function"}]

@lru_cache(maxsize=64)
def _dec(sym: str) -> int:
    symN = _norm(sym)
    dec_map = {k.upper(): v for k, v in getattr(C, "DECIMALS", {}).items()}
    if symN in dec_map:
        return int(dec_map[symN])
    token = _ctx().w3.eth.contract(address=Web3.to_checksum_address(_addr(symN)), abi=_ERC20_DECIMALS_ABI)
    return int(token.functions.decimals().call())

def _fee3(fee: int) -> bytes:
    return int(fee).to_bytes(3, "big")

def _find_pool(a: str, b: str) -> Optional[int]:
    """Return fee tier between tokens if a known pool exists (either direction)."""
    aN, bN = _norm(a), _norm(b)
    for key, _info in _pools().items():
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

# ------------ ROUTING HELPERS ------------
def _best_forward_route_to_usdc(sym: str, wei_in: int) -> Optional[List[Tuple[str,int,str]]]:
    """
    Among (direct), via WONE, via 1sDAI — pick the one that yields the highest USDC-out
    for the GIVEN input size (wei_in).
    """
    s = _norm(sym)
    if s == "1USDC":
        return []
    candidates: List[List[Tuple[str,int,str]]] = []

    # direct
    f = _find_pool(s, "1USDC")
    if f:
        candidates.append([(s, f, "1USDC")])

    # via WONE
    f1 = _find_pool(s, "WONE")
    f2 = _find_pool("WONE", "1USDC")
    if f1 and f2:
        candidates.append([(s, f1, "WONE"), ("WONE", f2, "1USDC")])

    # via 1sDAI
    f1 = _find_pool(s, "1sDAI")
    f2 = _find_pool("1sDAI", "1USDC")
    if f1 and f2:
        candidates.append([(s, f1, "1sDAI"), ("1sDAI", f2, "1USDC")])

    if not candidates:
        return None

    best = None
    best_out = -1
    for route in candidates:
        out = _quote_path(route, wei_in)
        if out is None:
            continue
        if out > best_out:
            best_out = out
            best = route
    return best

def _reverse_eth_price_from_usdc(usdc_in: Decimal) -> Optional[Tuple[Decimal,str]]:
    """
    Buy ETH with a small USDC amount; return (USDC per 1 ETH, route_text).
    Route is 1USDC -> WONE -> 1ETH using known 0.3% pools.
    """
    try:
        dec_u = _dec("1USDC"); dec_e = _dec("1ETH")
        wei = int(usdc_in * (Decimal(10)**dec_u))
        route = [
            ("1USDC", 3000, "WONE"),
            ("WONE", 3000, "1ETH"),
        ]
        out = _quote_path(route, wei)
        if not out:
            return None
        eth_out = Decimal(out) / (Decimal(10)**dec_e)
        if eth_out <= 0:
            return None
        price = usdc_in / eth_out  # USDC per 1 ETH
        return price, "1USDC → WONE → 1ETH (rev)"
    except Exception:
        return None

# ------------ PUBLIC (legacy behavior kept) ------------
def price_usd(sym: str, amount: Decimal) -> Optional[Decimal]:
    """
    Return total USDC for `amount` of `sym` using forward best-known route.
    (Legacy behavior; kept for compatibility with slippage and other callers.)
    """
    s = _norm(sym)
    amt = Decimal(amount)

    if s == "1USDC":
        return amt

    if s == "1SDAI":
        # Prefer pool if available; else assume 1:1
        f = _find_pool("1sDAI", "1USDC")
        if not f:
            return amt
        dec_in = _dec("1sDAI")
        wei_in = int(amt * (Decimal(10) ** dec_in))
        out = _quote_path([("1sDAI", f, "1USDC")], wei_in)
        return (Decimal(out) / (Decimal(10)**_dec("1USDC"))) if out else amt

    # General forward best-of (at this size)
    dec_in = _dec(s)
    wei_in = int(amt * (Decimal(10) ** dec_in))
    route = _best_forward_route_to_usdc(s, wei_in)
    if route is None:
        return None
    wei_out = _quote_path(route, wei_in)
    if wei_out is None:
        return None
    return Decimal(wei_out) / (Decimal(10) ** _dec("1USDC"))

# ------------ NEW: unit-level pricer for /prices ------------
_BASIS_BY_ASSET: Dict[str, Decimal] = {
    "1ETH":  Decimal("0.05"),   # basis for display; ETH price uses small reverse buy under the hood
    "TEC":   Decimal("100"),
    "ONE":   Decimal("1000"),
    "WONE":  Decimal("1000"),
    "1SDAI": Decimal("1"),
    "1USDC": Decimal("1"),
}

def _basis_for(sym: str) -> Decimal:
    return _BASIS_BY_ASSET.get(_norm(sym), Decimal("1"))

def _safe_micro(amount: Decimal) -> Decimal:
    # 1/20th of basis but not below a dust floor
    micro = (amount / Decimal(20)).quantize(Decimal("0.000001"))
    return micro if micro > Decimal("0") else Decimal("0.000001")

def unit_quote(sym: str) -> Optional[Dict[str, object]]:
    """
    Policy used by /prices table:
      - ETH: reverse at small USDC size ($100 USDC), invert to USDC/ETH
      - Others: forward best-of at a chosen basis size; normalize to per-1
      - Slippage shown: per-unit at basis vs per-unit at micro (basis/20), in bps
    Returns: {
      'asset': '1ETH',
      'unit_price': Decimal,            # USDC per 1 token
      'basis': Decimal,                 # the size used for quoting (in token units; ETH row still reports per 1)
      'slippage_bps': int,              # bps vs micro
      'route': str,                     # human text of route and direction
      'direction': 'fwd'|'rev'|'base'
    }
    """
    s = _norm(sym)

    # 1USDC is base
    if s == "1USDC":
        return {
            "asset": s,
            "unit_price": Decimal("1"),
            "basis": Decimal("1"),
            "slippage_bps": 0,
            "route": "—",
            "direction": "base",
        }

    # ETH uses reverse small USDC buy for robustness
    if s == "1ETH":
        # main quote from $100 USDC; micro from $25 USDC
        main = _reverse_eth_price_from_usdc(Decimal("100"))
        micro = _reverse_eth_price_from_usdc(Decimal("25"))
        if not main or not micro:
            return None
        px_main, route_txt = main
        px_micro, _ = micro
        # bps vs micro
        slip_bps = int((((px_main - px_micro) / px_micro) * Decimal(10000)).quantize(Decimal("1")))
        return {
            "asset": s,
            "unit_price": px_main,   # USDC per 1 ETH
            "basis": Decimal("1"),   # display per 1 ETH (derived from $100 buy)
            "slippage_bps": slip_bps,
            "route": route_txt,
            "direction": "rev",
        }

    # Everyone else: forward best-of at basis
    basis = _basis_for(s)
    dec_in = _dec(s)
    wei_in = int(basis * (Decimal(10)**dec_in))
    route = _best_forward_route_to_usdc(s, wei_in)
    if not route:
        return None

    out = _quote_path(route, wei_in)
    if not out:
        return None
    usdc_out = Decimal(out) / (Decimal(10)**_dec("1USDC"))
    per_unit = usdc_out / basis

    # micro probe for slippage
    micro_amt = _safe_micro(basis)
    wei_micro = int(micro_amt * (Decimal(10)**dec_in))
    route_micro = _best_forward_route_to_usdc(s, wei_micro) or route
    out_micro = _quote_path(route_micro, wei_micro)
    per_unit_micro = (Decimal(out_micro) / (Decimal(10)**_dec("1USDC")) / micro_amt) if out_micro else per_unit
    slip_bps = int((((per_unit - per_unit_micro) / per_unit_micro) * Decimal(10000)).quantize(Decimal("1")))

    # route text
    route_txt = " → ".join([route[0][0]] + [h[2] for h in route])

    return {
        "asset": s,
        "unit_price": per_unit,   # USDC per 1 token
        "basis": basis,           # token units used for quoting
        "slippage_bps": slip_bps,
        "route": f"{route_txt} (fwd)",
        "direction": "fwd",
    }

__all__ = ["price_usd", "_addr", "_dec", "_find_pool", "_quote_path", "unit_quote"]

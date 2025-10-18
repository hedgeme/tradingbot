# /bot/app/prices.py
# On-chain prices via Uniswap V3 QuoterV2 (Harmony)
# Exposes:
#   - price_usd(sym: str, amount: Decimal) -> Decimal|None    (effective sell price for `amount`)
#   - mid_price(sym: str) -> Decimal|None                     (tiny-notional mid; robust for 1ETH)
# Helpers used by slippage.py and telegram_listener.py:
#   _addr, _dec, _find_pool, _quote_path

from decimal import Decimal
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

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

# ---------- ABIs ----------
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

_ERC20_DECIMALS_ABI = [{
    "inputs":[], "name":"decimals",
    "outputs":[{"internalType":"uint8","name":"","type":"uint8"}],
    "stateMutability":"view","type":"function"
}]

# ---------- context ----------
@lru_cache(maxsize=1)
def _ctx():
    return get_ctx(getattr(C, "HARMONY_RPC", "https://api.s0.t.hmny.io"))

@lru_cache(maxsize=1)
def _quoter() -> Contract:
    return _ctx().w3.eth.contract(
        address=Web3.to_checksum_address(C.QUOTER_ADDR),
        abi=_QUOTER_V2_ABI
    )

# ---------- symbol helpers ----------
def _norm(sym: str) -> str:
    return sym.upper().strip()

def _canon(sym: str) -> str:
    """Map native 'ONE' to 'WONE' for routing/quoting. Others pass-through."""
    s = _norm(sym)
    if s == "ONE":
        return "WONE"
    return s

def _tokens() -> Dict[str, str]:
    # keep original map but allow lookups by canon name too
    t = {k.upper(): v for k, v in getattr(C, "TOKENS", {}).items()}
    if "ONE" in t and "WONE" in t and t["ONE"] == t["WONE"]:
        # both present and same address -> good
        pass
    return t

def _pools() -> Dict[str, Dict[str, int]]:
    return {k: {"address": v["address"], "fee": int(v["fee"])}
            for k, v in getattr(C, "POOLS_V3", {}).items()}

def _addr(sym: str) -> str:
    s = _canon(sym)
    t = _tokens()
    if s not in t:
        raise KeyError(f"missing address for {s}")
    return t[s]

@lru_cache(maxsize=64)
def _dec(sym: str) -> int:
    s = _canon(sym)
    # optional local hints
    dec_map = {k.upper(): v for k, v in getattr(C, "DECIMALS", {}).items()}
    if s in dec_map:
        return int(dec_map[s])
    w3 = _ctx().w3
    token = w3.eth.contract(address=Web3.to_checksum_address(_addr(s)), abi=_ERC20_DECIMALS_ABI)
    return int(token.functions.decimals().call())

def _fee3(fee: int) -> bytes:
    return int(fee).to_bytes(3, "big")

def _find_pool(a: str, b: str) -> Optional[int]:
    """Return fee tier if a known v3 pool exists between a and b (either order)."""
    aN, bN = _canon(a), _canon(b)
    for key, info in _pools().items():
        try:
            pair, fee_txt = key.split("@", 1)
            x, y = [p.upper() for p in pair.split("/", 1)]
            fee = int(fee_txt)
        except Exception:
            continue
        # treat ONE as WONE by canonicalization
        if {aN, bN} == {_canon(x), _canon(y)}:
            return fee
    return None

def _build_path(hops: List[Tuple[str, int, str]]) -> bytes:
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
        q = _quoter()
        out = q.functions.quoteExactInput(_build_path(hops), int(amount_in_wei)).call()
        return int(out[0])
    except Exception:
        return None

# ---------- routing ----------
def _best_route_to_usdc(sym: str) -> Optional[List[Tuple[str, int, str]]]:
    """Pick the best of: direct, via WONE, via 1sDAI — by tiny forward probe into 1USDC."""
    s = _canon(sym)
    if s == "1USDC":
        return []
    candidates: List[List[Tuple[str, int, str]]] = []

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

    # probe each with 0.01 unit and pick highest USDC out
    dec_in = _dec(s)
    probe_wei = int((Decimal("0.01")) * (Decimal(10) ** dec_in)) or 1
    best = None
    best_out = Decimal("-1")

    for route in candidates:
        out = _quote_path(route, probe_wei)
        if out is None:
            continue
        usdc_out = Decimal(out) / (Decimal(10) ** _dec("1USDC"))
        if usdc_out > best_out:
            best_out = usdc_out
            best = route
    return best

# ---------- public API ----------
def price_usd(sym: str, amount: Decimal) -> Optional[Decimal]:
    """
    Effective sell price: USDC you’d receive by selling `amount` of `sym`.
    This is what the trade would roughly execute at (forward path).
    """
    s = _canon(sym)
    amt = Decimal(amount)

    if s == "1USDC":
        return amt

    if s == "1SDAI":
        # quote via sdai->usdc
        route = _best_route_to_usdc("1sDAI")
        if not route:
            return amt
        dec_in = _dec("1sDAI")
        wei_in = int(amt * (Decimal(10) ** dec_in))
        out = _quote_path(route, wei_in)
        if out is None:
            return amt
        return Decimal(out) / (Decimal(10) ** _dec("1USDC"))

    # general forward route
    route = _best_route_to_usdc(s)
    if route is None:
        return None
    dec_in = _dec(s)
    wei_in = int(amt * (Decimal(10) ** dec_in))
    wei_out = _quote_path(route, wei_in)
    if wei_out is None:
        return None
    return Decimal(wei_out) / (Decimal(10) ** _dec("1USDC"))

def mid_price(sym: str) -> Optional[Decimal]:
    """
    Tiny-notional, more neutral 'mid':
      - For 1ETH: robust reverse tiny probes (USDC→ETH) vs forward tiny sell; take median,
        ignore obviously bad reverse outliers.
      - For others: forward tiny probe.
    Returns USDC per 1 unit of `sym`.
    """
    s = _canon(sym)
    if s == "1USDC":
        return Decimal("1")

    # forward tiny: sell 0.01 sym into USDC
    route_f = _best_route_to_usdc(s)
    if not route_f:
        return None
    dec_in = _dec(s)
    tiny_amt = Decimal("0.01")
    wei_in = int(tiny_amt * (Decimal(10) ** dec_in))
    f_out = _quote_path(route_f, wei_in)
    if f_out is None:
        return None
    f_px = (Decimal(f_out) / (Decimal(10) ** _dec("1USDC"))) / tiny_amt

    if s != "1ETH":
        return f_px  # forward tiny is fine for non-ETH

    # reverse tiny probes for 1ETH (USDC -> ETH), then invert
    # try a few small sizes and collect acceptable implied prices
    def _rev_px(usdc_in: Decimal) -> Optional[Decimal]:
        # build path USDC->...->ETH by inverting best route
        # Our forward best is ETH->(WONE|1sDAI)->USDC; reverse is USDC->(WONE|1sDAI)->ETH
        best_eth_to_usdc = _best_route_to_usdc("1ETH")
        if not best_eth_to_usdc:
            return None
        rev_hops = []
        # reverse hops order & tokens
        cur_dst = "1ETH"
        for (a, fee, b) in best_eth_to_usdc[::-1]:
            rev_hops.append((_canon(b), fee, _canon(a)))
        # ensure first is 1USDC
        if rev_hops[0][0] != "1USDC":
            # If we ended with WONE->1ETH, first should be 1USDC->WONE; that’s fine.
            pass
        dec_u = _dec("1USDC")
        wei = int(usdc_in * (Decimal(10) ** dec_u))
        eth_out_wei = _quote_path(rev_hops, wei)
        if eth_out_wei is None or eth_out_wei == 0:
            return None
        eth_out = Decimal(eth_out_wei) / (Decimal(10) ** _dec("1ETH"))
        if eth_out <= 0:
            return None
        return usdc_in / eth_out  # implied USDC per 1 ETH

    samples = []
    for usdc in (Decimal("50"), Decimal("100"), Decimal("250")):
        px = _rev_px(usdc)
        if px is not None:
            samples.append(px)

    # guardrails: drop samples that are > 2.0x or < 0.5x of forward tiny
    good = [p for p in samples if (p <= f_px * Decimal("2.0") and p >= f_px * Decimal("0.5"))]
    if not good:
        return f_px
    # median-of forward tiny and the good reverse samples
    cand = sorted(good + [f_px])
    mid = cand[len(cand)//2]
    return mid

__all__ = ["price_usd", "mid_price", "_addr", "_dec", "_find_pool", "_quote_path"]

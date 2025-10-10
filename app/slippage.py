# Slippage / minOut calculator using QuoterV2 (path-based)
# Exposes: compute_slippage(token_in, token_out, amount_in: Decimal, slippage_bps: int=30) -> dict|None

from decimal import Decimal
from typing import List, Tuple, Optional

# tolerant imports
try:
    from app import config as C
except Exception:
    import config as C  # type: ignore

try:
    from app.chain import get_ctx
except Exception:
    from chain import get_ctx  # type: ignore

from web3 import Web3
from app import prices as PR  # reuse addr/dec/_find_pool/_quote_path

# QuoterV2 ABI (only method we need)
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

def _ctx():
    return get_ctx(getattr(C, "HARMONY_RPC", "https://api.s0.t.hmny.io"))

def _quoter():
    return _ctx().w3.eth.contract(address=Web3.to_checksum_address(C.QUOTER_ADDR), abi=_QUOTER_V2_ABI)

def _fee3(fee: int) -> bytes:
    return int(fee).to_bytes(3, "big")

def _build_path(hops: List[Tuple[str,int,str]]) -> bytes:
    b = b""
    for i,(src,fee,dst) in enumerate(hops):
        if i == 0:
            b += Web3.to_bytes(hexstr=Web3.to_checksum_address(PR._addr(src)))
        b += _fee3(fee) + Web3.to_bytes(hexstr=Web3.to_checksum_address(PR._addr(dst)))
    return b

def _route(a: str, b: str) -> Optional[List[Tuple[str,int,str]]]:
    """Find up to 2-hop route between a and b using verified pools."""
    a = a.upper(); b = b.upper()
    if a == b:
        return []

    f = PR._find_pool(a,b)
    if f:
        return [(a,f,b)]

    # try via common bridges
    bridges = ["WONE", "1USDC", "1sDAI"]
    for mid in bridges:
        f1 = PR._find_pool(a, mid)
        f2 = PR._find_pool(mid, b)
        if f1 and f2:
            return [(a,f1,mid),(mid,f2,b)]
    return None

def _fmt_amt(wei: int, sym: str) -> str:
    dec = PR._dec(sym)
    return str(Decimal(wei) / (Decimal(10) ** dec))

def compute_slippage(token_in: str, token_out: str, amount_in: Decimal, slippage_bps: int = 30):
    a = token_in.upper().strip()
    b = token_out.upper().strip()
    route = _route(a, b)
    if route is None:
        return None

    dec_in = PR._dec(a)
    wei_in = int(Decimal(amount_in) * (Decimal(10) ** dec_in))

    # quote path
    q = _quoter()
    try:
        amount_out, *_ = q.functions.quoteExactInput(_build_path(route), wei_in).call()
    except Exception:
        return None

    dec_out = PR._dec(b)
    amt_out = Decimal(amount_out) / (Decimal(10) ** dec_out)

    # naive impact estimate: re-quote half size and compare per-unit
    try:
        half_out, *_ = q.functions.quoteExactInput(_build_path(route), wei_in // 2).call()
        pu_full = amt_out / Decimal(amount_in) if amount_in > 0 else Decimal(0)
        pu_half = (Decimal(half_out) / (Decimal(10) ** dec_out)) / (Decimal(amount_in) / 2) if amount_in > 0 else Decimal(0)
        impact_bps = int((max(Decimal(0), pu_half - pu_full) / pu_half) * Decimal(1_0000)) if pu_half > 0 else None
    except Exception:
        impact_bps = None

    # minOut using tolerance
    min_out = amt_out * (Decimal(1) - Decimal(slippage_bps) / Decimal(10_000))

    # human path text
    path_text = " → ".join([route[0][0]] + [h[2] for h in route])

    return {
        "amount_out": amt_out,
        "amount_out_fmt": f"{amt_out:.8f}",
        "min_out": min_out,
        "min_out_fmt": f"{min_out:.8f}",
        "slippage_bps": int(slippage_bps),
        "impact_bps": impact_bps,
        "path_text": path_text,
    }

__all__ = ["compute_slippage"]

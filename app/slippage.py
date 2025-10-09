# app/slippage.py — slippage/impact using QuoterV2.quoteExactInput(path, amountIn)
from decimal import Decimal, ROUND_DOWN
from typing import Dict, List, Tuple, Optional

# Always import from app.* (chain.py lives in app/)
from app import config as C
from app import prices as PR

DEFAULT_BPS = int(getattr(C, "SLIPPAGE_DEFAULT_BPS", 30))  # 0.30% default

def _path_between(sym_in: str, sym_out: str) -> Optional[List[Tuple[str,int,str]]]:
    a, b = sym_in.upper(), sym_out.upper()

    # direct pool?
    fee = PR._find_pool(a, b)
    if fee:
        return [(a, fee, b)]

    # Known 2-hop combos (per verified pools)
    if (a, b) == ("1ETH", "1USDC"):
        return [("1ETH", 3000, "WONE"), ("WONE", 3000, "1USDC")]
    if (a, b) == ("TEC", "1USDC"):
        return [("TEC", 10000, "1sDAI"), ("1sDAI", 500, "1USDC")]
    if (a, b) == ("1sDAI", "1USDC"):
        return [("1sDAI", 500, "1USDC")]
    if (a, b) == ("WONE", "1USDC"):
        return [("WONE", 3000, "1USDC")]
    if (a, b) == ("1ETH", "WONE"):
        return [("1ETH", 3000, "WONE")]

    # try reverse fee (direction still tokenIn→tokenOut)
    fee_rev = PR._find_pool(b, a)
    if fee_rev:
        return [(a, fee_rev, b)]
    return None

def compute_slippage(token_in: str, token_out: str, amount_in: Decimal, slippage_bps: Optional[int]=None) -> Optional[Dict]:
    """
    Returns dict with:
      amount_out_wei, amount_out_fmt, min_out_wei, min_out_fmt, impact_bps, slippage_bps, path_text
    or None on failure.
    """
    path = _path_between(token_in, token_out)
    if not path:
        return None

    dec_in = PR._dec(token_in)
    amt_wei = int((amount_in * (Decimal(10) ** dec_in)).to_integral_value(rounding=ROUND_DOWN))
    out_wei = PR._quote_path(path, amt_wei)
    if out_wei is None:
        return None

    dec_out = PR._dec(token_out)
    out_amt = (Decimal(out_wei) / (Decimal(10) ** dec_out))
    bps = int(DEFAULT_BPS if slippage_bps is None else slippage_bps)
    min_out = (out_amt * (Decimal(1) - Decimal(bps) / Decimal(10_000))).quantize(Decimal("0.000001"))

    impact_bps = None  # precise impact requires pool liquidity math; optional later

    # build human path text: A@fee → B → C…
    parts = []
    for i, (a, fee, b) in enumerate(path):
        if i < len(path) - 1:
            parts.append(f"{a}@{fee}")
        else:
            parts.append(b)
    path_text = " → ".join(parts)

    return {
        "amount_out_wei": out_wei,
        "amount_out_fmt": f"{out_amt}",
        "min_out_wei": int((min_out * (Decimal(10) ** dec_out)).to_integral_value(rounding=ROUND_DOWN)),
        "min_out_fmt": f"{min_out}",
        "impact_bps": impact_bps,
        "slippage_bps": bps,
        "path_text": path_text,
    }

__all__ = ["compute_slippage"]

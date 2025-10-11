# app/slippage.py
from decimal import Decimal, ROUND_DOWN
from typing import Dict, List, Optional, Tuple

try:
    from app import config as C
except Exception:
    import config as C  # type: ignore

try:
    from app import prices as PR
except Exception:
    import prices as PR  # type: ignore

from web3 import Web3

# ---------- helpers from prices.py ----------
# _addr(sym) -> address, _dec(sym) -> int, _find_pool(a,b) -> Optional[int], _quote_path(hops, amount_in_wei) -> Optional[int]

def _norm(s: str) -> str:
    return s.upper().strip()

def _pick_verified_path(token_in: str, token_out: str) -> Optional[List[Tuple[str,int,str]]]:
    """Pick a route using only verified pools: direct, via WONE, via 1sDAI."""
    a, b = _norm(token_in), _norm(token_out)
    if a == "ONE": a = "WONE"
    if b == "ONE": b = "WONE"

    # direct
    f = PR._find_pool(a, b)
    if f:
        return [(a, f, b)]
    # via WONE
    f1 = PR._find_pool(a, "WONE"); f2 = PR._find_pool("WONE", b)
    if f1 and f2:
        return [(a, f1, "WONE"), ("WONE", f2, b)]
    # via 1sDAI
    f1 = PR._find_pool(a, "1SDAI"); f2 = PR._find_pool("1SDAI", b)
    if f1 and f2:
        return [(a, f1, "1SDAI"), ("1SDAI", f2, b)]
    return None

def compute_slippage(token_in: str, token_out: str, amount_in: Decimal, slippage_bps: int = 30) -> Optional[Dict]:
    """
    Legacy single-size API used by /slippage <IN> [AMOUNT] [OUT].
    Returns a dict with amount_out, min_out, slippage_bps, impact_bps, path_text.
    """
    a, b = _norm(token_in), _norm(token_out)
    if b == "ONE": b = "WONE"
    if a == "ONE": a = "WONE"

    route = _pick_verified_path(a, b)
    if not route:
        return None

    dec_in = PR._dec(a); dec_out = PR._dec(b)
    wei_in = int(amount_in * (Decimal(10) ** dec_in))
    wei_out = PR._quote_path(route, wei_in)
    if wei_out is None:
        return None

    out_amt = (Decimal(wei_out) / (Decimal(10) ** dec_out))

    # Baseline mid (USDC per 1 a) if OUT is 1USDC, otherwise derive via prices.price_usd
    if b == "1USDC":
        mid = PR.price_usd(a, Decimal("1"))  # USDC per 1 a
        impact_bps = int(((mid - (out_amt / amount_in)) / mid) * Decimal(10000)) if (mid and amount_in > 0) else 0
    else:
        # compute synthetic mid via USDC: px_a and px_b in USDC; mid_out_per_in = px_a / px_b
        px_a = PR.price_usd(a, Decimal("1"))
        px_b = PR.price_usd(b, Decimal("1"))
        mid = (px_a / px_b) if (px_a and px_b and px_b > 0) else None
        impact_bps = int(((mid - (out_amt / amount_in)) / mid) * Decimal(10000)) if (mid and amount_in > 0) else 0

    # minOut from slippage tolerance
    min_out = out_amt * (Decimal(1) - Decimal(slippage_bps) / Decimal(10000))
    return {
        "amount_out": out_amt,
        "amount_out_fmt": f"{out_amt:.8f}",
        "min_out": min_out,
        "min_out_fmt": f"{min_out:.8f}",
        "slippage_bps": slippage_bps,
        "impact_bps": max(0, impact_bps),
        "path_text": " → ".join([route[0][0]] + [hop[2] for hop in route]),
    }

def slippage_curve_usdc_sizes(token_in: str, token_out: str = "1USDC",
                              sizes_usdc: Optional[List[Decimal]] = None) -> Optional[Dict]:
    """
    Build a curve where the X-axis is target *USDC size*, for trading token_in -> token_out.
    For each USDC size S, we:
      - Estimate amount_in = S / baseline_price (USDC per 1 token_in),
      - Quote the actual output with that amount_in,
      - Compute effective price = out / amount_in and slippage vs baseline.

    Returns:
      {
        "token_in": "1ETH",
        "token_out": "1USDC",
        "baseline_usdc_per_in": Decimal(...),
        "rows": [
           {"size_usdc": Decimal, "amt_in": Decimal, "eff_px": Decimal, "slippage_pct": Decimal},
           ...
        ],
        "path_text": "1ETH → WONE → 1USDC"
      }
    """
    a, b = _norm(token_in), _norm(token_out)
    if b == "ONE": b = "WONE"
    if a == "ONE": a = "WONE"

    route = _pick_verified_path(a, b)
    if not route:
        return None

    # baseline in USDC terms for token_in
    if b == "1USDC":
        baseline = PR.price_usd(a, Decimal("1"))  # USDC per 1 a
    else:
        pa = PR.price_usd(a, Decimal("1"))
        pb = PR.price_usd(b, Decimal("1"))
        baseline = (pa / pb) if (pa and pb and pb > 0) else None

    if not baseline or baseline <= 0:
        return None

    if sizes_usdc is None:
        sizes_usdc = [Decimal("10"), Decimal("100"), Decimal("1000"), Decimal("10000")]

    dec_in = PR._dec(a); dec_out = PR._dec(b)
    rows = []
    for S in sizes_usdc:
        # Guess amount_in from baseline, keep a few extra decimals to avoid rounding up
        amt_in = (S / baseline).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        if amt_in <= 0:
            rows.append({"size_usdc": S, "amt_in": None, "eff_px": None, "slippage_pct": None})
            continue

        wei_in = int(amt_in * (Decimal(10) ** dec_in))
        wei_out = PR._quote_path(route, wei_in)
        if wei_out is None:
            rows.append({"size_usdc": S, "amt_in": None, "eff_px": None, "slippage_pct": None})
            continue

        out_amt = Decimal(wei_out) / (Decimal(10) ** dec_out)
        eff_px = (out_amt / amt_in) if amt_in > 0 else None  # units: token_out per token_in

        if eff_px is None or eff_px <= 0:
            rows.append({"size_usdc": S, "amt_in": amt_in, "eff_px": None, "slippage_pct": None})
            continue

        # Convert slippage vs baseline (%)
        # If token_out is USDC, eff_px is USDC per a, perfect.
        # Otherwise, it's token_out per a, and baseline is token_out per a as well.
        slip_pct = ((eff_px - baseline) / baseline) * Decimal(100)

        rows.append({
            "size_usdc": S,
            "amt_in": amt_in,
            "eff_px": eff_px,
            "slippage_pct": slip_pct,
        })

    return {
        "token_in": a,
        "token_out": b,
        "baseline_usdc_per_in": baseline if b == "1USDC" else None,
        "path_text": " → ".join([route[0][0]] + [h[2] for h in route]),
        "rows": rows,
    }

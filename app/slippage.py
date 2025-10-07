# app/slippage.py
from __future__ import annotations
from decimal import Decimal, getcontext
from typing import Optional

# tolerant import: app.config or root config
try:
    from app import config as C
except Exception:
    import config as C

from app.prices import _dec, _find_pool, _quote_single, _quote_two_hop

getcontext().prec = 40

def compute_slippage(token_in: str, token_out: str, amount_in: Decimal, default_bps: int = 30) -> Optional[dict]:
    token_in = token_in.upper()
    token_out = token_out.upper()
    dec_in = _dec(token_in); dec_out = _dec(token_out)

    amt_wei = int(amount_in * (Decimal(10) ** dec_in))
    out_wei = _quote_single(token_in, token_out, amt_wei)
    if out_wei is None and _find_pool(token_in, "WONE") and _find_pool("WONE", token_out):
        out_wei = _quote_two_hop(token_in, "WONE", token_out, amt_wei)
    if out_wei is None:
        return None

    amount_out = Decimal(out_wei) / (Decimal(10) ** dec_out)

    # marginal price via 1-unit quote
    unit_out_wei = _quote_single(token_in, token_out, int(Decimal(10) ** dec_in)) or out_wei
    unit_out = Decimal(unit_out_wei) / (Decimal(10) ** dec_out)
    implied_no_impact = unit_out * amount_in
    impact_bps = (Decimal(0) if implied_no_impact == 0
                  else (1 - (amount_out / implied_no_impact)) * Decimal(10000))

    min_out = amount_out * (Decimal(1) - Decimal(default_bps) / Decimal(10000))

    return {
        "amount_in": amount_in,
        "amount_out": amount_out.quantize(Decimal("0.00000001")),
        "unit_price": unit_out.quantize(Decimal("0.00000001")),
        "impact_bps": impact_bps.quantize(Decimal("0.01")),
        "min_out": min_out.quantize(Decimal("0.00000001")),
    }

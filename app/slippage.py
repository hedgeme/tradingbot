# app/slippage.py
from __future__ import annotations
from decimal import Decimal, getcontext
from typing import Optional

try:
    from app import config as C
except Exception:
    import config as C

# reuse helpers from prices
from app.prices import _canon, _dec, _find_pool, _quote_single, _quote_two_hop

getcontext().prec = 40

def compute_slippage(token_in: str, token_out: str, amount_in: Decimal, default_bps: int = 30) -> Optional[dict]:
    ti = _canon(token_in)
    to = _canon(token_out)

    dec_in = _dec(ti)
    dec_out = _dec(to)

    amt_wei = int(amount_in * (Decimal(10) ** dec_in))

    out_wei = _quote_single(ti, to, amt_wei)
    if out_wei is None and _find_pool(ti, "WONE") and _find_pool("WONE", to):
        out_wei = _quote_two_hop(ti, "WONE", to, amt_wei)
    if out_wei is None:
        return None

    amount_out = Decimal(out_wei) / (Decimal(10) ** dec_out)

    # approximate "unit" price for no-impact reference
    one_unit_wei = int(Decimal(10) ** dec_in)
    unit_out_wei = _quote_single(ti, to, one_unit_wei)
    if unit_out_wei is None and _find_pool(ti, "WONE") and _find_pool("WONE", to):
        unit_out_wei = _quote_two_hop(ti, "WONE", to, one_unit_wei)
    unit_out = (Decimal(unit_out_wei) / (Decimal(10) ** dec_out)) if unit_out_wei else amount_out / amount_in

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

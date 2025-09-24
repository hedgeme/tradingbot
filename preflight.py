# /bot/app/preflight.py
# Provides run_sanity() used by /sanity command

from decimal import Decimal
from typing import Dict, Any

# ----- IMPORT YOUR BALANCES HELPER FROM wallet.py -----
# Try common names; change ONE LINE below if your function is named differently.
try:
    # Preferred name in this project
    from app.wallet import get_all_wallet_balances as _get_balances
except Exception:
    # Fallbacks (uncomment the one that exists in your wallet.py)
    # from app.wallet import get_balances as _get_balances
    # from app.wallet import balances as _get_balances
    _get_balances = None

# ----- THRESHOLDS (edit if your policy differs)
THRESHOLDS = {
    "tecbot_eth": {"ONE": Decimal("200")},
    "tecbot_usdc": {"ONE": Decimal("200")},
    "tecbot_sdai": {"USDC": Decimal("5")},  # If your symbol is "1USDC", see ALIASES below
    "tecbot_tec": {"TEC": Decimal("10")},
}

# If your /balances uses aliases like "1USDC" or "1sDAI", normalize them here:
ALIASES = {
    "1USDC": "USDC",
    "1sDAI": "sDAI",
    "ONE": "ONE",
    "TEC": "TEC",
    "USDC": "USDC",
    "sDAI": "sDAI",
}

def _norm_symbol(sym: str) -> str:
    return ALIASES.get(str(sym).strip(), str(sym).strip())

def _to_decimal(x) -> Decimal:
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal("0")

def run_sanity() -> Dict[str, Any]:
    """
    Returns:
      {
        "ok": bool,
        "items": [
          {"wallet":"tecbot_eth",
           "checks":[{"asset":"ONE","have":"250","need":"200","ok":True}],
           "status":"ok"},
          ...
        ]
      }
    """
    if _get_balances is None:
        raise RuntimeError("wallet balances helper not found. Point preflight.py to your wallet function.")

    # Expect: {wallet: {SYMBOL: amount, ...}, ...}
    balances = _get_balances()
    items = []
    overall_ok = True

    # Normalize symbols in balances (handle 1USDC -> USDC, etc.)
    norm_balances: Dict[str, Dict[str, Decimal]] = {}
    for wallet, assets in (balances or {}).items():
        norm_balances[wallet] = {}
        for sym, amt in (assets or {}).items():
            norm_sym = _norm_symbol(sym)
            norm_balances[wallet][norm_sym] = _to_decimal(amt)

    for wallet, reqs in THRESHOLDS.items():
        checks = []
        this_ok = True
        wallet_bal = norm_balances.get(wallet, {})

        for asset, need in reqs.items():
            need_dec = _to_decimal(need)
            have_dec = _to_decimal(wallet_bal.get(asset, 0))
            ok = have_dec >= need_dec
            if not ok:
                this_ok = False
                overall_ok = False
            checks.append({
                "asset": asset,
                "have": f"{have_dec.normalize()}",
                "need": f"{need_dec.normalize()}",
                "ok": ok
            })

        items.append({
            "wallet": wallet,
            "checks": checks,
            "status": "ok" if this_ok else "low"
        })

    return {"ok": overall_ok, "items": items}


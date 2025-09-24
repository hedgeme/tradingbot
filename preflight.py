# /bot/app/preflight.py

from decimal import Decimal
from typing import Dict, Any, Optional
import os

from app.wallet import (
    get_w3,
    get_native_balance_wei,
    get_erc20_balance_wei,
)

try:
    from app.config import WALLETS as CFG_WALLETS
    from app.config import TOKENS as CFG_TOKENS
    from app.config import DECIMALS as CFG_DECIMALS
except Exception:
    CFG_WALLETS, CFG_TOKENS, CFG_DECIMALS = {}, {}, {}

WALLETS: Dict[str, Optional[str]] = dict(CFG_WALLETS or {})
TOKENS:  Dict[str, Optional[str]] = dict(CFG_TOKENS or {})
DECIMALS: Dict[str, int] = dict(CFG_DECIMALS or {})

# Optional env fallbacks
WALLETS.setdefault("tecbot_eth",  os.environ.get("WALLET_TECBOT_ETH"))
WALLETS.setdefault("tecbot_usdc", os.environ.get("WALLET_TECBOT_USDC"))
WALLETS.setdefault("tecbot_sdai", os.environ.get("WALLET_TECBOT_SDAI"))
WALLETS.setdefault("tecbot_tec",  os.environ.get("WALLET_TECBOT_TEC"))

SYMBOL_ALIAS = {
    "USDC": "1USDC",
    "sDAI": "1sDAI",
    "1USDC": "1USDC",
    "1sDAI": "1sDAI",
    "TEC": "TEC",
    "ONE": "ONE",
}

THRESHOLDS: Dict[str, Dict[str, Decimal]] = {
    "tecbot_eth":  {"ONE":  Decimal("200")},
    "tecbot_usdc": {"ONE":  Decimal("200")},
    "tecbot_sdai": {"USDC": Decimal("5")},
    "tecbot_tec":  {"TEC":  Decimal("10")},
}

ONE_DECIMALS = Decimal(10) ** 18

def _decimals(sym: str) -> Decimal:
    return Decimal(10) ** int(DECIMALS.get(sym, 18))

def _d(x) -> Decimal:
    try:
        return x if isinstance(x, Decimal) else Decimal(str(x))
    except Exception:
        return Decimal(0)

def _fmt(x: Decimal) -> str:
    try:
        return f"{x.normalize()}"
    except Exception:
        return str(x)

def _need_addr(kind: str, val: Optional[str]) -> str:
    if not val:
        raise RuntimeError(f"Missing address for {kind}. Provide it via app.config or env.")
    return val

def _get_one_balance(addr: str) -> Decimal:
    return _d(get_native_balance_wei(addr)) / ONE_DECIMALS

def _get_erc20_balance(token_addr: str, wallet_addr: str, symbol: str) -> Decimal:
    raw = get_erc20_balance_wei(token_addr, wallet_addr)
    return _d(raw) / _decimals(symbol)

def run_sanity() -> Dict[str, Any]:
    _ = get_w3()  # RPC reachability

    items = []
    overall_ok = True

    for wallet_name, reqs in THRESHOLDS.items():
        wallet_addr = _need_addr(wallet_name, WALLETS.get(wallet_name))
        this_ok = True
        checks = []

        for asset, need in reqs.items():
            need_d = _d(need)
            asset_norm = SYMBOL_ALIAS.get(asset, asset)

            if asset_norm == "ONE":
                have_d = _get_one_balance(wallet_addr)
            else:
                token_addr = _need_addr(asset_norm, TOKENS.get(asset_norm))
                have_d = _get_erc20_balance(token_addr, wallet_addr, asset_norm)

            ok = have_d >= need_d
            if not ok:
                this_ok = False
                overall_ok = False

            checks.append({
                "asset": asset,
                "have": _fmt(have_d),
                "need": _fmt(need_d),
                "ok": ok,
            })

        items.append({
            "wallet": wallet_name,
            "checks": checks,
            "status": "ok" if this_ok else "low",
        })

    return {"ok": overall_ok, "items": items}

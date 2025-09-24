# /bot/app/preflight.py
# Self-contained sanity checker for TECBot.
# Uses low-level wallet helpers (no dependency on a missing get_all_wallet_balances()).

from decimal import Decimal
from typing import Dict, Any, Optional
import os

# ---- Import low-level helpers that EXIST in wallet.py
from app.wallet import (
    get_w3,
    get_native_balance_wei,
    get_erc20_balance_wei,
)

# ---- Try to get addresses from config.py (preferred), else fall back to env
WALLETS: Dict[str, Optional[str]] = {}
ERC20_ADDR: Dict[str, Optional[str]] = {}

try:
    # If your config exposes these, great:
    # - WALLETS like {"tecbot_eth": "0x...", "tecbot_usdc": "0x...", ...}
    # - TOKENS  like {"USDC": "0x...", "TEC": "0x...", "sDAI": "0x..."}
    from app.config import WALLETS as CFG_WALLETS  # type: ignore
    from app.config import TOKENS as CFG_TOKENS    # type: ignore
    if isinstance(CFG_WALLETS, dict):
        WALLETS.update(CFG_WALLETS)
    if isinstance(CFG_TOKENS, dict):
        ERC20_ADDR.update(CFG_TOKENS)
except Exception:
    # config.py might not expose those dicts; we’ll fall back to env
    pass

# Fallback to env var names if config not set
WALLETS.setdefault("tecbot_eth", os.environ.get("WALLET_TECBOT_ETH"))
WALLETS.setdefault("tecbot_usdc", os.environ.get("WALLET_TECBOT_USDC"))
WALLETS.setdefault("tecbot_sdai", os.environ.get("WALLET_TECBOT_SDAI"))
WALLETS.setdefault("tecbot_tec",  os.environ.get("WALLET_TECBOT_TEC"))

# Token contract addresses (Harmony mainnet or your network)
ERC20_ADDR.setdefault("USDC", os.environ.get("TOKEN_USDC"))
ERC20_ADDR.setdefault("TEC",  os.environ.get("TOKEN_TEC"))
ERC20_ADDR.setdefault("sDAI", os.environ.get("TOKEN_SDAI"))

# ---- Threshold policy (tune as you like)
THRESHOLDS: Dict[str, Dict[str, Decimal]] = {
    "tecbot_eth": {"ONE": Decimal("200")},   # native ONE on Harmony
    "tecbot_usdc": {"ONE": Decimal("200")},
    "tecbot_sdai": {"USDC": Decimal("5")},
    "tecbot_tec": {"TEC": Decimal("10")},
}

ONE_DECIMALS = Decimal(10) ** 18  # Harmony ONE uses 18 decimals
ERC20_DECIMALS = {
    # If you want exact on-chain decimals, you can add a call; for sanity we hardcode common ones:
    "USDC": Decimal(10) ** 6,
    "TEC":  Decimal(10) ** 18,
    "sDAI": Decimal(10) ** 18,
}

def _d(x) -> Decimal:
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
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
    wei = get_native_balance_wei(addr)
    return _d(wei) / ONE_DECIMALS

def _get_erc20_balance(token_addr: str, wallet_addr: str, symbol: str) -> Decimal:
    raw = get_erc20_balance_wei(token_addr, wallet_addr)
    decs = ERC20_DECIMALS.get(symbol, Decimal(10) ** 18)
    return _d(raw) / decs

def run_sanity() -> Dict[str, Any]:
    """
    Returns:
      {
        "ok": bool,
        "items": [
          {
            "wallet": "tecbot_eth",
            "checks": [{"asset":"ONE","have":"250","need":"200","ok":True}],
            "status": "ok"
          },
          ...
        ]
      }
    """
    # Ensure web3 is constructed (and RPC reachable). Raises if RPC is down.
    _ = get_w3()

    items = []
    overall_ok = True

    for wallet_name, reqs in THRESHOLDS.items():
        wallet_addr = _need_addr(wallet_name, WALLETS.get(wallet_name))
        this_ok = True
        checks = []

        for asset, need in reqs.items():
            need_d = _d(need)

            if asset == "ONE":
                have_d = _get_one_balance(wallet_addr)
            else:
                token_addr = _need_addr(asset, ERC20_ADDR.get(asset))
                have_d = _get_erc20_balance(token_addr, wallet_addr, asset)

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

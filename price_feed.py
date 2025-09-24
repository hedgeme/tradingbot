# price_feed.py
# -*- coding: utf-8 -*-
"""
Unified price helpers for the Telegram bot.

Exports:
- get_prices() -> dict                        # LP USD quotes for core symbols + ETH Coinbase compare
- get_eth_prices_lp_vs_cb() -> dict           # {'lp_eth_usd', 'cb_eth_usd', 'diff_pct'}

LP quotes are expected to come from your Uniswap-style Quoter/Router (Harmony),
with a conservative fallback so /prices remains informative even if a quote fails.
"""

from __future__ import annotations
from typing import Dict, Any
import logging
import math

log = logging.getLogger("price_feed")

# Local modules
def _imp(modname: str):
    try:
        return __import__(modname, fromlist=['*'])
    except Exception:
        return __import__(f"app.{modname}", fromlist=['*'])

config = _imp("config")

# Coinbase client (present in repo)
try:
    coinbase_client = _imp("coinbase_client")
except Exception:
    coinbase_client = None

# If you have a dedicated pool quote helper, you can import it here:
# from test_pool_quotes import get_usd_price  # if present & stable

# ----------------------------- LP quote adapter -------------------------------
def _lp_quote_usd(symbol: str) -> float:
    """
    Return a USD price for a Harmony token symbol from LP/Quoter.
    Replace the stub with your canonical quoting call if you have one.
    """
    # Example of wiring to a helper if you ship it:
    # try:
    #     from test_pool_quotes import get_usd_price
    #     return float(get_usd_price(symbol))
    # except Exception:
    #     pass

    # Basic, safe fallbacks:
    if symbol == "1USDC":
        return 1.0
    if symbol == "1sDAI":
        return 1.0

    raise RuntimeError(f"LP quote function not wired for symbol {symbol}; connect your Quoter here.")

# ----------------------------- Public API -------------------------------------
def get_eth_prices_lp_vs_cb() -> Dict[str, float]:
    """
    Get ETH price from LP (Harmony) and Coinbase, plus pct diff ((cb-lp)/cb*100).
    """
    lp_eth_usd = float("nan")
    cb_eth_usd = float("nan")

    # LP side
    try:
        lp_eth_usd = float(_lp_quote_usd("1ETH"))
    except Exception as e:
        log.error("LP ETH quote failed: %s", e)

    # Coinbase side
    try:
        if not coinbase_client or not hasattr(coinbase_client, "get_eth_usd"):
            raise RuntimeError("coinbase_client.get_eth_usd() not available")
        cb_eth_usd = float(coinbase_client.get_eth_usd())
    except Exception as e:
        log.error("Coinbase ETH quote failed: %s", e)

    diff_pct = float("nan")
    if cb_eth_usd == cb_eth_usd and lp_eth_usd == lp_eth_usd and cb_eth_usd != 0.0:  # NaN-safe
        diff_pct = (cb_eth_usd - lp_eth_usd) / cb_eth_usd * 100.0

    return {"lp_eth_usd": lp_eth_usd, "cb_eth_usd": cb_eth_usd, "diff_pct": diff_pct}

def get_prices() -> Dict[str, Any]:
    """
    Return a dict suitable for the /prices command:
      keys: 'ONE','1USDC','1sDAI','TEC','1ETH' (LP values where possible)
            'ETH_COMPARE' -> result of get_eth_prices_lp_vs_cb()
            'errors' -> list[str] (non-fatal notes)
    """
    out: Dict[str, Any] = {"errors": []}

    for sym in ["ONE", "1USDC", "1sDAI", "TEC", "1ETH"]:
        try:
            out[sym] = float(_lp_quote_usd(sym))
        except Exception as e:
            out[sym] = None
            out["errors"].append(f"{sym}: {e}")

    out["ETH_COMPARE"] = get_eth_prices_lp_vs_cb()
    return out

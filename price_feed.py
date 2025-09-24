# /bot/app/price_feed.py
# Single source of truth for prices.
# Pulls ONLY from your on-chain Quoter via app.routes, plus (OPTIONAL) Coinbase ETH/USD for comparison.
# get_prices() -> {
#   "via": "routes.<fn or per_token>",
#   "prices": {SYMBOL: "<value> 1USDC", ...},
#   "comparisons": {
#       "1ETH": {
#           "harmony_1USDC": "<value> 1USDC",
#           "coinbase_usd": "<value> USD",
#           "premium_pct": <decimal string>   # (harmony - coinbase) / coinbase * 100
#       }
#   }
# }

from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Any, Callable, Optional
from web3 import Web3

from app.wallet import get_w3
from app.config import TOKENS, DECIMALS

# OPTIONAL: external only for ETH/USD comparison
try:
    from app.coinbase_client import fetch_eth_usd_price  # must return numeric/str
except Exception:
    fetch_eth_usd_price = None  # gracefully skip if not available


def _decimals(sym: str) -> int:
    try:
        return int(DECIMALS.get(sym, 18))
    except Exception:
        return 18

def _fmt_decimal(x: Decimal, places: int = 6) -> str:
    q = Decimal(10) ** -places
    return str(x.quantize(q, rounding=ROUND_HALF_UP))

def _to_decimal(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))

def _resolve_snapshot_fn(routes_module) -> Optional[Callable[[], Any]]:
    for name in ("quoter_snapshot", "get_quotes", "snapshot", "get_prices_from_quoter"):
        fn = getattr(routes_module, name, None)
        if callable(fn):
            return fn
    return None

def _resolve_quote_fn(routes_module) -> Optional[Callable[..., Any]]:
    for name in ("quote_exact_in", "quote_exact_in_route", "get_amount_out", "get_quote"):
        fn = getattr(routes_module, name, None)
        if callable(fn):
            return fn
    return None

SYMBOLS = ["TEC", "1sDAI", "1ETH", "WONE", "1USDC"]

def _price_from_snapshot_struct(data: Any) -> Optional[Dict[str, str]]:
    out: Dict[str, str] = {}
    try:
        if isinstance(data, dict) and "prices" in data and isinstance(data["prices"], dict):
            for sym, val in data["prices"].items():
                try:
                    out[sym] = f"{_fmt_decimal(_to_decimal(val))} 1USDC"
                except Exception:
                    out[sym] = str(val)
            return out
        if isinstance(data, dict):
            for sym, val in data.items():
                try:
                    out[sym] = f"{_fmt_decimal(_to_decimal(val))} 1USDC"
                except Exception:
                    out[sym] = str(val)
            return out
        if isinstance(data, (list, tuple)):
            for item in data:
                sym, val = item[0], item[1]
                out[str(sym)] = f"{_fmt_decimal(_to_decimal(val))} 1USDC"
            return out
    except Exception:
        pass
    return None

def _addr(sym: str) -> Optional[str]:
    if sym == "ONE":
        return None
    return TOKENS.get(sym)

def _quote_one_token_vs_1usdc(routes_module, quote_fn, w3: Web3, sym: str) -> Optional[str]:
    """
    Compute price of 1 unit of <sym> in 1USDC using the routes quote function.
    We try several calling conventions:
      quote_fn(token_in_addr, token_out_addr, amount_in_wei)
      quote_fn(token_in_sym,  token_out_sym,  amount_in_wei)
    """
    try:
        if sym == "1USDC":
            return "1.000000 1USDC"
        token_in_addr = _addr(sym)
        token_out_addr = _addr("1USDC")
        if sym == "ONE":
            amount_in = 10 ** 18  # 1 ONE in wei
            args_variants = [
                (token_in_addr, token_out_addr, amount_in),
                (sym, "1USDC", amount_in),
            ]
        else:
            amount_in = 10 ** _decimals(sym)
            args_variants = [
                (token_in_addr, token_out_addr, amount_in),
                (sym, "1USDC", amount_in),
            ]

        for args in args_variants:
            try:
                out = quote_fn(*args)
                raw = int(out["amount_out"]) if isinstance(out, dict) and "amount_out" in out else int(out)
                price = Decimal(raw) / (Decimal(10) ** _decimals("1USDC"))
                return f"{_fmt_decimal(price)} 1USDC"
            except TypeError:
                continue
        return None
    except Exception:
        return None

def _parse_price_str_1usdc(s: str) -> Optional[Decimal]:
    """
    Turn '2487.120000 1USDC' -> Decimal('2487.120000').
    """
    try:
        val = s.split()[0]
        return Decimal(val)
    except Exception:
        return None

def get_prices() -> Dict[str, Any]:
    """
    Canonical function used by Telegram and strategies.
    Returns:
      {
        "via": "routes.<fn-name>" or "routes.per_token",
        "prices": {sym: "<value> 1USDC", ...},
        "comparisons": {
            "1ETH": {"harmony_1USDC": "...", "coinbase_usd": "...", "premium_pct": "..."}
        }
      }
    """
    _ = get_w3()  # ensure provider ready
    from app import routes

    # 1) Try a snapshot-style function first (fast path)
    snap_fn = _resolve_snapshot_fn(routes)
    prices: Dict[str, str] = {}
    via = None

    if callable(snap_fn):
        data = snap_fn()
        normalized = _price_from_snapshot_struct(data)
        if normalized:
            prices = normalized
            via = f"routes.{snap_fn.__name__}"

    # 2) Otherwise quote each symbol vs 1USDC with a direct quoter call
    if not prices:
        quote_fn = _resolve_quote_fn(routes)
        if not callable(quote_fn):
            raise RuntimeError("No quoter found in routes (expected snapshot fn or per-token quote fn).")
        for sym in SYMBOLS:
            p = _quote_one_token_vs_1usdc(routes, quote_fn, _, sym)
            if p is not None:
                prices[sym] = p
        if not prices:
            raise RuntimeError("Quoter returned no prices (no viable routes).")
        via = "routes.per_token"

    # ---- ETH comparison (Harmony vs Coinbase) ----
    comparisons: Dict[str, Dict[str, str]] = {}
    try:
        eth_harmony = prices.get("1ETH")
        if eth_harmony and fetch_eth_usd_price is not None:
            cb_usd = _to_decimal(fetch_eth_usd_price())  # assume USD
            hm_usdc = _parse_price_str_1usdc(eth_harmony)
            if hm_usdc is not None and cb_usd > 0:
                premium = (hm_usdc - cb_usd) / cb_usd * Decimal(100)
                comparisons["1ETH"] = {
                    "harmony_1USDC": f"{_fmt_decimal(hm_usdc)} 1USDC",
                    "coinbase_usd": f"{_fmt_decimal(cb_usd)} USD",
                    "premium_pct": _fmt_decimal(premium, 4),
                }
    except Exception:
        # comparison is optional; ignore failures
        pass

    out = {"via": via or "routes", "prices": prices}
    if comparisons:
        out["comparisons"] = comparisons
    return out

def get_price(symbol: str) -> str:
    snap = get_prices()
    prices = snap.get("prices", {})
    if symbol not in prices:
        raise KeyError(f"{symbol} not found in quoter snapshot")
    return prices[symbol]

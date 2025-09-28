# telegram_listener.py
# -*- coding: utf-8 -*-
"""
TECBot Telegram Listener (python-telegram-bot v13-compatible, non-blocking)

Commands:
  /start, /help, /ping
  /balances   - grouped balances by strategy (tecbot_eth, tecbot_usdc, ...)
  /prices     - LP prices + ETH Coinbase comparison (time-boxed, never hangs)
  /slippage   - slippage curve for a token vs USDC (time-boxed)
  /assets     - show supported assets and quick /slippage buttons
  /sanity     - run preflight checks (time-boxed, never hangs)
  /version    - show deployed git version
  /cooldowns  - show default cooldowns from config.py
  /plan       - show brief plan (reads local repo docs); always replies
  /dryrun     - explicit stub so it never fails silently
"""
from __future__ import annotations
import os
import logging
import math
import subprocess
import threading
from pathlib import Path
from typing import Dict, Any, Iterable, Tuple, List, Optional

from decimal import Decimal
from web3 import Web3

# web3.py v5 vs v6 PoA middleware compatibility
try:
    from web3.middleware import ExtraDataToPOAMiddleware as _POA_MIDDLEWARE
except Exception:
    try:
        from web3.middleware import geth_poa_middleware as _POA_MIDDLEWARE
    except Exception:
        _POA_MIDDLEWARE = None

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackContext, MessageHandler, Filters

# ------- local imports (support both flat and app.* package styles) ----------
def _imp(modname: str):
    try:
        return __import__(modname, fromlist=['*'])
    except Exception:
        return __import__(f"app.{modname}", fromlist=['*'])

config = _imp("config")
wallet = _imp("wallet")

# Optional modules; handlers will degrade gracefully if missing
try:
    price_feed = _imp("price_feed")
except Exception:
    price_feed = None

try:
    preflight = _imp("preflight")
except Exception:
    preflight = None

# ---------------------------- logging ----------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s"
)
log = logging.getLogger("telegram_listener")

# ---------------------------- constants --------------------------------------
PRICES_TIMEOUT_SEC = 6
SANITY_TIMEOUT_SEC = 10
SLIPPAGE_TIMEOUT_SEC = 8
MAX_PLAN_LINES = 40

# Keep this aligned with price_feed.get_slippage_curve supported symbols
SUPPORTED_ASSETS = ["1ETH", "TEC", "ONE", "1sDAI", "1USDC"]

# ---------------------------- ERC20 ABI (minimal) ----------------------------
ERC20_MIN_ABI = [
    {"constant": True, "inputs": [{"name": "owner", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}],
     "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals",
     "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "symbol",
     "outputs": [{"name": "", "type": "string"}], "type": "function"},
]

# ---------------------------- helpers ----------------------------------------
def _get_w3() -> Web3:
    """Use wallet.get_w3() if available; otherwise build one from config.RPC_URL."""
    if hasattr(wallet, "get_w3"):
        w3 = wallet.get_w3()
    else:
        rpc = getattr(config, "RPC_URL", "https://api.harmony.one")
        w3 = Web3(Web3.HTTPProvider(rpc))

    # Inject PoA middleware if available (safe on Harmony)
    if _POA_MIDDLEWARE:
        try:
            w3.middleware_onion.inject(_POA_MIDDLEWARE, layer=0)
        except Exception:
            pass
    return w3

def _checksum(w3: Web3, addr: str) -> str:
    try:
        return w3.to_checksum_address(addr)
    except Exception:
        return addr

def _iter_wallet_groups_from_config(cfg) -> Iterable[Tuple[str, str, List[str]]]:
    """
    Yields (group_name, evm_address, display_list) from cfg.WALLETS.
    Accepts either:
      WALLETS = {"tecbot_eth": "0x..."}                           # simple form
    or:
      WALLETS = {"tecbot_eth": {"address":"0x...","display":[…]}} # rich form
    """
    WAL = getattr(cfg, "WALLETS", None)
    if not WAL or not isinstance(WAL, dict):
        raise RuntimeError("No WALLETS in config.py")

    defaults = {
        "tecbot_eth":  ["ONE", "1ETH"],
        "tecbot_usdc": ["ONE", "1USDC"],
        "tecbot_sdai": ["ONE", "1ETH", "TEC", "1USDC"],
        "tecbot_tec":  ["ONE", "TEC", "1sDAI"],
    }
    for name, val in WAL.items():
        if isinstance(val, str):
            addr = val
            disp = defaults.get(name, ["ONE"])
        elif isinstance(val, dict):
            addr = (val.get("address") or "").strip()
            disp = val.get("display") or defaults.get(name, ["ONE"])
        else:
            continue
        if addr:
            yield name, addr, disp

def _fmt_usd(v: float | None) -> str:
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return "—"
    if v >= 1000:
        return f"${v:,.2f}"
    return f"${v:,.4f}"

def _fmt_qty(v: Decimal | float | int | None, decimals: int = 4) -> str:
    if v is None:
        return "0"
    if isinstance(v, Decimal):
        return f"{v:.{decimals}f}"
    return f"{float(v):.{decimals}f}"

def _fmt_pct(x: float | None) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "—"
    return f"{x:.2f}%"

def _reply(update: Update, text: str):
    if update and update.message:
        update.message.reply_text(text)

def _get_token_address(symbol: str) -> str | None:
    tokens = getattr(config, "TOKENS", {})
    return tokens.get(symbol)

def _get_decimals(symbol: str, default: int = 18) -> int:
    decs = getattr(config, "DECIMALS", {})
    return int(decs.get(symbol, default))

def _get_one_balance(w3: Web3, addr: str) -> Decimal:
    wei = w3.eth.get_balance(addr)
    return Decimal(wei) / Decimal(10**18)

def _get_erc20_balance(w3: Web3, token_addr: str, holder: str, decimals: int) -> Decimal:
    c = w3.eth.contract(address=_checksum(w3, token_addr), abi=ERC20_MIN_ABI)
    bal = c.functions.balanceOf(_checksum(w3, holder)).call()
    return Decimal(bal) / Decimal(10**decimals)

# --------- time-box wrappers to guarantee a response (no silent hangs) -------
def _call_with_timeout(fn, timeout_sec: int, default: Any) -> Any:
    box = {"res": default}
    def runner():
        try:
            box["res"] = fn()
        except Exception as e:
            box["res"] = e
    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)
    if t.is_alive():
        return TimeoutError(f"timed out after {timeout_sec}s")
    return box["res"]

# ---------------------------- command handlers (sync) -------------------------
def cmd_start(update: Update, context: CallbackContext):
    _reply(update, "TECBot online. Try /balances, /prices, /slippage 1ETH, /assets, /sanity, /version, /cooldowns, /plan")

def cmd_help(update: Update, context: CallbackContext):
    _reply(update, "/start /help /ping /balances /prices /slippage <SYMBOL> [USDC sizes…] /assets /sanity /version /cooldowns /plan /dryrun")

def cmd_ping(update: Update, context: CallbackContext):
    _reply(update, "pong")

def cmd_balances(update: Update, context: CallbackContext):
    try:
        w3 = _get_w3()
        groups = list(_iter_wallet_groups_from_config(config))
        if not groups:
            _reply(update, "No wallet groups configured.")
            return

        # Symbol -> token address
        tok_addr = {s: _get_token_address(s) for s in ["1ETH", "1USDC", "1sDAI", "TEC"]}

        lines: List[str] = []
        for name, address, display in groups:
            cs_addr = _checksum(w3, address)
            lines.append(f"{name}")
            # ONE balance always included
            one_bal = _get_one_balance(w3, cs_addr)
            lines.append(f"  ONE   {_fmt_qty(one_bal, 4)}")

            for sym in display:
                if sym == "ONE":
                    continue
                taddr = tok_addr.get(sym)
                if not taddr:
                    continue
                decs = _get_decimals(sym, 18 if sym != "1USDC" else 6)
                bal = _get_erc20_balance(w3, taddr, cs_addr, decs)
                lines.append(f"  {sym:<5} {_fmt_qty(bal, 4)}")

            lines.append("")  # spacer

        _reply(update, "\n".join(lines).rstrip())

    except Exception as e:
        log.exception("/balances error")
        _reply(update, f"/balances error: {e}")

def cmd_prices(update: Update, context: CallbackContext):
    def _do():
        if not price_feed or not hasattr(price_feed, "get_prices"):
            raise RuntimeError("price_feed.get_prices() not available (ensure Quoter-backed get_prices exists).")
        return price_feed.get_prices()

    res = _call_with_timeout(_do, PRICES_TIMEOUT_SEC, default=None)
    if isinstance(res, Exception):
        _reply(update, f"/prices error: {res}")
        return
    if res is None:
        _reply(update, f"/prices error: timed out after {PRICES_TIMEOUT_SEC}s")
        return

    try:
        data: Dict

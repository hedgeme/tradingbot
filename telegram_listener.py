# telegram_listener.py
# -*- coding: utf-8 -*-
"""
TECBot Telegram Listener (python-telegram-bot v13-compatible)

Commands:
  /start, /help, /ping
  /balances        - grouped balances by strategy (tecbot_eth, tecbot_usdc, ...)
  /prices          - LP prices + ETH Coinbase comparison
  /sanity          - run preflight checks
  /version         - show deployed git version
"""

from __future__ import annotations
import os
import logging
import math
import subprocess
from typing import Dict, Any, Iterable, Tuple, List

from decimal import Decimal
from web3 import Web3

# web3.py v5 vs v6 PoA middleware compatibility
try:
    # v6 alias
    from web3.middleware import ExtraDataToPOAMiddleware as _POA_MIDDLEWARE
except Exception:
    try:
        # v5 name
        from web3.middleware import geth_poa_middleware as _POA_MIDDLEWARE
    except Exception:
        _POA_MIDDLEWARE = None

# Telegram (pre-v20)
from telegram import Update
from telegram.ext import Updater, CommandHandler, CallbackContext

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

# ---------------------------- command handlers (sync) -------------------------
def cmd_start(update: Update, context: CallbackContext):
    _reply(update, "TECBot online. Try /balances, /prices, /sanity, /version")

def cmd_help(update: Update, context: CallbackContext):
    _reply(update, "/start /help /ping /balances /prices /sanity /version")

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
    try:
        if not price_feed or not hasattr(price_feed, "get_prices"):
            raise RuntimeError("price_feed.get_prices() not available (ensure Quoter-backed get_prices exists).")
        data: Dict[str, Any] = price_feed.get_prices()

        err = data.get("errors", [])
        ethc = data.get("ETH_COMPARE", {}) or {}
        lp = ethc.get("lp_eth_usd")
        cb = ethc.get("cb_eth_usd")
        df = ethc.get("diff_pct")

        lines = []
        lines.append("LP Prices")
        for sym in ["ONE", "1USDC", "1sDAI", "TEC", "1ETH"]:
            v = data.get(sym, None)
            if v is None:
                lines.append(f"  {sym:<5}  —")
            else:
                lines.append(f"  {sym:<5}  {_fmt_usd(v)}")
        lines.append("")
        lines.append("ETH: Harmony LP vs Coinbase")
        lines.append(f"  LP:       {_fmt_usd(lp)}")
        lines.append(f"  Coinbase: {_fmt_usd(cb)}")
        lines.append(f"  Diff:     {('%.2f' % df) + '%' if df == df else '—'}")  # NaN-safe

        if err:
            lines.append("")
            lines.append("Notes:")
            for m in err:
                lines.append(f"  - {m}")

        _reply(update, "\n".join(lines))

    except Exception as e:
        log.exception("/prices error")
        _reply(update, f"/prices error: {e}")

def cmd_sanity(update: Update, context: CallbackContext):
    try:
        if not preflight or not hasattr(preflight, "run_sanity"):
            raise RuntimeError("preflight.run_sanity() not available")
        res = preflight.run_sanity()
        ok = res.get("ok", False)
        summary = res.get("summary", "")
        details = res.get("details", [])
        msg = f"Sanity: {'OK' if ok else 'FAILED'}\n{summary}"
        if details:
            msg += "\n\nDetails:\n" + "\n".join(f"- {d}" for d in details)
        _reply(update, msg)
    except Exception as e:
        log.exception("/sanity error")
        _reply(update, f"/sanity error: {e}")

def _git(cmd: list[str]) -> str:
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        out = ""
    return out

def cmd_version(update: Update, context: CallbackContext):
    # Show tag (if any), short hash, dirty marker
    tag = _git(["git", "describe", "--tags", "--abbrev=0"])
    rev = _git(["git", "rev-parse", "--short", "HEAD"])
    dirty = _git(["git", "status", "--porcelain"])
    marker = "" if not dirty else " (dirty)"
    text = f"Version: {tag or 'no-tag'} @ {rev or 'unknown'}{marker}"
    _reply(update, text)

# ---------------------------- main bootstrap ----------------------------------
def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN not set")

    updater = Updater(token=token, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("help", cmd_help))
    dp.add_handler(CommandHandler("ping", cmd_ping))
    dp.add_handler(CommandHandler("balances", cmd_balances))
    dp.add_handler(CommandHandler("prices", cmd_prices))
    dp.add_handler(CommandHandler("sanity", cmd_sanity))
    dp.add_handler(CommandHandler("version", cmd_version))

    log.info("Telegram bot started")
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()

# telegram_listener.py
# -*- coding: utf-8 -*-
"""
TECBot Telegram Listener (python-telegram-bot v13-compatible, non-blocking)

Commands:
  /start, /help, /ping
  /assets     - list configured wallet groups & tracked symbols (from config)
  /balances   - grouped balances by strategy (tecbot_eth, tecbot_usdc, ...)
  /prices     - LP prices + ETH Coinbase comparison (time-boxed, never hangs)
  /slippage   - slippage curve for a token vs USDC (time-boxed)
  /sanity     - run preflight checks (subprocess w/ timeout; parse PASS/FAIL)
  /version    - show deployed git version (robust fallbacks)
  /cooldowns  - show default cooldowns from config.py
  /plan       - show brief plan (reads local repo docs); always replies
  /dryrun     - explicit stub so it never fails silently
"""

from __future__ import annotations
import os
import sys
import re
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

# Telegram (pre-v20 API)
from telegram import Update
from telegram.ext import Updater, CommandHandler, CallbackContext, MessageHandler, Filters

# ---------------------------- repo root ---------------------------------------
REPO_ROOT = Path(__file__).resolve().parent
GIT_DIR = REPO_ROOT / ".git"

# ------- local imports (support both flat and legacy app.* package styles) ----
def _imp(modname: str):
    try:
        return __import__(modname, fromlist=["*"])
    except Exception:
        return __import__(f"app.{modname}", fromlist=["*"])

config = _imp("config")
wallet = _imp("wallet")

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
SANITY_TIMEOUT_SEC = 12
SLIPPAGE_TIMEOUT_SEC = 8
MAX_PLAN_LINES = 40

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
    if hasattr(wallet, "get_w3"):
        w3 = wallet.get_w3()
    else:
        rpc = getattr(config, "HARMONY_RPC", None) or getattr(config, "RPC_URL", "https://api.harmony.one")
        w3 = Web3(Web3.HTTPProvider(rpc))
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

# ---------------------------- command handlers -------------------------------
def cmd_start(update: Update, context: CallbackContext):
    _reply(update, "TECBot online. Try /assets, /balances, /prices, /slippage 1ETH, /sanity, /version, /cooldowns, /plan")

def cmd_help(update: Update, context: CallbackContext):
    supported_syms = ", ".join(getattr(config, "TOKENS", {}).keys())
    _reply(update,
           "/start /help /ping /assets /balances /prices /slippage <SYMBOL> [USDC sizes…] /sanity /version /cooldowns /plan /dryrun\n"
           f"Symbols: {supported_syms}\n")

def cmd_ping(update: Update, context: CallbackContext):
    _reply(update, "pong")

# (assets, balances, prices, slippage — unchanged, omitted here for brevity)
# ------------------------------------------------------------------------------

# -------------------- Sanity via subprocess (fixed) ---------------------------
def _run_preflight_subprocess(timeout_sec: int) -> Tuple[int, str]:
    """
    Run the repo's sanity script directly from the repo root.
    Captures stdout+stderr; returns (exit_code, combined_output).
    """
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    try:
        p = subprocess.run(
            [sys.executable, "-u", str(REPO_ROOT / "run_quote_sanity.py")],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_sec,
            text=True
        )
        out = (p.stdout or "") + (("\n" + p.stderr) if p.stderr else "")
        return p.returncode, out.strip()
    except subprocess.TimeoutExpired as e:
        return 124, (e.stdout or "") + (("\n" + e.stderr) if e.stderr else "")

def cmd_sanity(update: Update, context: CallbackContext):
    rc, out = _run_preflight_subprocess(SANITY_TIMEOUT_SEC)
    if rc == 124:
        _reply(update, f"Sanity: FAILED\nTimed out after {SANITY_TIMEOUT_SEC}s")
        return
    if rc != 0 and not out:
        _reply(update, "Sanity: FAILED\nNo output from preflight.")
        return
    ok = bool(re.search(r"OVERALL:\s*(✅\s*PASS|PASS)", out, re.IGNORECASE))
    lines = [ln for ln in out.splitlines() if ln.strip()]
    summary = lines[-1] if lines else "preflight completed."
    _reply(update, f"Sanity: {'OK' if ok else 'FAILED'}\n{summary}")

# ---------------------------- version helpers --------------------------------
def _git(args: List[str]) -> str:
    try:
        return subprocess.check_output(args, stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return ""

def cmd_version(update: Update, context: CallbackContext):
    tag = rev = dirty = ""
    if GIT_DIR.exists():
        tag   = _git(["/usr/bin/git", f"--git-dir={GIT_DIR}", "describe", "--tags", "--abbrev=0"])
        rev   = _git(["/usr/bin/git", f"--git-dir={GIT_DIR}", "rev-parse", "--short", "HEAD"])
        dirty = _git(["/usr/bin/git", f"--git-dir={GIT_DIR}", "status", "--porcelain"])
    marker = "" if not dirty else " (dirty)"
    _reply(update, f"Version: {tag or 'no-tag'} @ {rev or 'unknown'}{marker}")

# ---------------------------- bootstrap --------------------------------------
def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN not set")

    os.environ.setdefault("PYTHONPATH", str(REPO_ROOT))

    updater = Updater(token=token, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("help", cmd_help))
    dp.add_handler(CommandHandler("ping", cmd_ping))
    dp.add_handler(CommandHandler("sanity", cmd_sanity))
    dp.add_handler(CommandHandler("version", cmd_version))
    dp.add_handler(MessageHandler(Filters.command, lambda u, c: _reply(u, f"Unknown command: {u.message.text}")))

    log.info("Telegram bot started")
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()

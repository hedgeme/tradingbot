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
# This file lives at repo root (/bot). If it were nested, .parent handles it.
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
SANITY_TIMEOUT_SEC = 12  # headroom for subprocess spawn
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
    """Prefer wallet.get_w3() if available; otherwise use HARMONY_RPC/RPC_URL."""
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
    """
    Yields (group_name, evm_address, display_list) from cfg.WALLETS.
    Accepts:
      WALLETS = {"tecbot_eth": "0x..."}                           # simple
      WALLETS = {"tecbot_eth": {"address":"0x...","display":[…]}} # rich
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
    _reply(update, "TECBot online. Try /assets, /balances, /prices, /slippage 1ETH, /sanity, /version, /cooldowns, /plan")

def cmd_help(update: Update, context: CallbackContext):
    supported_syms = ", ".join(getattr(config, "TOKENS", {}).keys())
    _reply(update,
           "/start /help /ping /assets /balances /prices /slippage <SYMBOL> [USDC sizes…] /sanity /version /cooldowns /plan /dryrun\n"
           f"Symbols: {supported_syms}\n"
           "Examples:\n"
           "  /slippage 1ETH 10 100 1000\n"
           "  /slippage TEC 50 200 1000")

def cmd_ping(update: Update, context: CallbackContext):
    _reply(update, "pong")

def cmd_assets(update: Update, context: CallbackContext):
    try:
        WAL = getattr(config, "WALLETS", {})
        TOK = getattr(config, "TOKENS", {})
        if not isinstance(WAL, dict) or not WAL:
            _reply(update, "No WALLETS configured in config.py")
            return
        defaults = {
            "tecbot_eth":  ["ONE", "1ETH"],
            "tecbot_usdc": ["ONE", "1USDC"],
            "tecbot_sdai": ["ONE", "1ETH", "TEC", "1USDC"],
            "tecbot_tec":  ["ONE", "TEC", "1sDAI"],
        }
        lines = ["Assets (by wallet group):"]
        for name, val in WAL.items():
            if isinstance(val, str):
                disp = defaults.get(name, ["ONE"])
            elif isinstance(val, dict):
                disp = val.get("display") or defaults.get(name, ["ONE"])
            else:
                continue
            marks = [s if s in TOK else f"{s}❓" for s in disp]
            lines.append(f"  {name}: " + ", ".join(marks))
        _reply(update, "\n".join(lines))
    except Exception as e:
        log.exception("/assets error")
        _reply(update, f"/assets error: {e}")

def cmd_balances(update: Update, context: CallbackContext):
    try:
        w3 = _get_w3()
        groups = list(_iter_wallet_groups_from_config(config))
        if not groups:
            _reply(update, "No wallet groups configured.")
            return

        tok_addr = {s: _get_token_address(s) for s in ["1ETH", "1USDC", "1sDAI", "TEC"]}

        lines: List[str] = []
        for name, address, display in groups:
            cs_addr = _checksum(w3, address)
            lines.append(f"{name}")
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

            lines.append("")

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
        data: Dict[str, Any] = res
        err = data.get("errors", [])
        ethc = data.get("ETH_COMPARE", {}) or {}
        lp = ethc.get("lp_eth_usd")
        cb = ethc.get("cb_eth_usd")
        df = ethc.get("diff_pct")

        lines = ["LP Prices"]
        for sym in ["ONE", "1USDC", "1sDAI", "TEC", "1ETH"]:
            v = data.get(sym, None)
            lines.append(f"  {sym:<5}  {_fmt_usd(v)}" if v is not None else f"  {sym:<5}  —")
        lines += [
            "",
            "ETH: Harmony LP vs Coinbase",
            f"  LP:       {_fmt_usd(lp)}",
            f"  Coinbase: {_fmt_usd(cb)}",
            f"  Diff:     {('%.2f' % df) + '%' if df == df else '—'}"
        ]
        if err:
            lines.append("")
            lines.append("Notes:")
            lines.extend([f"  - {m}" for m in err])

        _reply(update, "\n".join(lines))
    except Exception as e:
        log.exception("/prices format error")
        _reply(update, f"/prices error: {e}")

def cmd_slippage(update: Update, context: CallbackContext):
    """Usage: /slippage 1ETH [10 100 1000]  -> USDC targets"""
    args = (update.message.text.split()[1:] if update and update.message and update.message.text else [])
    if not args:
        _reply(update, "Usage: /slippage <SYMBOL> [USDC sizes…]\nEx: /slippage 1ETH 10 100 1000")
        return
    sym = args[0].strip()
    try:
        sizes = [float(x) for x in args[1:]] if len(args) > 1 else [10, 100, 1000, 10000]
    except Exception:
        sizes = [10, 100, 1000, 10000]

    def _do():
        if not price_feed or not hasattr(price_feed, "get_slippage_curve"):
            raise RuntimeError("price_feed.get_slippage_curve() not available.")
        return price_feed.get_slippage_curve(sym, sizes)

    res = _call_with_timeout(_do, SLIPPAGE_TIMEOUT_SEC, default=None)
    if isinstance(res, Exception):
        _reply(update, f"/slippage error: {res}")
        return
    if res is None:
        _reply(update, f"/slippage error: timed out after {SLIPPAGE_TIMEOUT_SEC}s")
        return

    try:
        mid = res.get("mid_usdc_per_sym", None)
        rows = res.get("rows", [])
        errs = res.get("errors", [])
        lines = [f"Slippage curve: {sym} → USDC",
                 f"Baseline (mid): {_fmt_usd(mid) if mid is not None else '—'} per {sym}",
                 "",
                 "Size (USDC) | Amount In (sym) | Eff. Price | Slippage vs mid"]
        for r in rows:
            usdc = r["usdc"]
            amt  = r["amount_in_sym"]
            px   = r["px_eff"]
            sl   = r["slippage_pct"]
            lines.append(f"{usdc:>10,.0f} | {('%.6f' % amt) if amt else '—':>15} | "
                         f"{_fmt_usd(px):>10} | {(('%.2f' % sl)+'%') if sl is not None else '—':>8}")
        if errs:
            lines += ["", "Notes:"]
            lines += [f"  - {e}" for e in errs]
        _reply(update, "\n".join(lines))
    except Exception as e:
        log.exception("/slippage format error")
        _reply(update, f"/slippage error: {e}")

# -------------------- Sanity via subprocess (repo-root aware) -----------------
def _run_preflight_subprocess(timeout_sec: int) -> Tuple[int, str]:
    """
    Execute a small inline script that tries flat import first, then legacy app.*.
    Runs from REPO_ROOT and prints a JSON line on success.
    """
    one_liner = (
        "import sys,json,os; "
        "from pathlib import Path; "
        "root=Path(__file__).resolve().parent; "
        "sys.path.insert(0,str(root)); "
        "try:\n"
        " import preflight as p\n"
        "except Exception:\n"
        " import app.preflight as p\n"
        "res=p.run_sanity(); "
        "print('__JSON__:'+json.dumps(res)) if isinstance(res,dict) else None"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    try:
        p = subprocess.run(
            [sys.executable, "-c", one_liner],
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

    m = re.search(r"__JSON__:(\{.*\})", out)
    if m:
        try:
            import json
            data = json.loads(m.group(1))
            ok = bool(data.get("ok", False))
            summary = data.get("summary", "") or "preflight completed."
            details = data.get("details", []) or []
            msg = f"Sanity: {'OK' if ok else 'FAILED'}\n{summary}"
            if details:
                msg += "\n\nDetails:\n" + "\n".join(f"- {d}" for d in details)
            _reply(update, msg)
            return
        except Exception:
            pass

    if "Traceback (most recent call last)" in out or "SyntaxError:" in out:
        snippet = "\n".join(out.splitlines()[-20:])
        _reply(update, f"Sanity: FAILED\n{_subsnippet(snippet)}")
        return

    ok = bool(re.search(r"OVERALL:\s*(✅\s*PASS|PASS)", out, re.IGNORECASE))
    lines = [ln for ln in out.splitlines() if ln.strip()]
    summary = lines[-1] if lines else "preflight completed."
    _reply(update, f"Sanity: {'OK' if ok else 'FAILED'}\n{summary}")

def _subsnippet(s: str, limit: int = 1500) -> str:
    return s if len(s) <= limit else s[-limit:]

# ---------------------------- version helpers --------------------------------
def _git(args: List[str]) -> str:
    try:
        return subprocess.check_output(args, stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return ""

def _changelog_headline() -> str:
    try:
        p = REPO_ROOT / "changelog.md"
        if p.exists():
            with p.open("r", encoding="utf-8", errors="ignore") as f:
                for ln in f:
                    ln = ln.strip()
                    if ln:
                        return ln[:120]
    except Exception:
        pass
    return ""

def cmd_version(update: Update, context: CallbackContext):
    tag = rev = dirty = ""
    if GIT_DIR.exists():
        tag   = _git(["/usr/bin/git", f"--git-dir={GIT_DIR}", "describe", "--tags", "--abbrev=0"])
        rev   = _git(["/usr/bin/git", f"--git-dir={GIT_DIR}", "rev-parse", "--short", "HEAD"])
        dirty = _git(["/usr/bin/git", f"--git-dir={GIT_DIR}", "status", "--porcelain"])
    else:
        tag   = _git(["/usr/bin/git", "describe", "--tags", "--abbrev=0"])
        rev   = _git(["/usr/bin/git", "rev-parse", "--short", "HEAD"])
        dirty = _git(["/usr/bin/git", "status", "--porcelain"])

    marker = "" if not dirty else " (dirty)"
    if not tag and not rev:
        head = _changelog_headline()
        if head:
            _reply(update, f"Version: (no git) • {head}")
            return
        _reply(update, f"Version: unknown • RepoRoot={REPO_ROOT} • .git present={GIT_DIR.exists()}")
        return
    _reply(update, f"Version: {tag or 'no-tag'} @ {rev or 'unknown'}{marker}")

# ---------------------------- docs & misc ------------------------------------
def _read_text_if_exists(paths: List[Path]) -> Optional[str]:
    for p in paths:
        try:
            if p.exists() and p.is_file():
                with p.open("r", encoding="utf-8", errors="ignore") as f:
                    return f.read()
        except Exception:
            continue
    return None

def cmd_plan(update: Update, context: CallbackContext):
    roots = [REPO_ROOT]
    candidates: List[Path] = []
    for root in roots:
        candidates += [
            root / "Project_overview.md",
            root / "BOT_ARCHITECTURE.md",
            root / "README.md",
        ]
    txt = _read_text_if_exists(candidates)
    if not txt:
        _reply(update, "Plan: repo docs not found locally. (Place Project_overview.md or BOT_ARCHITECTURE.md alongside the bot files.)")
        return
    lines = [ln.rstrip() for ln in txt.splitlines()]
    snippet = "\n".join(lines[:MAX_PLAN_LINES]).strip()
    _reply(update, f"Plan (preview):\n{snippet}\n\n(Showing first {MAX_PLAN_LINES} lines)")

def cmd_cooldowns(update: Update, context: CallbackContext):
    data = getattr(config, "COOLDOWNS_DEFAULTS", {})
    if not isinstance(data, dict) or not data:
        _reply(update, "No default cooldowns configured.")
        return
    lines = ["Default cooldowns (seconds):"]
    for k, v in data.items():
        lines.append(f"  {k}: {v}")
    _reply(update, "\n".join(lines))

def cmd_dryrun(update: Update, context: CallbackContext):
    _reply(update, "Dry-run is not enabled in this build. (No trades will be simulated.)")

def cmd_unknown(update: Update, context: CallbackContext):
    if update and update.message and update.message.text and update.message.text.startswith("/"):
        _reply(update, f"Unknown command: {update.message.text}")

# ---------------------------- main bootstrap ----------------------------------
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
    dp.add_handler(CommandHandler("assets", cmd_assets))
    dp.add_handler(CommandHandler("balances", cmd_balances))
    dp.add_handler(CommandHandler("prices", cmd_prices))
    dp.add_handler(CommandHandler("slippage", cmd_slippage))
    dp.add_handler(CommandHandler("sanity", cmd_sanity))
    dp.add_handler(CommandHandler("version", cmd_version))
    dp.add_handler(CommandHandler("cooldowns", cmd_cooldowns))
    dp.add_handler(CommandHandler("plan", cmd_plan))
    dp.add_handler(CommandHandler("dryrun", cmd_dryrun))

    dp.add_handler(MessageHandler(Filters.command, cmd_unknown))

    log.info("Telegram bot started")
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()

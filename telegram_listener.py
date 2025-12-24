#!/usr/bin/env python3
# /bot/telegram_listener.py
# TECBot Telegram Listener — SINGLE-SUMMARY MESSAGING GUARANTEED
#
# Fixes included (based on your reported problems):
# 1) /trade wizard balance lookup is now CASE-SAFE (fixes 1sDAI showing 0 when wallet has 1sDAI)
# 2) Callback queries are answered immediately to avoid “Query is too old…”
# 3) Gas expense displayed human-readable: gasUsed + ~cost in ONE (when available)
# 4) Manual /trade + /withdraw flows suppress app.alert Telegram sends to prevent duplicates
#
# NOTE: Full raw file replacement.

import os
import sys
import logging
import subprocess
import shlex
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Optional, List, Dict, Tuple, Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ParseMode, Update
from telegram.ext import (
    Updater,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    Filters,
    CallbackContext,
)

from web3 import Web3

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
log = logging.getLogger("telegram_listener")

# Ensure /bot imports work
if "/bot" not in sys.path:
    sys.path.insert(0, "/bot")

# -----------------------------------------------------------------------------
# Imports (prefer app.*, fallback root)
# -----------------------------------------------------------------------------
try:
    from app import config as C
    log.info("Loaded config from app.config")
except Exception:
    import config as C  # type: ignore
    log.info("Loaded config from root config")

PR = BL = SL = None
try:
    from app import prices as PR
    log.info("Loaded prices from app.prices")
except Exception as e:
    log.warning("prices module not available: %s", e)

try:
    from app import balances as BL
    log.info("Loaded balances from app.balances")
except Exception as e:
    log.warning("balances module not available: %s", e)

try:
    from app import slippage as SL
    log.info("Loaded slippage from app.slippage")
except Exception as e:
    log.warning("slippage module not available: %s", e)

planner = None
try:
    from app.strategies import planner
    log.info("Loaded planner from app.strategies.planner")
except Exception:
    try:
        from strategies import planner  # type: ignore
        log.info("Loaded planner from root strategies.planner")
    except Exception as e:
        log.warning("planner module not available: %s", e)
        planner = None

runner = None
try:
    from app import runner
    log.info("Loaded runner from app.runner")
except Exception:
    try:
        import runner  # type: ignore
        log.info("Loaded runner from root runner")
    except Exception as e:
        log.warning("runner module not available: %s", e)
        runner = None

W = None
try:
    from app import wallet as W
    log.info("Loaded wallet from app.wallet")
except Exception:
    try:
        import wallet as W  # type: ignore
        log.info("Loaded wallet from root wallet")
    except Exception as e:
        log.warning("wallet module not available: %s", e)
        W = None  # type: ignore

TE = None
try:
    from app import trade_executor as TE
    log.info("Loaded trade_executor from app.trade_executor")
except Exception:
    try:
        import trade_executor as TE  # type: ignore
        log.info("Loaded trade_executor from root trade_executor")
    except Exception as e:
        log.warning("trade_executor not available: %s", e)
        TE = None  # type: ignore

ALERTMOD = None
try:
    from app import alert as ALERTMOD
except Exception:
    try:
        import alert as ALERTMOD  # type: ignore
    except Exception:
        ALERTMOD = None  # type: ignore

# -----------------------------------------------------------------------------
# Constants / WONE unwrap helper ABI
# -----------------------------------------------------------------------------
try:
    _cfg_wone = (getattr(C, "TOKENS", {}) or {}).get("WONE", "")
    if _cfg_wone:
        WONE_ADDR = Web3.to_checksum_address(_cfg_wone)
    else:
        WONE_ADDR = Web3.to_checksum_address("0xcF664087a5bB0237a0BAd6742852ec6c8d69A27a")
except Exception:
    WONE_ADDR = Web3.to_checksum_address("0xcF664087a5bB0237a0BAd6742852ec6c8d69A27a")

WONE_ABI = [
    {
        "name": "withdraw",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "wad", "type": "uint256"}],
        "outputs": [],
    }
]

# -----------------------------------------------------------------------------
# Small helpers
# -----------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def _norm_tx_hash(txh: str) -> str:
    if not txh:
        return ""
    txh = str(txh).strip()
    if not txh.startswith("0x"):
        txh = "0x" + txh
    return txh

def _git_short_rev() -> Optional[str]:
    try:
        out = subprocess.check_output(shlex.split("git rev-parse --short HEAD"), cwd="/bot", stderr=subprocess.DEVNULL)
        return out.decode().strip()
    except Exception:
        return None

def is_admin(user_id: int) -> bool:
    try:
        return int(user_id) in set(int(x) for x in (getattr(C, "ADMIN_USER_IDS", []) or []))
    except Exception:
        return False

def _fmt_money(x: Optional[Decimal]) -> str:
    if x is None:
        return "—"
    x = Decimal(str(x))
    if x >= 1000:
        return f"${x:,.2f}"
    if x >= 1:
        s = f"{x:.5f}"
        return f"${s.rstrip('0').rstrip('.') if '.' in s else s}"
    return f"${x:,.5f}"

def _fmt_amt(sym: str, val) -> str:
    try:
        d = Decimal(str(val))
    except Exception:
        return str(val)
    if sym.upper() == "1ETH":
        q = Decimal("0.00000001")
        return f"{d.quantize(q, rounding=ROUND_DOWN):f}"
    q = Decimal("0.01")
    return f"{d.quantize(q, rounding=ROUND_DOWN):.2f}"

def _fmt_gas_summary(gas_used: Optional[int], gas_one: Optional[Decimal]) -> str:
    try:
        gu = int(gas_used or 0)
    except Exception:
        gu = 0
    cost = None
    if gas_one is not None:
        try:
            cost = Decimal(str(gas_one))
        except Exception:
            cost = None
    if gu <= 0 and cost is None:
        return "Gas: —"
    if cost is None:
        return f"Gas: used {gu:,}"
    return f"Gas: used {gu:,} · cost ~{cost:.6f} ONE"

def _estimate_gas_cost_one(gas_est: Optional[int]) -> Optional[Decimal]:
    """Estimate gas cost in ONE using current gas price (best-effort)."""
    try:
        ge = int(gas_est or 0)
    except Exception:
        ge = 0
    if ge <= 0:
        return None

    gas_price_wei = None
    try:
        if TE is not None and hasattr(TE, "_current_gas_price_wei_capped"):
            gas_price_wei = int(TE._current_gas_price_wei_capped())
        elif W is not None and hasattr(W, "suggest_gas_price_wei"):
            gas_price_wei = int(W.suggest_gas_price_wei())
    except Exception:
        gas_price_wei = None

    if not gas_price_wei or gas_price_wei <= 0:
        return None

    try:
        return (Decimal(ge) * Decimal(gas_price_wei)) / (Decimal(10) ** 18)
    except Exception:
        return None

def _fmt_gas_est_line(gas_est: Optional[int]) -> str:
    """Render gas estimate including approximate ONE cost when possible."""
    try:
        ge = int(gas_est or 0)
    except Exception:
        ge = 0
    if ge <= 0:
        return "Gas Est  : —"
    est_one = _estimate_gas_cost_one(ge)
    if est_one is None:
        return f"Gas Est  : {ge:,}"
    return f"Gas Est  : {ge:,} · cost ~{Decimal(str(est_one)):.6f} ONE"

@contextmanager
def _suppress_alerts_temporarily():
    """
    Prevent duplicate Telegram messages from app.alert during manual wizard flows.
    We blank TELEGRAM_CHAT_ID so app.alert._send becomes a no-op.
    """
    if ALERTMOD is None:
        yield
        return
    try:
        old_chat = getattr(ALERTMOD, "TELEGRAM_CHAT_ID", "")
        setattr(ALERTMOD, "TELEGRAM_CHAT_ID", "")
        yield
    finally:
        try:
            setattr(ALERTMOD, "TELEGRAM_CHAT_ID", old_chat)
        except Exception:
            pass

# -----------------------------------------------------------------------------
# Balances: canonical keys + safe lookups
# -----------------------------------------------------------------------------
_ONE_KEY_ORDER = [
    "ONE(native)", "ONE (native)", "ONE_NATIVE", "NATIVE_ONE", "NATIVE",
    "ONE", "WONE"
]

def _resolve_one_value(row: Dict[str, Any]) -> Decimal:
    lower = {k.lower(): k for k in row.keys()}
    for key in _ONE_KEY_ORDER:
        k = lower.get(key.lower())
        if k is not None:
            try:
                return Decimal(str(row[k]))
            except Exception:
                pass
    return Decimal("0")

def _row_get_token_amount(row: Dict[str, Any], token_symbol: str) -> Decimal:
    """
    Case-safe token balance lookup.

    Fixes the exact bug you reported:
    - Wizard used token.upper() ('1SDAI') but balances row uses '1sDAI'
    """
    if not row:
        return Decimal("0")

    sym = (token_symbol or "").strip()
    if not sym:
        return Decimal("0")

    # Native ONE special
    if sym.upper() == "ONE":
        return _resolve_one_value(row)

    # Build case-insensitive map of keys -> original key
    keymap = {k.upper(): k for k in row.keys()}

    # Handle Harmony mixed-case token
    if sym.upper() == "1SDAI":
        # try common variants
        for cand in ("1sDAI", "1SDAI", "1SDAi"):
            k = keymap.get(cand.upper())
            if k is not None:
                try:
                    return Decimal(str(row.get(k, 0)))
                except Exception:
                    return Decimal("0")
        return Decimal("0")

    # General case-insensitive lookup
    k = keymap.get(sym.upper())
    if k is None:
        return Decimal("0")
    try:
        return Decimal(str(row.get(k, 0)))
    except Exception:
        return Decimal("0")

# -----------------------------------------------------------------------------
# Render helpers
# -----------------------------------------------------------------------------
def render_plan(actions_by_bot) -> str:
    if not actions_by_bot or not any(actions_by_bot.values()):
        return f"Plan (preview @ {now_iso()})\nNo actions proposed."
    lines = [f"Plan (preview @ {now_iso()})"]
    for bot, actions in actions_by_bot.items():
        if not actions:
            continue
        lines.append(f"\nBot: {bot}")
        for a in actions:
            aid = getattr(a, "action_id", "NA")
            prio = getattr(a, "priority", "-")
            route = getattr(a, "route_human", "(route)")
            amt  = getattr(a, "amount_in_text", "(amount)")
            reason = getattr(a, "reason", "n/a")
            limits = getattr(a, "limits_text", "n/a")
            lines.append(
                f"- Action #{aid}  PRIORITY:{prio}\n"
                f"  Route : {route}\n"
                f"  Size  : {amt}\n"
                f"  Rationale:\n    • {reason}\n"
                f"  Limits:\n    • {limits}"
            )
    return "\n".join(lines)

def render_dryrun(results):
    if not results:
        return f"Dry-run (@ {now_iso()}): no executable actions."
    lines = [f"Dry-run (tick @ {now_iso()})\n"]
    for r in results:
        aid = getattr(r, "action_id", "NA")
        bot = getattr(r, "bot", "NA")
        path = getattr(r, "path_text", "(path)")
        ain  = getattr(r, "amount_in_text", "(amount)")
        qout = getattr(r, "quote_out_text", "(quote)")
        imp  = getattr(r, "impact_bps", None)
        slip = getattr(r, "slippage_bps", None)
        mino = getattr(r, "min_out_text", "(minOut)")
        gas  = getattr(r, "gas_estimate", "—")
        allow = getattr(r, "allowance_ok", False)
        nonce = getattr(r, "nonce", "—")
        txp  = getattr(r, "tx_preview_text", "(tx preview)")
        lines.append(
            f"Action #{aid} — {bot}\n"
            f"Path     : {path}\n"
            f"AmountIn : {ain}\n"
            f"QuoteOut : {qout}"
        )
        lines.append(f"Impact   : {imp:.2f} bps" if imp is not None else "Impact   : —")
        lines.append(f"Slippage : {slip} bps → minOut {mino}" if slip is not None else f"minOut   : {mino}")
        lines.append(
            f"Gas Est  : {gas}\n"
            f"Allowance: {'OK' if allow else 'NEEDED'}\n"
            f"Nonce    : {nonce}\n"
            f"Would send:\n{txp}\n"
        )
    return "\n".join(lines).strip()

# -----------------------------------------------------------------------------
# Commands: start/help/version/sanity/assets
# -----------------------------------------------------------------------------
def cmd_start(update: Update, context: CallbackContext):
    update.message.reply_text(
        "TECBot online.\n"
        "Try: /help\n"
        "Core: /trade /withdraw /balances /prices\n"
        "Debug: /dryrun /ping /sanity\n"
    )

def cmd_help(update: Update, context: CallbackContext):
    update.message.reply_text(
        "Commands:\n"
        "  /ping — health check\n"
        "  /trade — manual trade (wallet, route, size, slippage, execute)\n"
        "  /withdraw — withdraw funds to treasury wallet\n"
        "  /balances — per-wallet balances\n"
        "  /prices — on-chain quotes in USDC\n"
        "  /slippage <IN> [AMOUNT] [OUT] — impact curve\n"
        "  /cooldowns [bot|route] — show cooldowns\n"
        "  /version — running bot version\n"
        "  /sanity — config/modules sanity\n"
        "  /assets — configured tokens & wallets\n"
        "  /dryrun — strategy dry-run (internal)\n"
        "  /plan — strategy planner preview (internal)\n"
    )

def cmd_version(update: Update, context: CallbackContext):
    ver = os.getenv("TECBOT_VERSION", getattr(C, "TECBOT_VERSION", "v0.1.0-ops"))
    rev = _git_short_rev()
    update.message.reply_text(f"Version: {ver}" + (f" · git {rev}" if rev else ""))

def cmd_sanity(update: Update, context: CallbackContext):
    details = {
        "chain_id": getattr(C, "CHAIN_ID", "?"),
        "rpc": getattr(C, "HARMONY_RPC", "?"),
        "dryrun_enabled": getattr(C, "DRYRUN_ENABLED", True),
        "admin_ids": getattr(C, "ADMIN_USER_IDS", []),
        "tokens": len(getattr(C, "TOKENS", {})),
        "pools_v3": len(getattr(C, "POOLS_V3", {})),
        "slippage_default_bps": getattr(C, "SLIPPAGE_DEFAULT_BPS", 30),
    }
    avail = {
        "planner_loaded": bool(planner),
        "runner_loaded": bool(runner),
        "prices_loaded": bool(PR),
        "balances_loaded": bool(BL),
        "slippage_loaded": bool(SL),
        "wallet_loaded": bool(W),
        "trade_executor_loaded": bool(TE),
        "alert_loaded": bool(ALERTMOD),
    }
    txt = "Sanity:\n  " + "\n  ".join(f"{k}: {v}" for k, v in details.items())
    txt += "\n\nModules:\n  " + "\n  ".join(f"{k}: {v}" for k, v in avail.items())
    update.message.reply_text(txt)

def cmd_assets(update: Update, context: CallbackContext):
    tokens = {k.upper(): v for k, v in getattr(C, "TOKENS", {}).items()}
    wallets = getattr(C, "WALLETS", {})
    lines = []
    lines.append("TOKENS  ADDRESS                                   ")
    lines.append("-------------------------------------------------")
    for k in sorted(tokens.keys()):
        lines.append(f"{k:<6}  {tokens[k]}")
    lines.append("")
    lines.append("WALLETS      ADDRESS                                   ")
    lines.append("-------------------------------------------------------")
    for k in sorted(wallets.keys()):
        lines.append(f"{k:<12} {wallets[k]}")
    update.message.reply_text(f"<pre>\n{chr(10).join(lines)}\n</pre>", parse_mode=ParseMode.HTML)

# -----------------------------------------------------------------------------
# /balances
# -----------------------------------------------------------------------------
def cmd_balances(update: Update, context: CallbackContext):
    if BL is None:
        update.message.reply_text("Balances unavailable (module not loaded).")
        return
    try:
        table = BL.all_balances()
    except Exception as e:
        log.exception("balances failure")
        update.message.reply_text(f"Balances error: {e}")
        return

    cols = ["ONE", "WONE", "1USDC", "1ETH", "TEC", "1sDAI"]
    w_wallet = 22
    w_amt = 11
    header = f"{'Wallet':<{w_wallet}}  " + "  ".join(f"{c:>{w_amt}}" for c in cols)
    sep = "-" * len(header)
    lines = [f"Balances (@ {now_iso()})", header, sep]

    for w_name in sorted(table.keys()):
        row = table[w_name]
        vals: List[str] = []
        vals.append(_fmt_amt("ONE", _resolve_one_value(row)))
        vals.append(_fmt_amt("WONE", _row_get_token_amount(row, "WONE")))
        vals.append(_fmt_amt("1USDC", _row_get_token_amount(row, "1USDC")))
        vals.append(_fmt_amt("1ETH", _row_get_token_amount(row, "1ETH")))
        vals.append(_fmt_amt("TEC", _row_get_token_amount(row, "TEC")))
        vals.append(_fmt_amt("1sDAI", _row_get_token_amount(row, "1sDAI")))
        lines.append(f"{w_name:<{w_wallet}}  " + "  ".join(f"{v:>{w_amt}}" for v in vals))

    update.message.reply_text(f"<pre>\n{chr(10).join(lines)}\n</pre>", parse_mode=ParseMode.HTML)

# -----------------------------------------------------------------------------
# /prices (kept from your working enhanced version)
# -----------------------------------------------------------------------------
def _coinbase_eth() -> Optional[Decimal]:
    try:
        import coinbase_client
        val = coinbase_client.fetch_eth_usd_price()
        return Decimal(str(val)) if val is not None else None
    except Exception:
        return None

def _eth_best_side_and_route() -> Tuple[Optional[Decimal], str]:
    if PR is None:
        return None, "fwd"
    try:
        from app.chain import get_ctx
        ctx = get_ctx(C.HARMONY_RPC)
        ABI = [{
            "inputs":[{"internalType":"bytes","name":"path","type":"bytes"},
                      {"internalType":"uint256","name":"amountIn","type":"uint256"}],
            "name":"quoteExactInput",
            "outputs":[{"internalType":"uint256","name":"amountOut","type":"uint256"},
                       {"internalType":"uint160","name":"sqrtPriceX96After","type":"uint160"},
                       {"internalType":"uint32","name":"initializedTicksCrossed","type":"uint32"},
                       {"internalType":"uint256","name":"gasEstimate","type":"uint256"}],
            "stateMutability":"nonpayable","type":"function"}]
        q = ctx.w3.eth.contract(address=Web3.to_checksum_address(C.QUOTER_ADDR), abi=ABI)

        def addr(s): return Web3.to_checksum_address(PR._addr(s))
        def fee3(f): return int(f).to_bytes(3, "big")

        dec_e = PR._dec("1ETH")
        dec_u = PR._dec("1USDC")

        choices = []
        for usdc_in in (Decimal("25"), Decimal("50"), Decimal("100"), Decimal("250")):
            wei = int(usdc_in * (Decimal(10) ** dec_u))
            path = (Web3.to_bytes(hexstr=addr("1USDC")) + fee3(3000) +
                    Web3.to_bytes(hexstr=addr("WONE"))  + fee3(3000) +
                    Web3.to_bytes(hexstr=addr("1ETH")))
            out = q.functions.quoteExactInput(path, wei).call()[0]
            eth_out = Decimal(out) / (Decimal(10) ** dec_e)
            if eth_out > 0:
                choices.append(usdc_in / eth_out)

        rev = min(choices) if choices else None

        wei_in = int(Decimal("1") * (Decimal(10) ** dec_e))
        path_f = (Web3.to_bytes(hexstr=addr("1ETH")) + fee3(3000) +
                  Web3.to_bytes(hexstr=addr("WONE")) + fee3(3000) +
                  Web3.to_bytes(hexstr=addr("1USDC")))
        out_f = q.functions.quoteExactInput(path_f, wei_in).call()[0]
        fwd = (Decimal(out_f) / (Decimal(10) ** dec_u)) if out_f else None

        if rev is not None and fwd is not None:
            cb = _coinbase_eth()
            if cb:
                d_rev = abs(rev - cb)
                d_fwd = abs(fwd - cb)
                return (rev, "rev") if d_rev <= d_fwd else (fwd, "fwd")
            if abs(rev - fwd) / max(Decimal("1"), rev) > Decimal("0.05"):
                return rev, "rev"
            return fwd, "fwd"
        return (rev or fwd), ("rev" if rev else "fwd")
    except Exception:
        return None, "fwd"

def cmd_prices(update: Update, context: CallbackContext):
    if PR is None:
        update.message.reply_text("Prices unavailable (module not loaded).")
        return

    syms = ["ONE", "WONE", "1USDC", "1sDAI", "TEC", "1ETH"]

    w_asset, w_lp, w_basis, w_slip, w_route = 6, 11, 12, 9, 27
    header = (
        f"{'Asset':<{w_asset}} | {'LP Price':>{w_lp}} | {'Quote Basis':>{w_basis}} | "
        f"{'Slippage':>{w_slip}} | {'Route':<{w_route}}"
    )
    sep = "-" * len(header)
    lines = ["LP Prices", header, sep]

    cb_eth = _coinbase_eth()
    lp_eth_pref, side = _eth_best_side_and_route()

    for s in syms:
        route_text = "—"
        basis = Decimal("1")
        slip_txt = "0 bps"
        price: Optional[Decimal] = None

        try:
            if s == "ONE":
                price = PR.price_usd("WONE", Decimal("1"))
                route_text = "Native ONE ≈ WONE → 1USDC" if price is not None else "—"
            elif s == "WONE":
                price = PR.price_usd("WONE", Decimal("1"))
                route_text = "WONE → 1USDC (fwd)" if price is not None else "—"
            elif s == "1USDC":
                price = Decimal("1")
                route_text = "—"
            elif s == "1sDAI":
                price = PR.price_usd("1sDAI", Decimal("1"))
                route_text = "1sDAI → 1USDC (fwd)" if price is not None else "—"
            elif s == "TEC":
                basis = Decimal("100")
                out = PR.price_usd("TEC", basis)
                price = (out / basis) if out is not None and basis > 0 else None
                route_text = "TEC → WONE → 1USDC"
                slip_txt = "—" if price is None else "  "
            elif s == "1ETH":
                price = lp_eth_pref or PR.price_usd("1ETH", Decimal("1"))
                route_text = ("1USDC → WONE → 1ETH (rev)" if side == "rev"
                              else "1ETH → WONE → 1USDC (fwd)")
            else:
                price = PR.price_usd(s, Decimal("1"))
                route_text = "—" if price is None else "(direct/best)"
        except Exception:
            price = None

        lp_str = _fmt_money(price).rjust(w_lp)
        basis_str = f"{basis:.5f}".rjust(w_basis)
        slip_str = slip_txt.rjust(w_slip)
        lines.append(f"{s:<{w_asset}} | {lp_str} | {basis_str} | {slip_str} | {route_text:<{w_route}}")

    eth_lp_display = None
    try:
        for ln in lines:
            if ln.startswith("1ETH "):
                cell = ln.split("|")[1].strip().replace("$", "").replace(",", "")
                eth_lp_display = Decimal(cell) if cell and cell != "—" else None
                break
    except Exception:
        eth_lp_display = None

    lines += ["", "ETH: Harmony LP vs Coinbase"]
    lines.append(f"  LP:       {_fmt_money(eth_lp_display)}")
    lines.append(f"  Coinbase: {_fmt_money(cb_eth)}")
    if eth_lp_display is not None and cb_eth not in (None, Decimal(0)):
        try:
            diff = (Decimal(eth_lp_display) - Decimal(cb_eth)) / Decimal(cb_eth) * Decimal(100)
            lines.append(f"  Diff:     {diff:+.2f}%")
        except Exception:
            pass

    update.message.reply_text(f"<pre>\n{chr(10).join(lines)}\n</pre>", parse_mode=ParseMode.HTML)

# -----------------------------------------------------------------------------
# /slippage (kept from your working enhanced version)
# -----------------------------------------------------------------------------
def _mid_usdc_per_unit(token_in: str) -> Optional[Decimal]:
    if PR is None:
        return None
    t = token_in.upper()
    try:
        if t == "1ETH":
            from app.chain import get_ctx
            ctx = get_ctx(C.HARMONY_RPC)
            ABI = [{
                "inputs":[{"internalType":"bytes","name":"path","type":"bytes"},
                          {"internalType":"uint256","name":"amountIn","type":"uint256"}],
                "name":"quoteExactInput",
                "outputs":[{"internalType":"uint256","name":"amountOut","type":"uint256"},
                           {"internalType":"uint160","name":"sqrtPriceX96After","type":"uint160"},
                           {"internalType":"uint32","name":"initializedTicksCrossed","type":"uint32"},
                           {"internalType":"uint256","name":"gasEstimate","type":"uint256"}],
                "stateMutability":"nonpayable","type":"function"}]
            q = ctx.w3.eth.contract(address=Web3.to_checksum_address(C.QUOTER_ADDR), abi=ABI)
            def addr(s): return Web3.to_checksum_address(PR._addr(s))
            def fee3(f): return int(f).to_bytes(3, "big")
            dec_e = PR._dec("1ETH")
            dec_u = PR._dec("1USDC")
            choices = []
            for usdc_in in (Decimal("25"), Decimal("50"), Decimal("100"), Decimal("250")):
                wei = int(usdc_in * (Decimal(10) ** dec_u))
                path = (Web3.to_bytes(hexstr=addr("1USDC")) + fee3(3000) +
                        Web3.to_bytes(hexstr=addr("WONE"))  + fee3(3000) +
                        Web3.to_bytes(hexstr=addr("1ETH")))
                out = q.functions.quoteExactInput(path, wei).call()[0]
                eth_out = Decimal(out) / (Decimal(10) ** dec_e)
                if eth_out > 0:
                    choices.append(usdc_in / eth_out)
            return min(choices) if choices else None
        if t == "ONE":
            return PR.price_usd("WONE", Decimal("1"))
        return PR.price_usd(t, Decimal("1"))
    except Exception:
        return None

def cmd_slippage(update: Update, context: CallbackContext):
    args = context.args or []
    if not args:
        update.message.reply_text(
            "Usage: /slippage <TOKEN_IN> [AMOUNT] [TOKEN_OUT]\n"
            "Examples:\n"
            "  /slippage 1ETH\n"
            "  /slippage 1ETH 0.5 1USDC"
        )
        return

    token_in = args[0].upper()
    token_out = args[2].upper() if len(args) >= 3 else "1USDC"
    mid = _mid_usdc_per_unit(token_in)

    targets = [Decimal("10"), Decimal("100"), Decimal("1000"), Decimal("10000")]
    rows = []
    for usdc in targets:
        if mid and mid > 0:
            est_in = (usdc / mid).quantize(Decimal("0.000001"))
        else:
            est_in = Decimal("0")
        try:
            if PR is None:
                raise RuntimeError("prices module unavailable")
            px_usd = PR.price_usd("WONE" if token_in == "ONE" else token_in, est_in)
            eff = (px_usd / est_in) if (px_usd and est_in > 0) else None
            slip = ((eff - mid) / mid * Decimal("100")) if (eff and mid) else None
            rows.append((f"{usdc:,.0f}", f"{est_in:.6f}", f"{eff:,.2f}" if eff else "—", f"{slip:+.2f}%" if slip is not None else "—"))
        except Exception:
            rows.append((f"{usdc:,.0f}", "—", "—", "—"))

    w1, w2, w3, w4 = 12, 16, 12, 16
    col1, col2, col3, col4 = "Size (USDC)", "Amount In (sym)", "Eff. Price", "Slippage vs mid"
    line_hdr = f"{col1:>{w1}} | {col2:>{w2}} | {col3:>{w3}} | {col4:>{w4}}"
    line_sep = "-" * len(line_hdr)
    tbl = [line_hdr, line_sep]
    for a, b, c, d in rows:
        tbl.append(f"{a:>{w1}} | {b:>{w2}} | {('$' + c) if c != '—' else '—':>{w3}} | {d:>{w4}}")

    out = [f"Slippage curve: {token_in} → {token_out}"]
    if mid:
        out.append(f"Baseline (mid): ${mid:,.2f} per 1{token_in}")
    out.append("")
    out.extend(tbl)
    update.message.reply_text(f"<pre>\n{chr(10).join(out)}\n</pre>", parse_mode=ParseMode.HTML)

# -----------------------------------------------------------------------------
# Health / planner / cooldowns / dryrun
# -----------------------------------------------------------------------------
def cmd_ping(update: Update, context: CallbackContext):
    ip_txt = "unknown"
    try:
        with open("/bot/db/public_ip.txt", "r") as f:
            ip_txt = f.read().strip() or "unknown"
    except Exception:
        pass
    ver = os.getenv("TECBOT_VERSION", getattr(C, "TECBOT_VERSION", "v0.1.0-ops"))
    update.message.reply_text(f"pong · IP: {ip_txt} · {ver}")

def cmd_plan(update: Update, context: CallbackContext):
    if planner is None or not hasattr(planner, "build_plan_snapshot"):
        update.message.reply_text("Plan error: planner module not available (strategies/planner.py).")
        return
    try:
        snap = planner.build_plan_snapshot()
    except Exception as e:
        log.exception("plan failure")
        update.message.reply_text(f"Plan error: {e}")
        return
    update.message.reply_text(render_plan(snap))

def cmd_cooldowns(update: Update, context: CallbackContext):
    defaults = getattr(C, "COOLDOWNS_DEFAULTS", {"price_refresh": 15, "trade_retry": 30, "alert_throttle": 60})
    by_bot = getattr(C, "COOLDOWNS_BY_BOT", {})
    by_route = getattr(C, "COOLDOWNS_BY_ROUTE", {})
    args = context.args or []
    if not args:
        update.message.reply_text("Default cooldowns (seconds):\n  " + "\n  ".join(f"{k}: {v}" for k, v in defaults.items()))
        return
    key = args[0]
    if key in by_bot:
        d = by_bot[key]; header = f"Cooldowns for {key} (seconds):"
    elif key in by_route:
        d = by_route[key]; header = f"Cooldowns for route {key} (seconds):"
    else:
        update.message.reply_text(f"No specific cooldowns for '{key}'. Showing defaults.\n  " + "\n  ".join(f"{k}: {v}" for k, v in defaults.items()))
        return
    update.message.reply_text(header + "\n  " + "\n  ".join(f"{k}: {v}" for k, v in d.items()))

def cmd_dryrun(update: Update, context: CallbackContext):
    if not getattr(C, "DRYRUN_ENABLED", True):
        update.message.reply_text("Dry-run is disabled (set DRYRUN_ENABLED=True in config).")
        return
    if runner is None or not all(hasattr(runner, n) for n in ("build_dryrun", "execute_action")):
        update.message.reply_text("Dry-run unavailable: runner.build_dryrun()/execute_action() not found.")
        return
    try:
        results = runner.build_dryrun()
    except Exception as e:
        log.exception("dryrun failure")
        update.message.reply_text(f"Dry-run error: {e}")
        return
    if not results:
        update.message.reply_text("Dry-run: no executable actions.")
        return

    text = render_dryrun(results)
    kb = [[InlineKeyboardButton(f"▶️ Execute {getattr(r, 'action_id', '?')}", callback_data=f"exec:{getattr(r, 'action_id', '?')}")] for r in results]
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="exec_cancel")])
    update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)

def on_exec_button(update: Update, context: CallbackContext):
    q = update.callback_query
    data = q.data or ""
    try:
        q.answer()
    except Exception:
        pass
    if not data.startswith("exec:"):
        return
    if not is_admin(q.from_user.id):
        q.answer("Not authorized.", show_alert=True)
        return
    aid = data.split(":", 1)[1]
    q.edit_message_text(f"Confirm execution: Action #{aid}\nAre you sure?")
    q.edit_message_reply_markup(
        InlineKeyboardMarkup([[InlineKeyboardButton("✅ Confirm", callback_data=f"exec_go:{aid}"),
                               InlineKeyboardButton("❌ Abort", callback_data="exec_cancel")]])
    )

def on_exec_confirm(update: Update, context: CallbackContext):
    q = update.callback_query
    data = q.data or ""
    try:
        q.answer()
    except Exception:
        pass
    if not data.startswith("exec_go:"):
        return
    if not is_admin(q.from_user.id):
        q.answer("Not authorized.", show_alert=True)
        return
    if runner is None or not hasattr(runner, "execute_action"):
        q.answer("Execution backend missing.", show_alert=True)
        return
    aid = data.split(":", 1)[1]
    try:
        txr = runner.execute_action(aid)
        txh = _norm_tx_hash(str(getattr(txr, "tx_hash", "")))
        gas_used = getattr(txr, "gas_used", "—")
        explorer = getattr(txr, "explorer_url", "") or (f"https://explorer.harmony.one/tx/{txh}?shard=0" if txh else "")
        filled = getattr(txr, "filled_text", "")
        q.edit_message_text(f"✅ Executed {aid}\n{filled}\nGas used: {gas_used}\nTx: {txh}\n{explorer}".strip())
    except Exception as e:
        q.edit_message_text(f"❌ Execution failed for {aid}\n{e}")

def on_exec_cancel(update: Update, context: CallbackContext):
    q = update.callback_query
    try:
        q.answer()
    except Exception:
        pass
    q.edit_message_text("Canceled. No transaction sent.")

# -----------------------------------------------------------------------------
# /trade wizard
# -----------------------------------------------------------------------------
_TRADE_STATE: Dict[int, Dict[str, str]] = {}


# Prevent accidental double-execution / double-messaging when Telegram retries callbacks.
# Keyed by (user_id, callback_data); values are last-seen epoch seconds.
_CALLBACK_DEDUP: Dict[Tuple[int, str], float] = {}

def _seen_callback_recently(uid: int, data: str, window_s: float = 5.0) -> bool:
    try:
        import time as _time
        now = float(_time.time())
        key = (int(uid), str(data))
        last = _CALLBACK_DEDUP.get(key, 0.0)
        if now - last < window_s:
            return True
        _CALLBACK_DEDUP[key] = now
        # light cleanup
        if len(_CALLBACK_DEDUP) > 2000:
            for k, v in list(_CALLBACK_DEDUP.items())[:500]:
                if now - v > 300:
                    _CALLBACK_DEDUP.pop(k, None)
        return False
    except Exception:
        return False


def cmd_trade(update: Update, context: CallbackContext):
    uid = update.effective_user.id
    _TRADE_STATE[uid] = {
        "step": "wallet",
        "wallet": "",
        "from": "",
        "to": "",
        "amount": "",
        "slip_bps": str(getattr(C, "SLIPPAGE_DEFAULT_BPS", 30)),
        "waiting_amount": "0",
    }

    wallets = getattr(C, "WALLETS", {})
    kb = [[InlineKeyboardButton(name, callback_data=f"tw_wallet:{name}")]
          for name in sorted(wallets.keys())]
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="tw_cancel")])
    update.message.reply_text("Select wallet:", reply_markup=InlineKeyboardMarkup(kb))

def _tw_reply_edit(q, text, kb=None, html=False):
    """Edit the wizard message in-place when possible; fall back to a new message."""
    rm = InlineKeyboardMarkup(kb) if kb else None
    try:
        q.edit_message_text(
            text,
            reply_markup=rm,
            parse_mode=(ParseMode.HTML if html else None),
            disable_web_page_preview=True,
        )
    except Exception:
        try:
            q.message.reply_text(
                text,
                reply_markup=rm,
                parse_mode=(ParseMode.HTML if html else None),
                disable_web_page_preview=True,
            )
        except Exception:
            pass

def _tw_require_state(uid):
    st = _TRADE_STATE.get(uid)
    if not st:
        st = {
            "step": "wallet",
            "wallet": "",
            "from": "",
            "to": "",
            "amount": "",
            "slip_bps": str(getattr(C, "SLIPPAGE_DEFAULT_BPS", 30)),
            "waiting_amount": "0",
        }
        _TRADE_STATE[uid] = st
    return st

def _tw_assets_keyboard(uid, which):
    syms = ["ONE", "WONE", "1USDC", "1sDAI", "TEC", "1ETH"]
    kb = [[InlineKeyboardButton(s, callback_data=f"tw_{which}:{s}")] for s in syms]
    kb.append([InlineKeyboardButton("⬅ Back", callback_data="tw_back_wallet" if which == "from" else "tw_back_from")])
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="tw_cancel")])
    return kb

def _tw_slip_keyboard(uid):
    choices = [
        ("0.10% max", 10),
        ("0.50% max", 50),
        ("1.00% max", 100),
        ("Use default", getattr(C, "SLIPPAGE_DEFAULT_BPS", 30)),
    ]
    kb = [[InlineKeyboardButton(label, callback_data=f"tw_slip:{bps}")] for label, bps in choices]
    kb.append([InlineKeyboardButton("⬅ Back", callback_data="tw_back_amount")])
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="tw_cancel")])
    return kb

def _tw_amount_keyboard(uid, wallet, token_in):
    bal_display = "?"
    if BL:
        try:
            table = BL.all_balances()
            row = table.get(wallet, {})
            bal = _row_get_token_amount(row, token_in)
            bal_display = f"{bal}"
        except Exception:
            pass

    kb = [
        [InlineKeyboardButton(f"All ({bal_display} {token_in})", callback_data="tw_amt_all")],
        [InlineKeyboardButton("⬅ Back", callback_data="tw_back_to")],
        [InlineKeyboardButton("❌ Cancel", callback_data="tw_cancel")],
    ]
    return kb, bal_display

def _tw_render_manual_quote(uid, st):
    if runner is None or not hasattr(runner, "build_manual_quote"):
        return ("Runner manual quote not available.", None, None)

    wallet_key   = st["wallet"]
    token_in_ui  = st["from"]
    token_out_ui = st["to"]
    amt_str      = st["amount"]
    slip_bps     = int(st["slip_bps"])

    if not (wallet_key and token_in_ui and token_out_ui and amt_str):
        return ("Trade is incomplete.", None, None)

    try:
        amt_dec = Decimal(amt_str)
    except Exception:
        return ("Bad amount.", None, None)

    mq = runner.build_manual_quote(
        wallet_key=wallet_key,
        token_in=token_in_ui,
        token_out=token_out_ui,
        amount_in=amt_dec,
        slippage_bps=slip_bps,
    )

    path     = getattr(mq, "path_text", f"{token_in_ui} → {token_out_ui}")
    ain_txt  = getattr(mq, "amount_in_text", f"{amt_str} {token_in_ui}")
    qout_txt = getattr(mq, "quote_out_text", f"? {token_out_ui}")
    imp_bps  = getattr(mq, "impact_bps", None)
    slip_bps_val = getattr(mq, "slippage_bps", slip_bps)
    min_out  = getattr(mq, "min_out_text", f"? {token_out_ui}")

    gas_est  = getattr(mq, "gas_estimate", "—")
    nonce    = getattr(mq, "nonce", "—")
    allow_ok = getattr(mq, "allowance_ok", False)
    tx_prev  = getattr(mq, "tx_preview_text", "(tx preview)")
    slip_ok  = getattr(mq, "slippage_ok", True)
    need_appr_txt = getattr(mq, "approval_required_amount_text", None)

    lines = [
        f"Review Trade — {wallet_key}",
        f"Path     : {path}",
        f"AmountIn : {ain_txt}",
        f"QuoteOut : {qout_txt}",
        (f"Impact   : {imp_bps:.2f} bps" if imp_bps is not None else "Impact   : —"),
        f"Slippage : {Decimal(slip_bps_val) / Decimal(100):.2f}% max → minOut {min_out}",
        _fmt_gas_est_line(gas_est),
        f"Nonce    : {nonce}",
        f"Allowance: {'OK' if allow_ok else 'NOT APPROVED'}",
    ]
    if not allow_ok and need_appr_txt:
        lines.append(f"Required : approve {need_appr_txt}")
    if not slip_ok:
        lines.append("⚠ Price already worse than allowed slippage")

    preview_human = f"Swap: {ain_txt} → ≥ {min_out} via {path} (deadline 10m)"
    lines += ["", "Would send:", preview_human]
    txt = "\n".join(lines)

    kb_rows = []
    if not allow_ok and need_appr_txt:
        kb_rows.append([InlineKeyboardButton("✅ Approve spend first", callback_data="tw_do_approve")])
    elif allow_ok and slip_ok:
        kb_rows.append([InlineKeyboardButton("✅ Execute Trade", callback_data="tw_do_execute")])
    else:
        kb_rows.append([InlineKeyboardButton("⚠ Adjust Slippage", callback_data="tw_back_slip")])

    kb_rows.append([InlineKeyboardButton("❌ Cancel", callback_data="tw_cancel")])
    return (txt, kb_rows, mq)

def _tw_set_amount(uid, st, amount_text):
    st["amount"] = amount_text
    st["step"] = "slip"
    return True

def _tw_handle_wallet(q, uid, wallet):
    st = _tw_require_state(uid)
    st["wallet"] = wallet
    st["step"] = "from"
    kb = _tw_assets_keyboard(uid, "from")
    _tw_reply_edit(q, f"Wallet: {wallet}\n\nSelect FROM asset:", kb)

def _tw_handle_from(q, uid, sym):
    st = _tw_require_state(uid)
    st["from"] = sym
    st["step"] = "to"
    kb = _tw_assets_keyboard(uid, "to")
    _tw_reply_edit(q, f"FROM: {sym}\n\nSelect TO asset:", kb)

def _tw_handle_to(q, uid, sym):
    st = _tw_require_state(uid)
    st["to"] = sym
    st["step"] = "amount"
    st["waiting_amount"] = "1"
    kb, bal_disp = _tw_amount_keyboard(uid, st["wallet"], st["from"])
    _tw_reply_edit(
        q,
        f"FROM {st['from']} TO {sym}\n\nEnter amount of {st['from']} to trade (type a number in chat).\nBalance: {bal_disp} {st['from']}",
        kb
    )

def _tw_handle_amt_all(q, uid):
    st = _tw_require_state(uid)
    amt = Decimal("0")
    if BL:
        try:
            table = BL.all_balances()
            row = table.get(st["wallet"], {})
            amt = _row_get_token_amount(row, st["from"])
        except Exception:
            pass
    _tw_set_amount(uid, st, str(amt))
    kb = _tw_slip_keyboard(uid)
    _tw_reply_edit(q, f"Amount set to ALL ({amt} {st['from']}).\nSelect slippage limit:", kb)

def _tw_handle_slip(q, uid, bps_str):
    st = _tw_require_state(uid)
    st["slip_bps"] = bps_str
    st["step"] = "review"
    txt, kb_rows, _ = _tw_render_manual_quote(uid, st)
    _tw_reply_edit(q, txt, kb_rows)

def _tw_handle_back(q, uid, dest):
    st = _tw_require_state(uid)
    if dest == "wallet":
        st["step"] = "wallet"
        wallets = getattr(C, "WALLETS", {})
        kb = [[InlineKeyboardButton(name, callback_data=f"tw_wallet:{name}")]
              for name in sorted(wallets.keys())]
        kb.append([InlineKeyboardButton("❌ Cancel", callback_data="tw_cancel")])
        _tw_reply_edit(q, "Select wallet:", kb)
    elif dest == "from":
        st["step"] = "from"
        kb = _tw_assets_keyboard(uid, "from")
        _tw_reply_edit(q, f"Wallet: {st['wallet']}\n\nSelect FROM asset:", kb)
    elif dest == "to":
        st["step"] = "to"
        kb = _tw_assets_keyboard(uid, "to")
        _tw_reply_edit(q, f"FROM: {st['from']}\n\nSelect TO asset:", kb)
    elif dest == "amount":
        st["step"] = "amount"
        st["waiting_amount"] = "1"
        kb, bal_disp = _tw_amount_keyboard(uid, st["wallet"], st["from"])
        _tw_reply_edit(
            q,
            f"FROM {st['from']} TO {st['to']}\n\nEnter amount of {st['from']} to trade (type a number in chat).\nBalance: {bal_disp} {st['from']}",
            kb
        )
    elif dest == "slip":
        st["step"] = "slip"
        kb = _tw_slip_keyboard(uid)
        _tw_reply_edit(q, f"Amount: {st.get('amount','?')} {st.get('from','?')}\nSelect slippage limit:", kb)

def _tw_handle_approve(q, uid):
    # Answer immediately to avoid "query is too old"
    try:
        q.answer("Sending approval...")
    except Exception:
        pass

    if not is_admin(q.from_user.id):
        q.message.reply_text("Not authorized.")
        return
    st = _tw_require_state(uid)
    if TE is None or runner is None:
        _tw_reply_edit(q, "Approve backend missing.")
        return
    try:
        amt = Decimal(st["amount"])
        sym = st["from"]
        wallet_key = st["wallet"]

        token_addr = runner._addr(sym)  # resolve symbol -> address
        dec = TE.get_decimals(token_addr)
        wei = int((amt * (Decimal(10) ** dec)).to_integral_value(rounding=ROUND_DOWN))

        with _suppress_alerts_temporarily():
            TE.approve_if_needed(wallet_key, token_addr, TE.ROUTER_ADDR_ETH, wei)

        txt, kb_rows, _ = _tw_render_manual_quote(uid, st)
        _tw_reply_edit(q, txt, kb_rows)
    except Exception as e:
        _tw_reply_edit(q, f"Approval failed:\n<code>{e}</code>", html=True)

def _tw_handle_execute(q, uid):
    # Answer immediately to avoid "query is too old"
    try:
        q.answer("Executing trade...")
    except Exception:
        pass

    if not is_admin(q.from_user.id):
        q.message.reply_text("Not authorized.")
        return
    st = _tw_require_state(uid)
    if runner is None or not hasattr(runner, "execute_manual_quote"):
        _tw_reply_edit(q, "Execution backend missing.")
        return
    try:
        amt_dec = Decimal(st["amount"])
    except Exception:
        _tw_reply_edit(q, "Bad amount.")
        return

    try:
        with _suppress_alerts_temporarily():
            txr = runner.execute_manual_quote(
                wallet_key=st["wallet"],
                token_in=st["from"],
                token_out=st["to"],
                amount_in=amt_dec,
                slippage_bps=int(st["slip_bps"]),
            )

        txh = _norm_tx_hash(str(txr.get("tx_hash", "")))
        filled = txr.get("filled_text", "")
        gas_used = txr.get("gas_used", None)

        gas_one = None
        if "gas_cost_one" in txr:
            try:
                gas_one = Decimal(str(txr.get("gas_cost_one")))
            except Exception:
                gas_one = None

        # Optional secondary transaction hash (approval / wrap / unwrap / other contract call)
        secondary_keys = (
            "contract_tx_hash", "call_tx_hash", "approval_tx_hash", "approve_tx_hash",
            "unwrap_tx_hash", "wrap_tx_hash", "secondary_tx_hash", "tx_hash_2"
        )
        txh2 = ""
        for k in secondary_keys:
            if k in txr and txr.get(k):
                txh2 = _norm_tx_hash(str(txr.get(k)))
                break

        def _explorer_url(tx_hash: str) -> str:
            if not tx_hash:
                return ""
            return f"https://explorer.harmony.one/tx/{tx_hash}?shard=0"

        explorer = _explorer_url(txh) if txh else ""

        lines = [
            "✅ Executed manual trade",
            f"Wallet: {st['wallet']}",
        ]
        if filled:
            lines.append(str(filled))
        lines.append(_fmt_gas_summary(gas_used, gas_one))

        if txh:
            lines.append(f"Trade tx: {txh}")
            if explorer:
                lines.append(explorer)

        if txh2 and txh2 != txh:
            lines.append(f"Contract tx: {txh2}")
            lines.append(_explorer_url(txh2))

        _tw_reply_edit(q, "\n".join(lines).strip())
    except Exception as e:
        _tw_reply_edit(q, f"❌ Execution failed\n{e}")

def on_trade_callback(update: Update, context: CallbackContext):
    q = update.callback_query
    uid = q.from_user.id
    data = q.data or ""

    # Dedupe high-impact callbacks (Telegram may retry on slow networks)
    if data in ("tw_do_execute", "tw_do_approve"):
        if _seen_callback_recently(uid, data, window_s=8.0):
            try:
                q.answer("Already processing…")
            except Exception:
                pass
            return


    # Always answer quickly
    if data not in ("tw_do_approve", "tw_do_execute"):
        try:
            q.answer()
        except Exception:
            pass

    if data == "tw_cancel":
        _TRADE_STATE.pop(uid, None)
        q.edit_message_text("Canceled. No transaction sent.")
        return

    if data.startswith("tw_wallet:"):
        _tw_handle_wallet(q, uid, data.split(":", 1)[1]); return
    if data.startswith("tw_from:"):
        _tw_handle_from(q, uid, data.split(":", 1)[1]); return
    if data.startswith("tw_to:"):
        _tw_handle_to(q, uid, data.split(":", 1)[1]); return

    if data == "tw_amt_all":
        _tw_handle_amt_all(q, uid); return

    if data == "tw_back_wallet":
        _tw_handle_back(q, uid, "wallet"); return
    if data == "tw_back_from":
        _tw_handle_back(q, uid, "from"); return
    if data == "tw_back_to":
        _tw_handle_back(q, uid, "to"); return
    if data == "tw_back_amount":
        _tw_handle_back(q, uid, "amount"); return
    if data == "tw_back_slip":
        _tw_handle_back(q, uid, "slip"); return

    if data.startswith("tw_slip:"):
        _tw_handle_slip(q, uid, data.split(":", 1)[1]); return

    if data == "tw_do_approve":
        _tw_handle_approve(q, uid); return
    if data == "tw_do_execute":
        _tw_handle_execute(q, uid); return

# -----------------------------------------------------------------------------
# Typed amount capture (/trade and /withdraw)
# -----------------------------------------------------------------------------
_WITHDRAW_STATE: Dict[int, Dict[str, str]] = {}

def on_text_amount_capture(update: Update, context: CallbackContext):
    uid = update.effective_user.id if update.effective_user else None
    if uid is None or not update.message or not update.message.text:
        return

    txt = update.message.text.strip()

    # /trade amount capture
    st_trade = _TRADE_STATE.get(uid)
    if st_trade and st_trade.get("step") == "amount" and st_trade.get("waiting_amount") == "1":
        try:
            amt = Decimal(txt)
            if amt <= 0:
                raise ValueError("non-positive")
            st_trade["waiting_amount"] = "0"
            _tw_set_amount(uid, st_trade, str(amt))
            kb = _tw_slip_keyboard(uid)
            update.message.reply_text(
                f"Amount set to {amt} {st_trade['from']}. Select slippage:",
                reply_markup=InlineKeyboardMarkup(kb)
            )
            return
        except Exception:
            update.message.reply_text("Please send a positive numeric amount (e.g., 10 or 0.5).")
            return

    # /withdraw amount capture
    st_wd = _WITHDRAW_STATE.get(uid)
    if st_wd and st_wd.get("step") == "amount":
        try:
            amt = Decimal(txt)
            if amt <= 0:
                raise ValueError("non-positive")
            _wd_set_amount(uid, st_wd, str(amt))
            _wd_render_review(update, uid, via_message=True)
        except Exception:
            update.message.reply_text("Please send a positive numeric amount (e.g., 10 or 0.5).")

# -----------------------------------------------------------------------------
# /withdraw wizard
# -----------------------------------------------------------------------------
TREASURY_ADDR = "0x360c48a44f513b5781854588d2f1A40E90093c60"

def cmd_withdraw(update: Update, context: CallbackContext):
    uid = update.effective_user.id
    _WITHDRAW_STATE[uid] = {"step": "wallet", "wallet": "", "asset": "", "amount": ""}

    wallets = getattr(C, "WALLETS", {})
    kb = [[InlineKeyboardButton(name, callback_data=f"wd_wallet:{name}")]
          for name in sorted(wallets.keys())]
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="wd_cancel")])

    update.message.reply_text(
        f"Withdraw to treasury:\n{TREASURY_ADDR}\n\nSelect wallet:",
        reply_markup=InlineKeyboardMarkup(kb)
    )

def _wd_reply_edit(q, text, kb=None):
    try:
        q.edit_message_text(text)
        if kb:
            q.edit_message_reply_markup(InlineKeyboardMarkup(kb))
        else:
            q.edit_message_reply_markup(None)
    except Exception:
        q.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb) if kb else None)

def _wd_require_state(uid):
    st = _WITHDRAW_STATE.get(uid)
    if not st:
        st = {"step": "wallet", "wallet": "", "asset": "", "amount": ""}
        _WITHDRAW_STATE[uid] = st
    return st

def _wd_assets_keyboard():
    syms = ["ONE", "WONE", "1USDC", "1sDAI", "TEC", "1ETH"]
    kb = [[InlineKeyboardButton(s, callback_data=f"wd_asset:{s}")] for s in syms]
    kb.append([InlineKeyboardButton("⬅ Back", callback_data="wd_back_wallet")])
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="wd_cancel")])
    return kb

def _wd_amount_keyboard(uid, wallet, token):
    bal_display = "?"
    if BL:
        try:
            table = BL.all_balances()
            row = table.get(wallet, {})
            bal = _row_get_token_amount(row, token)
            bal_display = f"{bal}"
        except Exception:
            pass
    kb = [
        [InlineKeyboardButton(f"All ({bal_display} {token})", callback_data="wd_amt_all")],
        [InlineKeyboardButton("⬅ Back", callback_data="wd_back_asset")],
        [InlineKeyboardButton("❌ Cancel", callback_data="wd_cancel")],
    ]
    return kb, bal_display

def _wd_handle_wallet(q, uid, wallet):
    st = _wd_require_state(uid)
    st["wallet"] = wallet
    st["step"] = "asset"
    kb = _wd_assets_keyboard()
    _wd_reply_edit(q, f"Treasury: {TREASURY_ADDR}\nWallet: {wallet}\n\nSelect asset:", kb)

def _wd_handle_asset(q, uid, asset):
    st = _wd_require_state(uid)
    st["asset"] = asset
    st["step"] = "amount"
    kb, bal_disp = _wd_amount_keyboard(uid, st["wallet"], asset)
    _wd_reply_edit(
        q,
        f"Treasury: {TREASURY_ADDR}\nWallet: {st['wallet']}\nAsset: {asset}\n\nEnter withdrawal amount.\nBalance: {bal_disp} {asset}",
        kb
    )

def _wd_set_amount(uid, st, amount_text):
    st["amount"] = amount_text
    st["step"] = "review"
    return True

def _wd_handle_amt_all(q, uid):
    st = _wd_require_state(uid)
    amt = Decimal("0")
    if BL:
        try:
            table = BL.all_balances()
            row = table.get(st["wallet"], {})
            amt = _row_get_token_amount(row, st["asset"])
        except Exception:
            pass
    _wd_set_amount(uid, st, str(amt))
    _wd_render_review(q, uid)

def _wd_render_review(q_or_update, uid, via_message: bool = False):
    st = _wd_require_state(uid)
    txt = (
        "Withdraw Review\n"
        f"From Wallet : {st['wallet']}\n"
        f"Asset       : {st['asset']}\n"
        f"Amount      : {st['amount']} {st['asset']}\n"
        f"To          : {TREASURY_ADDR}\n\n"
        "Gas and nonce will be estimated before send."
    )
    kb = [
        [InlineKeyboardButton("✅ Send Withdrawal", callback_data="wd_do_send")],
        [InlineKeyboardButton("⬅ Back", callback_data="wd_back_amount")],
        [InlineKeyboardButton("❌ Cancel", callback_data="wd_cancel")],
    ]
    if via_message:
        q_or_update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(kb))
    else:
        _wd_reply_edit(q_or_update, txt, kb)

def _wd_handle_back(q, uid, dest):
    st = _wd_require_state(uid)
    if dest == "wallet":
        st["step"] = "wallet"
        wallets = getattr(C, "WALLETS", {})
        kb = [[InlineKeyboardButton(name, callback_data=f"wd_wallet:{name}")]
              for name in sorted(wallets.keys())]
        kb.append([InlineKeyboardButton("❌ Cancel", callback_data="wd_cancel")])
        _wd_reply_edit(q, f"Treasury: {TREASURY_ADDR}\nSelect wallet:", kb)
    elif dest == "asset":
        st["step"] = "asset"
        kb = _wd_assets_keyboard()
        _wd_reply_edit(q, f"Treasury: {TREASURY_ADDR}\nWallet: {st['wallet']}\n\nSelect asset:", kb)
    elif dest == "amount":
        st["step"] = "amount"
        kb, bal_disp = _wd_amount_keyboard(uid, st["wallet"], st["asset"])
        _wd_reply_edit(
            q,
            f"Treasury: {TREASURY_ADDR}\nWallet: {st['wallet']}\nAsset: {st['asset']}\n\nEnter withdrawal amount.\nBalance: {bal_disp} {st['asset']}",
            kb
        )

def _perform_withdraw(wallet_key: str, asset: str, amount_dec: Decimal) -> Dict[str, Any]:
    """
    Withdraw from bot wallet to TREASURY_ADDR.
    Handles:
      - ONE: native transfer
      - WONE: unwrap then native transfer
      - ERC-20: token transfer
    """
    if TE is None:
        raise RuntimeError("trade_executor module unavailable")

    acct = TE._get_account(wallet_key)
    from_addr = Web3.to_checksum_address(acct.address)
    to_addr = Web3.to_checksum_address(TREASURY_ADDR)

    asset_u = asset.upper()
    gas_used_total = 0
    gas_cost_total_one = Decimal("0")

    def _gas_cost_one(g_used: int, g_price_wei: int) -> Decimal:
        if not g_used or not g_price_wei:
            return Decimal("0")
        return (Decimal(g_used) * Decimal(g_price_wei)) / (Decimal(10) ** 18)

    # Case 1: native ONE
    if asset_u == "ONE":
        dec = 18
        amount_wei = int((amount_dec * (Decimal(10) ** dec)).to_integral_value(rounding=ROUND_DOWN))
        gas_price = TE._current_gas_price_wei_capped()
        tx = {
            "to": to_addr,
            "value": amount_wei,
            "chainId": TE.HMY_CHAIN_ID,
            "nonce": TE.w3.eth.get_transaction_count(from_addr),
            "gasPrice": gas_price,
        }
        try:
            est = TE.w3.eth.estimate_gas({**tx, "from": from_addr})
            tx["gas"] = max(int(est * 1.2), 50_000)
        except Exception:
            tx["gas"] = 100_000

        signed = acct.sign_transaction(tx)
        txh = TE.w3.eth.send_raw_transaction(signed.raw_transaction).hex()

        gas_used = 0
        try:
            r = TE.w3.eth.wait_for_transaction_receipt(txh, timeout=180)
            gas_used = int(getattr(r, "gasUsed", 0))
        except Exception:
            gas_used = 0

        gas_used_total = gas_used
        gas_cost_total_one = _gas_cost_one(gas_used, gas_price)
        return {"tx_hash": txh, "gas_used": gas_used_total, "gas_one": gas_cost_total_one}

    # Case 2: WONE unwrap then send ONE
    if asset_u == "WONE":
        dec = TE.get_decimals(WONE_ADDR)
        amount_wei = int((amount_dec * (Decimal(10) ** dec)).to_integral_value(rounding=ROUND_DOWN))

        wone_contract = TE.w3.eth.contract(address=WONE_ADDR, abi=WONE_ABI)
        fn = wone_contract.functions.withdraw(int(amount_wei))
        try:
            data_unwrap = fn._encode_transaction_data()
        except AttributeError:
            data_unwrap = fn.encode_abi()

        nonce0 = TE.w3.eth.get_transaction_count(from_addr)
        gas_price_unwrap = TE._current_gas_price_wei_capped()
        tx_unwrap = {
            "to": WONE_ADDR,
            "value": 0,
            "data": data_unwrap,
            "chainId": TE.HMY_CHAIN_ID,
            "nonce": nonce0,
            "gasPrice": gas_price_unwrap,
        }
        try:
            est_unwrap = TE.w3.eth.estimate_gas({**tx_unwrap, "from": from_addr})
            tx_unwrap["gas"] = max(int(est_unwrap * 1.2), 120_000)
        except Exception:
            tx_unwrap["gas"] = 150_000

        signed_unwrap = acct.sign_transaction(tx_unwrap)
        txh_unwrap = TE.w3.eth.send_raw_transaction(signed_unwrap.raw_transaction).hex()

        gas_used_unwrap = 0
        try:
            r_unwrap = TE.w3.eth.wait_for_transaction_receipt(txh_unwrap, timeout=180)
            gas_used_unwrap = int(getattr(r_unwrap, "gasUsed", 0))
        except Exception:
            gas_used_unwrap = 0
        cost_unwrap = _gas_cost_one(gas_used_unwrap, gas_price_unwrap)

        gas_price_send = TE._current_gas_price_wei_capped()
        tx_send = {
            "to": to_addr,
            "value": amount_wei,
            "chainId": TE.HMY_CHAIN_ID,
            "nonce": nonce0 + 1,
            "gasPrice": gas_price_send,
        }
        try:
            est_send = TE.w3.eth.estimate_gas({**tx_send, "from": from_addr})
            tx_send["gas"] = max(int(est_send * 1.2), 50_000)
        except Exception:
            tx_send["gas"] = 100_000

        signed_send = acct.sign_transaction(tx_send)
        txh_send = TE.w3.eth.send_raw_transaction(signed_send.raw_transaction).hex()

        gas_used_send = 0
        try:
            r_send = TE.w3.eth.wait_for_transaction_receipt(txh_send, timeout=180)
            gas_used_send = int(getattr(r_send, "gasUsed", 0))
        except Exception:
            gas_used_send = 0
        cost_send = _gas_cost_one(gas_used_send, gas_price_send)

        gas_used_total = gas_used_unwrap + gas_used_send
        gas_cost_total_one = cost_unwrap + cost_send

        return {
            "tx_hash": txh_send,
            "unwrap_tx_hash": txh_unwrap,
            "gas_used": gas_used_total,
            "gas_one": gas_cost_total_one,
        }

    # Case 3: ERC-20 transfer
    if runner is not None and hasattr(runner, "_addr"):
        token_addr = runner._addr(asset)
    else:
        token_addr = TE.get_token_address(asset)

    dec = TE.get_decimals(token_addr)
    amount_wei = int((amount_dec * (Decimal(10) ** dec)).to_integral_value(rounding=ROUND_DOWN))

    token = TE._erc20(token_addr)
    fn = token.functions.transfer(to_addr, int(amount_wei))
    try:
        data = fn._encode_transaction_data()
    except AttributeError:
        data = fn.encode_abi()

    gas_price = TE._current_gas_price_wei_capped()
    tx = {
        "to": Web3.to_checksum_address(token_addr),
        "value": 0,
        "data": data,
        "chainId": TE.HMY_CHAIN_ID,
        "nonce": TE.w3.eth.get_transaction_count(from_addr),
        "gasPrice": gas_price,
    }
    try:
        est = TE.w3.eth.estimate_gas({**tx, "from": from_addr})
        tx["gas"] = max(int(est * 1.2), 150_000)
    except Exception:
        tx["gas"] = 200_000

    signed = acct.sign_transaction(tx)
    txh = TE.w3.eth.send_raw_transaction(signed.raw_transaction).hex()

    gas_used = 0
    try:
        r = TE.w3.eth.wait_for_transaction_receipt(txh, timeout=180)
        gas_used = int(getattr(r, "gasUsed", 0))
    except Exception:
        gas_used = 0

    gas_used_total = gas_used
    gas_cost_total_one = _gas_cost_one(gas_used, gas_price)

    return {"tx_hash": txh, "gas_used": gas_used_total, "gas_one": gas_cost_total_one}

def _wd_handle_send(q, uid):
    # Answer immediately to avoid "query is too old"
    try:
        q.answer("Sending withdrawal...")
    except Exception:
        pass

    st = _wd_require_state(uid)
    wallet_key = st["wallet"]
    asset = st["asset"]
    try:
        amount_dec = Decimal(st["amount"])
    except Exception:
        _wd_reply_edit(q, "❌ Withdrawal failed\nBad amount.")
        return

    try:
        with _suppress_alerts_temporarily():
            res = _perform_withdraw(wallet_key, asset, amount_dec)
    except Exception as e:
        _wd_reply_edit(q, f"❌ Withdrawal failed\n{e}")
        return

    txh = _norm_tx_hash(str(res.get("tx_hash", "")))
    unwrap_txh = _norm_tx_hash(str(res.get("unwrap_tx_hash", "")))

    gas_used = int(res.get("gas_used", 0) or 0)
    gas_one = None
    try:
        gas_one = Decimal(str(res.get("gas_one", "0")))
    except Exception:
        gas_one = None

    explorer_main = f"https://explorer.harmony.one/tx/{txh}?shard=0" if txh else ""
    explorer_unwrap = f"https://explorer.harmony.one/tx/{unwrap_txh}?shard=0" if unwrap_txh else ""

    lines = [
        "✅ Withdrawal sent",
        f"From Wallet : {wallet_key}",
        f"Asset       : {asset}",
        f"Amount      : {st['amount']} {asset}",
        f"To          : {TREASURY_ADDR}",
    ]
    if unwrap_txh:
        lines += ["", "Unwrap WONE → ONE:", f"  tx: {unwrap_txh}"]
        if explorer_unwrap:
            lines.append(f"  {explorer_unwrap}")

    lines.append("")
    lines.append(_fmt_gas_summary(gas_used, gas_one))
    if txh:
        lines.append(f"Tx: {txh}")
    if explorer_main:
        lines.append(explorer_main)

    msg = "\n".join(lines).strip()

    try:
        q.edit_message_text(msg)
    except Exception:
        q.message.reply_text(msg)

def on_withdraw_callback(update: Update, context: CallbackContext):
    q = update.callback_query
    uid = q.from_user.id
    data = q.data or ""

    # Always answer quickly
    if data != "wd_do_send":
        try:
            q.answer()
        except Exception:
            pass

    if data == "wd_cancel":
        _WITHDRAW_STATE.pop(uid, None)
        q.edit_message_text("Canceled. No withdrawal sent.")
        return

    if data.startswith("wd_wallet:"):
        _wd_handle_wallet(q, uid, data.split(":", 1)[1]); return
    if data.startswith("wd_asset:"):
        _wd_handle_asset(q, uid, data.split(":", 1)[1]); return

    if data == "wd_amt_all":
        _wd_handle_amt_all(q, uid); return

    if data == "wd_back_wallet":
        _wd_handle_back(q, uid, "wallet"); return
    if data == "wd_back_asset":
        _wd_handle_back(q, uid, "asset"); return
    if data == "wd_back_amount":
        _wd_handle_back(q, uid, "amount"); return

    if data == "wd_do_send":
        _wd_handle_send(q, uid); return

# -----------------------------------------------------------------------------
# Error handler
# -----------------------------------------------------------------------------
def _log_error(update: object, context: CallbackContext):
    log.exception("Handler error")

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or getattr(C, "TELEGRAM_BOT_TOKEN", None)
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN not set in environment or config")

    up = Updater(token=token, use_context=True)
    dp = up.dispatcher

    # Core
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("help", cmd_help))
    dp.add_handler(CommandHandler("version", cmd_version))
    dp.add_handler(CommandHandler("sanity", cmd_sanity))
    dp.add_handler(CommandHandler("assets", cmd_assets))

    # On-chain read
    dp.add_handler(CommandHandler("prices", cmd_prices))
    dp.add_handler(CommandHandler("balances", cmd_balances))
    dp.add_handler(CommandHandler("slippage", cmd_slippage, pass_args=True))

    # Debug
    dp.add_handler(CommandHandler("ping", cmd_ping))
    dp.add_handler(CommandHandler("plan", cmd_plan))
    dp.add_handler(CommandHandler("dryrun", cmd_dryrun))
    dp.add_handler(CommandHandler("cooldowns", cmd_cooldowns, pass_args=True))

    # Manual flows
    dp.add_handler(CommandHandler("trade", cmd_trade))
    dp.add_handler(CommandHandler("withdraw", cmd_withdraw))

    # Callbacks
    dp.add_handler(CallbackQueryHandler(on_exec_button, pattern=r"^exec:[A-Za-z0-9_\-]+$"))
    dp.add_handler(CallbackQueryHandler(on_exec_confirm, pattern=r"^exec_go:[A-Za-z0-9_\-]+$"))
    dp.add_handler(CallbackQueryHandler(on_exec_cancel, pattern=r"^exec_cancel$"))

    dp.add_handler(CallbackQueryHandler(on_trade_callback, pattern=r"^tw_"))
    dp.add_handler(CallbackQueryHandler(on_withdraw_callback, pattern=r"^wd_"))

    # Amount capture
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, on_text_amount_capture), group=0)

    dp.add_error_handler(_log_error)

    log.info("TECBot Telegram listener started")
    up.start_polling(clean=True)
    up.idle()

if __name__ == "__main__":
    main()

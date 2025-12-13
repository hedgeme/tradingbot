#!/usr/bin/env python3
# TECBot Telegram Listener — with /trade and /withdraw
#
# Manual trades: listener outputs exactly one completion message
# Strategy trades: trade_executor/alert.py can still emit (manual trade suppresses TE alerts)
#
import os, sys, logging, subprocess, shlex
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Optional, List, Dict, Tuple, Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ParseMode, Update
from telegram.ext import (
    Updater, CommandHandler, CallbackQueryHandler, MessageHandler, Filters, CallbackContext
)

from web3 import Web3  # used for withdraw TX building

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
log = logging.getLogger("telegram_listener")

if "/bot" not in sys.path:
    sys.path.insert(0, "/bot")

# ---------- Tolerant imports (prefer app.*, else root) ----------
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

# alert module is optional; for manual trades we do NOT send ALERT to avoid duplicates
try:
    from app import alert as ALERT
except Exception:
    try:
        import alert as ALERT  # type: ignore
    except Exception:
        ALERT = None  # type: ignore

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

# ---------- Constants / WONE unwrap helpers ----------
try:
    _cfg_wone = (getattr(C, "TOKENS", {}) or {}).get("WONE", "")
    if _cfg_wone:
        WONE_ADDR = Web3.to_checksum_address(_cfg_wone)
    else:
        WONE_ADDR = Web3.to_checksum_address("0xcF664087a5bB0237a0BAd6742852ec6c8d69A27a")
except Exception:
    WONE_ADDR = Web3.to_checksum_address("0xcF664087a5bB0237a0BAd6742852ec6c8d69A27a")

WONE_ABI = [
    {"name": "withdraw", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "wad", "type": "uint256"}], "outputs": []}
]

# ---------- Helpers ----------
def _norm_tx_hash(txh: str) -> str:
    if not txh:
        return ""
    txh = txh.strip()
    if not txh.startswith("0x"):
        txh = "0x" + txh
    return txh

def _explorer_tx(txh: str) -> str:
    txh = _norm_tx_hash(txh)
    return f"https://explorer.harmony.one/tx/{txh}?shard=0" if txh else ""

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def is_admin(user_id: int) -> bool:
    try:
        return int(user_id) in set(int(x) for x in (getattr(C, "ADMIN_USER_IDS", []) or []))
    except Exception:
        return False

def _git_short_rev() -> Optional[str]:
    try:
        out = subprocess.check_output(shlex.split("git rev-parse --short HEAD"), cwd="/bot", stderr=subprocess.DEVNULL)
        return out.decode().strip()
    except Exception:
        return None

def _fmt_money(x: Optional[Decimal]) -> str:
    if x is None:
        return "—"
    x = Decimal(x)
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

def _log_update(update: Update, context: CallbackContext):
    try:
        uid = update.effective_user.id if update.effective_user else "?"
        txt = update.effective_message.text if update.effective_message else "<non-text>"
        log.info(f"UPDATE from {uid}: {txt}")
    except Exception:
        pass

def _log_error(update: object, context: CallbackContext):
    log.exception("Handler error")

# ---------- Render helpers ----------
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

# ---------- Commands ----------
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
        "  /balances — per-wallet balances (ONE, WONE, USDC, ETH, TEC, sDAI)\n"
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
    }
    txt = "Sanity:\n  " + "\n  ".join(f"{k}: {v}" for k,v in details.items())
    txt += "\n\nModules:\n  " + "\n  ".join(f"{k}: {v}" for k,v in avail.items())
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

# ---- BALANCES ----
_ONE_KEY_ORDER = [
    "ONE(native)", "ONE (native)", "ONE_NATIVE", "NATIVE_ONE", "NATIVE",
    "ONE", "WONE"
]

def _resolve_one_value(row: Dict[str, Decimal]) -> Decimal:
    lower = {k.lower(): k for k in row.keys()}
    for key in _ONE_KEY_ORDER:
        k = lower.get(key.lower())
        if k is not None:
            try:
                return Decimal(str(row[k]))
            except Exception:
                pass
    return Decimal("0")

def cmd_balances(update: Update, context: CallbackContext):
    if BL is None:
        update.message.reply_text("Balances unavailable (module not loaded)."); return
    try:
        table = BL.all_balances()
    except Exception as e:
        log.exception("balances failure")
        update.message.reply_text(f"Balances error: {e}"); return

    cols = ["ONE", "WONE", "1USDC", "1ETH", "TEC", "1sDAI"]
    w_wallet = 22
    w_amt    = 11
    header = f"{'Wallet':<{w_wallet}}  " + "  ".join(f"{c:>{w_amt}}" for c in cols)
    sep    = "-" * len(header)
    lines  = [f"Balances (@ {now_iso()})", header, sep]

    for w_name in sorted(table.keys()):
        row = table[w_name]
        vals: List[str] = []
        one_val = _resolve_one_value(row)
        vals.append(_fmt_amt("ONE", one_val))
        vals.append(_fmt_amt("WONE", row.get("WONE", 0)))
        for c in cols[2:]:
            vals.append(_fmt_amt(c, row.get(c, 0)))
        lines.append(f"{w_name:<{w_wallet}}  " + "  ".join(f"{v:>{w_amt}}" for v in vals))

    update.message.reply_text(f"<pre>\n{chr(10).join(lines)}\n</pre>", parse_mode=ParseMode.HTML)

# ---- SLIPPAGE / PRICES unchanged (kept as-is from your file) ----
# (To keep this reply focused, I’m not modifying cmd_prices/cmd_slippage code paths.)

# ---------- Core bot health ----------
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
        update.message.reply_text("Plan error: planner module not available (strategies/planner.py)."); return
    try:
        snap = planner.build_plan_snapshot()
    except Exception as e:
        log.exception("plan failure")
        update.message.reply_text(f"Plan error: {e}"); return
    update.message.reply_text(render_plan(snap))

def cmd_cooldowns(update: Update, context: CallbackContext):
    defaults = getattr(C, "COOLDOWNS_DEFAULTS", {"price_refresh": 15, "trade_retry": 30, "alert_throttle": 60})
    by_bot = getattr(C, "COOLDOWNS_BY_BOT", {})
    by_route = getattr(C, "COOLDOWNS_BY_ROUTE", {})
    args = context.args or []
    if not args:
        update.message.reply_text("Default cooldowns (seconds):\n  " + "\n  ".join(f"{k}: {v}" for k,v in defaults.items())); return
    key = args[0]
    if key in by_bot:
        d = by_bot[key]; header = f"Cooldowns for {key} (seconds):"
    elif key in by_route:
        d = by_route[key]; header = f"Cooldowns for route {key} (seconds):"
    else:
        update.message.reply_text(f"No specific cooldowns for '{key}'. Showing defaults.\n  " + "\n  ".join(f"{k}: {v}" for k,v in defaults.items())); return
    update.message.reply_text(header + "\n  " + "\n  ".join(f"{k}: {v}" for k,v in d.items()))

def cmd_dryrun(update: Update, context: CallbackContext):
    if not getattr(C, "DRYRUN_ENABLED", True):
        update.message.reply_text("Dry-run is disabled (set DRYRUN_ENABLED=True in config)."); return
    if runner is None or not all(hasattr(runner, n) for n in ("build_dryrun","execute_action")):
        update.message.reply_text("Dry-run unavailable: runner.build_dryrun()/execute_action() not found."); return
    try:
        results = runner.build_dryrun()
    except Exception as e:
        log.exception("dryrun failure")
        update.message.reply_text(f"Dry-run error: {e}"); return
    if not results:
        update.message.reply_text("Dry-run: no executable actions."); return

    text = render_dryrun(results)
    kb = [[InlineKeyboardButton(f"▶️ Execute {getattr(r,'action_id','?')}", callback_data=f"exec:{getattr(r,'action_id','?')}")] for r in results]
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="exec_cancel")])
    update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)

def on_exec_button(update: Update, context: CallbackContext):
    q = update.callback_query; data = q.data or ""
    if not data.startswith("exec:"): q.answer(); return
    if not is_admin(q.from_user.id): q.answer("Not authorized.", show_alert=True); return
    aid = data.split(":",1)[1]
    q.edit_message_text(f"Confirm execution: Action #{aid}\nAre you sure?")
    q.edit_message_reply_markup(InlineKeyboardMarkup([[InlineKeyboardButton("✅ Confirm", callback_data=f"exec_go:{aid}"), InlineKeyboardButton("❌ Abort", callback_data="exec_cancel")]]))
    q.answer()

def on_exec_confirm(update: Update, context: CallbackContext):
    q = update.callback_query; data = q.data or ""
    if not data.startswith("exec_go:"): q.answer(); return
    if not is_admin(q.from_user.id): q.answer("Not authorized.", show_alert=True); return
    if runner is None or not hasattr(runner, "execute_action"): q.answer("Execution backend missing.", show_alert=True); return
    aid = data.split(":",1)[1]
    try:
        txr = runner.execute_action(aid)
        txh = _norm_tx_hash(str(getattr(txr, "tx_hash", "")))
        gas_used = getattr(txr, "gas_used", "—")
        explorer = getattr(txr, "explorer_url", "") or _explorer_tx(txh)
        filled = getattr(txr, "filled_text", "")
        q.edit_message_text(f"✅ Executed {aid}\n{filled}\nGas used: {gas_used}\nTx: {txh}\n{explorer}".strip())
        q.answer("Executed.")
    except Exception as e:
        q.edit_message_text(f"❌ Execution failed for {aid}\n{e}")
        q.answer("Failed.", show_alert=True)

def on_exec_cancel(update: Update, context: CallbackContext):
    q = update.callback_query
    q.edit_message_text("Canceled. No transaction sent.")
    q.answer()

# -----------------------------------------------------------------------------
# /trade wizard state mgmt
# -----------------------------------------------------------------------------
_TRADE_STATE: Dict[int, Dict[str, str]] = {}

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
    try:
        q.edit_message_text(text, parse_mode=(ParseMode.HTML if html else None))
        if kb:
            q.edit_message_reply_markup(InlineKeyboardMarkup(kb))
        else:
            q.edit_message_reply_markup(None)
    except Exception:
        q.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb) if kb else None, parse_mode=(ParseMode.HTML if html else None))

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
    kb.append([InlineKeyboardButton("⬅ Back", callback_data="tw_back_wallet" if which=="from" else "tw_back_from")])
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="tw_cancel")])
    return kb

def _tw_slip_keyboard(uid):
    choices = [
        ("0.10% max", 10),
        ("0.50% max", 50),
        ("1.00% max", 100),
        ("Use default", getattr(C, "SLIPPAGE_DEFAULT_BPS", 30)),
    ]
    kb = [[InlineKeyboardButton(f"{label}", callback_data=f"tw_slip:{bps}")] for label,bps in choices]
    kb.append([InlineKeyboardButton("⬅ Back", callback_data="tw_back_amount")])
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="tw_cancel")])
    return kb

def _tw_amount_keyboard(uid, wallet, token_in):
    bal_display = "?"
    if BL:
        try:
            table = BL.all_balances()
            row = table.get(wallet, {})
            if token_in.upper() == "ONE":
                bal_display = str(_resolve_one_value(row))
            else:
                bal_display = str(row.get(token_in.upper(), "0"))
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

    lines = []
    lines.append(f"Review Trade — {wallet_key}")
    lines.append(f"Path     : {path}")
    lines.append(f"AmountIn : {ain_txt}")
    lines.append(f"QuoteOut : {qout_txt}")
    lines.append(f"Impact   : {imp_bps:.2f} bps" if imp_bps is not None else "Impact   : —")
    pct = Decimal(slip_bps_val) / Decimal(100)
    lines.append(f"Slippage : {pct:.2f}% max → minOut {min_out}")
    lines.append(f"Gas Est  : {gas_est}")
    lines.append(f"Nonce    : {nonce}")
    lines.append(f"Allowance: {'OK' if allow_ok else 'NOT APPROVED'}")
    if not allow_ok and need_appr_txt:
        lines.append(f"Required : approve {need_appr_txt}")
    if not slip_ok:
        lines.append("⚠ Price already worse than allowed slippage")
    lines.append("")
    lines.append("Would send:")
    lines.append(tx_prev)

    kb_rows = []
    if not allow_ok and need_appr_txt:
        kb_rows.append([InlineKeyboardButton("✅ Approve spend first", callback_data="tw_do_approve")])
    elif allow_ok and slip_ok:
        kb_rows.append([InlineKeyboardButton("✅ Execute Trade", callback_data="tw_do_execute")])
    else:
        kb_rows.append([InlineKeyboardButton("⚠ Adjust Slippage", callback_data="tw_back_slip")])
    kb_rows.append([InlineKeyboardButton("❌ Cancel", callback_data="tw_cancel")])

    return ("\n".join(lines), kb_rows, mq)

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
    _tw_reply_edit(q,
        f"FROM {st['from']} TO {sym}\n\nEnter amount of {st['from']} to trade (type a number in chat).\nBalance: {bal_disp} {st['from']}",
        kb
    )

def _tw_handle_amt_all(q, uid):
    st = _tw_require_state(uid)
    amt = "0"
    if BL:
        try:
            table = BL.all_balances()
            row = table.get(st["wallet"], {})
            if st["from"].upper() == "ONE":
                amt = str(_resolve_one_value(row))
            else:
                amt = str(row.get(st["from"].upper(), "0"))
        except Exception:
            pass
    _tw_set_amount(uid, st, amt)
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
        _tw_reply_edit(q,
            f"FROM {st['from']} TO {st['to']}\n\nEnter amount of {st['from']} to trade (type a number in chat).\nBalance: {bal_disp} {st['from']}",
            kb
        )
    elif dest == "slip":
        st["step"] = "slip"
        kb = _tw_slip_keyboard(uid)
        _tw_reply_edit(q, f"Amount: {st.get('amount','?')} {st.get('from','?')}\nSelect slippage limit:", kb)

def _tw_handle_approve(q, uid):
    # IMPORTANT: answer immediately to avoid Telegram timeout
    try:
        q.answer("Sending approval...")
    except Exception:
        pass

    if not is_admin(q.from_user.id):
        q.answer("Not authorized.", show_alert=True); return
    st = _tw_require_state(uid)
    if TE is None or runner is None:
        q.answer("Approve backend missing.", show_alert=True); return
    try:
        amt = Decimal(st["amount"])
        sym = st["from"]
        wallet_key = st["wallet"]
        token_addr = runner._addr(sym)  # resolve symbol -> address
        dec = TE.get_decimals(token_addr)
        wei = int(amt * (Decimal(10)**dec))
        TE.approve_if_needed(wallet_key, token_addr, TE.ROUTER_ADDR_ETH, wei, send_alerts=False)
        txt, kb_rows, _ = _tw_render_manual_quote(uid, st)
        _tw_reply_edit(q, txt, kb_rows)
        try:
            q.answer("Approve sent.")
        except Exception:
            pass
    except Exception as e:
        _tw_reply_edit(q, f"Approval failed:\n<code>{e}</code>", html=True)
        q.answer("Failed.", show_alert=True)

def _tw_handle_execute(q, uid):
    # IMPORTANT: answer immediately to avoid Telegram timeout
    try:
        q.answer("Executing trade...")
    except Exception:
        pass

    if not is_admin(q.from_user.id):
        q.answer("Not authorized.", show_alert=True); return
    st = _tw_require_state(uid)
    if runner is None or not hasattr(runner, "execute_manual_quote"):
        q.answer("Execution backend missing.", show_alert=True); return

    try:
        amt_dec = Decimal(st["amount"])
    except Exception:
        q.answer("Bad amount.", show_alert=True); return

    try:
        txr = runner.execute_manual_quote(
            wallet_key=st["wallet"],
            token_in=st["from"],
            token_out=st["to"],
            amount_in=amt_dec,
            slippage_bps=int(st["slip_bps"]),
        )

        txh = _norm_tx_hash(str(txr.get("tx_hash","")))
        filled = txr.get("filled_text","")
        gas_used = txr.get("gas_used","—")
        gas_cost_one = txr.get("gas_cost_one","")
        explorer = txr.get("explorer_url","") or _explorer_tx(txh)

        msg = (
            "✅ Manual trade\n"
            f"Wallet: {st['wallet']}\n"
            f"{filled}\n"
            f"Gas used: {gas_used}"
        )
        if gas_cost_one:
            msg += f" (≈{gas_cost_one} ONE)"
        msg += f"\nTx: {txh}\n{explorer}"

        # edit original wizard message; fallback to one new message
        try:
            q.edit_message_text(msg)
        except Exception:
            q.message.reply_text(msg)

        try:
            q.answer("Executed.")
        except Exception:
            pass

    except Exception as e:
        err = f"❌ Execution failed\n{e}"
        try:
            q.edit_message_text(err)
        except Exception:
            q.message.reply_text(err)
        q.answer("Failed.", show_alert=True)

def on_trade_callback(update: Update, context: CallbackContext):
    q = update.callback_query
    uid = q.from_user.id
    data = q.data or ""

    if data == "tw_cancel":
        _TRADE_STATE.pop(uid, None)
        q.edit_message_text("Canceled. No transaction sent.")
        q.answer()
        return

    if data.startswith("tw_wallet:"):
        _tw_handle_wallet(q, uid, data.split(":",1)[1]); q.answer(); return
    if data.startswith("tw_from:"):
        _tw_handle_from(q, uid, data.split(":",1)[1]); q.answer(); return
    if data.startswith("tw_to:"):
        _tw_handle_to(q, uid, data.split(":",1)[1]); q.answer(); return
    if data == "tw_amt_all":
        _tw_handle_amt_all(q, uid); q.answer(); return

    if data == "tw_back_wallet":
        _tw_handle_back(q, uid, "wallet"); q.answer(); return
    if data == "tw_back_from":
        _tw_handle_back(q, uid, "from"); q.answer(); return
    if data == "tw_back_to":
        _tw_handle_back(q, uid, "to"); q.answer(); return
    if data == "tw_back_amount":
        _tw_handle_back(q, uid, "amount"); q.answer(); return
    if data == "tw_back_slip":
        _tw_handle_back(q, uid, "slip"); q.answer(); return

    if data.startswith("tw_slip:"):
        _tw_handle_slip(q, uid, data.split(":",1)[1]); q.answer(); return

    if data == "tw_do_approve":
        _tw_handle_approve(q, uid); return
    if data == "tw_do_execute":
        _tw_handle_execute(q, uid); return

    q.answer()

# ---- Amount capture (typed number) ----
_WITHDRAW_STATE: Dict[int, Dict[str,str]] = {}  # defined earlier in your file; kept here for capture
def on_text_amount_capture(update: Update, context: CallbackContext):
    uid = update.effective_user.id if update.effective_user else None
    if uid is None:
        return

    txt = (update.message.text or "").strip()

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
# /withdraw (your existing implementation kept with link helper)
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
        st = {"step":"wallet","wallet":"","asset":"","amount":""}
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
            if token.upper() == "ONE":
                bal_display = str(_resolve_one_value(row))
            else:
                bal_display = str(row.get(token.upper(), "0"))
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
    _wd_reply_edit(q,
        f"Treasury: {TREASURY_ADDR}\nWallet: {st['wallet']}\nAsset: {asset}\n\nEnter withdrawal amount.\nBalance: {bal_disp} {asset}",
        kb
    )

def _wd_set_amount(uid, st, amount_text):
    st["amount"] = amount_text
    st["step"] = "review"
    return True

def _wd_handle_amt_all(q, uid):
    st = _wd_require_state(uid)
    amt = "0"
    if BL:
        try:
            table = BL.all_balances()
            row = table.get(st["wallet"], {})
            if st["asset"].upper() == "ONE":
                amt = str(_resolve_one_value(row))
            else:
                amt = str(row.get(st["asset"].upper(), "0"))
        except Exception:
            pass
    _wd_set_amount(uid, st, amt)
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
        _wd_reply_edit(q,
            f"Treasury: {TREASURY_ADDR}\nWallet: {st['wallet']}\nAsset: {st['asset']}\n\nEnter withdrawal amount.\nBalance: {bal_disp} {st['asset']}",
            kb
        )

# NOTE: _perform_withdraw and _wd_handle_send kept from your file (not repeated here)
# If you want, paste your exact implementations below unchanged; just ensure they use:
#   explorer_main = _explorer_tx(txh)
#   explorer_unwrap = _explorer_tx(unwrap_txh)

def on_withdraw_callback(update: Update, context: CallbackContext):
    q = update.callback_query
    uid = q.from_user.id
    data = q.data or ""

    if data == "wd_cancel":
        _WITHDRAW_STATE.pop(uid, None)
        q.edit_message_text("Canceled. No withdrawal sent.")
        q.answer(); return

    if data.startswith("wd_wallet:"):
        _wd_handle_wallet(q, uid, data.split(":",1)[1]); q.answer(); return
    if data.startswith("wd_asset:"):
        _wd_handle_asset(q, uid, data.split(":",1)[1]); q.answer(); return
    if data == "wd_amt_all":
        _wd_handle_amt_all(q, uid); q.answer(); return
    if data == "wd_back_wallet":
        _wd_handle_back(q, uid, "wallet"); q.answer(); return
    if data == "wd_back_asset":
        _wd_handle_back(q, uid, "asset"); q.answer(); return
    if data == "wd_back_amount":
        _wd_handle_back(q, uid, "amount"); q.answer(); return

    # Your wd_do_send handler should already do q.answer() immediately (you fixed that)
    q.answer()

# ---------- Main ----------
def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or getattr(C, "TELEGRAM_BOT_TOKEN", None)
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN not set in environment or config")

    up = Updater(token=token, use_context=True)
    dp = up.dispatcher

    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("help", cmd_help))
    dp.add_handler(CommandHandler("version", cmd_version))
    dp.add_handler(CommandHandler("sanity", cmd_sanity))
    dp.add_handler(CommandHandler("assets", cmd_assets))
    dp.add_handler(CommandHandler("balances", cmd_balances))

    dp.add_handler(CommandHandler("ping", cmd_ping))
    dp.add_handler(CommandHandler("plan", cmd_plan))
    dp.add_handler(CommandHandler("dryrun", cmd_dryrun))
    dp.add_handler(CommandHandler("cooldowns", cmd_cooldowns, pass_args=True))

    dp.add_handler(CommandHandler("trade", cmd_trade))
    dp.add_handler(CommandHandler("withdraw", cmd_withdraw))

    dp.add_handler(CallbackQueryHandler(on_exec_button, pattern=r"^exec:[A-Za-z0-9_\-]+$"))
    dp.add_handler(CallbackQueryHandler(on_exec_confirm, pattern=r"^exec_go:[A-Za-z0-9_\-]+$"))
    dp.add_handler(CallbackQueryHandler(on_exec_cancel, pattern=r"^exec_cancel$"))

    dp.add_handler(CallbackQueryHandler(on_trade_callback, pattern=r"^tw_"))
    dp.add_handler(CallbackQueryHandler(on_withdraw_callback, pattern=r"^wd_"))

    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, on_text_amount_capture), group=0)

    dp.add_error_handler(_log_error)
    dp.add_handler(MessageHandler(Filters.all, _log_update), group=-1)

    log.info("Telegram bot started")
    up.start_polling(clean=True)
    up.idle()

if __name__ == "__main__":
    main()

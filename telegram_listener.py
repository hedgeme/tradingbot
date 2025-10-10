#!/usr/bin/env python3
# TECBot Telegram Listener — python-telegram-bot 13.x, chain-first + Coinbase compare

import os, sys, logging, subprocess, shlex
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, List, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ParseMode, Update
from telegram.ext import (
    Updater, CommandHandler, CallbackQueryHandler, MessageHandler, Filters, CallbackContext
)

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
log = logging.getLogger("telegram_listener")

# PYTHONPATH so /bot works
if "/bot" not in sys.path:
    sys.path.insert(0, "/bot")

# ---------- tolerant imports ----------
# config
try:
    from app import config as C
    log.info("Loaded config from app.config")
except Exception:
    try:
        import config as C
        log.info("Loaded config from root config")
    except Exception as e:
        log.exception("Failed to import config")
        raise

# on-chain modules
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

# planner/runner (optional)
planner = None
try:
    from app.strategies import planner
    log.info("Loaded planner from app.strategies.planner")
except Exception:
    try:
        from strategies import planner
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
        import runner
        log.info("Loaded runner from root runner")
    except Exception as e:
        log.warning("runner module not available: %s", e)
        runner = None

# coinbase spot (optional)
CB = None
try:
    import coinbase_client as CB
except Exception:
    CB = None

# ---------- utils ----------
def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def is_admin(user_id: int) -> bool:
    try:
        return int(user_id) in set(int(x) for x in getattr(C, "ADMIN_USER_IDS", []) or [])
    except Exception:
        return False

def _git_short_rev() -> Optional[str]:
    try:
        out = subprocess.check_output(shlex.split("git rev-parse --short HEAD"), cwd="/bot", stderr=subprocess.DEVNULL)
        return out.decode().strip()
    except Exception:
        return None

def _fmt_kv(d: dict, indent: int = 2) -> str:
    pad = " " * indent
    return "\n".join(f"{pad}{k}: {v}" for k, v in d.items())

# ---------- rendering ----------
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
        if imp is not None:
            lines.append(f"Impact   : {imp:.2f} bps")
        if slip is not None:
            lines.append(f"Slippage : {slip} bps → minOut {mino}")
        else:
            lines.append(f"minOut   : {mino}")
        lines.append(
            f"Gas Est  : {gas}\n"
            f"Allowance: {'OK' if allow else 'NEEDED'}\n"
            f"Nonce    : {nonce}\n"
            f"Would send:\n{txp}\n"
        )
    return "\n".join(lines).strip()

# ---------- basic logging ----------
def _log_update(update: Update, context: CallbackContext):
    try:
        uid = update.effective_user.id if update.effective_user else "?"
        txt = update.effective_message.text if update.effective_message else "<non-text>"
        log.info(f"UPDATE from {uid}: {txt}")
    except Exception:
        pass

def _log_error(update: object, context: CallbackContext):
    log.exception("Handler error")

# ---------- commands ----------
def cmd_start(update: Update, context: CallbackContext):
    update.message.reply_text(
        "TECBot online.\n"
        "Try: /help\n"
        "Core: /plan /dryrun /cooldowns /ping\n"
        "On-chain: /prices [SYMS…] /balances /slippage <IN> [AMOUNT] [OUT]\n"
        "Meta: /version /sanity /assets"
    )

def cmd_help(update: Update, context: CallbackContext):
    update.message.reply_text(
        "Commands:\n"
        "  /ping — health check\n"
        "  /plan — preview proposed actions (planner)\n"
        "  /dryrun — simulate current action(s) with Execute button (runner)\n"
        "  /cooldowns [bot|route] — show cooldowns\n"
        "  /prices [SYMS…] — on-chain quotes in USDC (e.g. /prices 1ETH TEC)\n"
        "  /balances — per-wallet balances (ERC-20 + native ONE)\n"
        "  /slippage <IN> [AMOUNT] [OUT] — live impact/minOut (default OUT=1USDC, AMOUNT=1)\n"
        "  /assets — configured tokens & wallets\n"
        "  /version — code version\n"
        "  /sanity — config/modules sanity"
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
    }
    update.message.reply_text("Sanity:\n" + _fmt_kv(details) + "\n\nModules:\n" + _fmt_kv(avail))

def cmd_assets(update: Update, context: CallbackContext):
    tokens = getattr(C, "TOKENS", {})
    wallets = getattr(C, "WALLETS", {})
    lines = ["Assets:", "Tokens:"]
    for sym, addr in tokens.items():
        lines.append(f"  {sym}: {addr}")
    lines.append("Wallets:")
    for name, addr in wallets.items():
        lines.append(f"  {name}: {addr}")
    update.message.reply_text("\n".join(lines))

# -------- /prices (LP + Coinbase compare) --------
def _pretty_money(x: Optional[Decimal]) -> str:
    if x is None:
        return "—"
    # 4 decimals for “stable-ish”, 2 for big numbers
    if x >= 100:
        return f"${x:,.2f}"
    return f"${x:,.6f}" if x < Decimal("0.1") else f"${x:,.4f}"

def _coinbase_eth_usd() -> Optional[Decimal]:
    if CB is None:
        return None
    try:
        # expects your coinbase_client.get_spot("ETH-USD") -> Decimal
        px = CB.get_spot("ETH-USD")
        return Decimal(str(px))
    except Exception:
        return None

def cmd_prices(update: Update, context: CallbackContext):
    if PR is None:
        update.message.reply_text("Prices unavailable (module not loaded)."); return

    # default symbols list
    args = (context.args or [])
    syms = [s.upper() for s in args] if args else ["ONE", "1USDC", "1sDAI", "TEC", "1ETH"]

    # Compute LP prices
    lp: dict = {}
    notes: List[str] = []
    for s in syms:
        try:
            val = PR.price_usd(s, Decimal("1"))
            lp[s] = val
        except Exception as e:
            lp[s] = None
            notes.append(f"{s}: error ({e})")

    # Build header
    lines = ["LP Prices"]
    # Prefer a stable order
    order = [x for x in ["ONE","WONE","1USDC","1sDAI","TEC","1ETH"] if x in lp] + [s for s in syms if s not in ("ONE","WONE","1USDC","1sDAI","TEC","1ETH")]
    for s in order:
        # Hide raw ONE if it isn’t priced; but our prices.py maps ONE->WONE, so it should now show
        if s == "ONE" and lp.get(s) is None:
            continue
        lines.append(f"  {s:<6} {_pretty_money(lp.get(s))}")

    # ETH LP vs Coinbase
    lp_eth = lp.get("1ETH")
    cb_eth = _coinbase_eth_usd()
    if lp_eth is not None and cb_eth is not None:
        try:
            diff = (lp_eth - cb_eth) / cb_eth * Decimal("100")
            lines += [
                "",
                "ETH: Harmony LP vs Coinbase",
                f"  LP:       {_pretty_money(lp_eth)}",
                f"  Coinbase: {_pretty_money(cb_eth)}",
                f"  Diff:     {diff:.2f}%"
            ]
        except Exception:
            pass

    if notes:
        lines += ["", "Notes:"] + [f"  - {n}" for n in notes]

    update.message.reply_text("\n".join(lines))

# -------- /balances (prettier) --------
def cmd_balances(update: Update, context: CallbackContext):
    if BL is None:
        update.message.reply_text("Balances unavailable (module not loaded)."); return
    try:
        table = BL.all_balances()
    except Exception as e:
        log.exception("balances failure")
        update.message.reply_text(f"Balances error: {e}"); return
    lines = [f"Balances (@ {now_iso()} UTC)"]
    for w_name, row in table.items():
        lines.append(f"\n{w_name}:")
        for sym, amt in row.items():
            # Drop scientific zeros; align a bit
            v = f"{amt:.8f}" if hasattr(amt, 'quantize') or isinstance(amt, Decimal) else str(amt)
            if v.upper() == "0E-8":
                v = "0"
            lines.append(f"  {sym:<9} {v}")
    update.message.reply_text("\n".join(lines))

# -------- /slippage (curve-style summary) --------
def cmd_slippage(update: Update, context: CallbackContext):
    args = context.args or []
    if not args:
        update.message.reply_text(
            "Usage: /slippage <TOKEN_IN> [AMOUNT] [TOKEN_OUT]\n"
            "Examples:\n"
            "  /slippage 1ETH            (defaults: 1 unit to 1USDC)\n"
            "  /slippage 1ETH 0.5 1USDC  (0.5 1ETH to 1USDC)"
        ); return

    token_in = args[0].upper()
    amount_in = Decimal(args[1]) if len(args) >= 2 else Decimal("1")
    token_out = args[2].upper() if len(args) >= 3 else "1USDC"

    # Mid price (LP) to express slippage vs mid
    try:
        mid_usd = PR.price_usd(token_in, Decimal("1"))
    except Exception:
        mid_usd = None

    # If slippage module has a helper, use it once for the user-requested size
    summary_line = None
    if SL and hasattr(SL, "compute_slippage"):
        try:
            res = SL.compute_slippage(token_in, token_out, amount_in, int(getattr(C, "SLIPPAGE_DEFAULT_BPS", 30)))
            if res:
                summary_line = f"Size {amount_in} {token_in}: price { (res['amount_out']/amount_in) if amount_in else Decimal('0') :.2f} {token_out}/{token_in} · impact {res['impact_bps']} bps"
        except Exception:
            pass

    # Build a lightweight curve (target out sizes in USDC, estimated via mid)
    targets = [Decimal("10"), Decimal("100"), Decimal("1000"), Decimal("10000")]
    rows: List[Tuple[str,str,str,str]] = []
    for usdc in targets:
        # estimate amount_in by mid price if available; else use user amount scaled
        if mid_usd and mid_usd > 0:
            est_in = (usdc / mid_usd).quantize(Decimal("0.000001"))
        else:
            est_in = amount_in
        try:
            px_usd = PR.price_usd(token_in, est_in)
            if px_usd is None or est_in is None:
                raise ValueError("no quote")
            eff_price = (px_usd / est_in) if est_in > 0 else None  # USDC per 1 token
            if mid_usd and eff_price:
                slip = (eff_price - mid_usd) / mid_usd * Decimal("100")
                slip_txt = f"{slip:+.2f}%"
            else:
                slip_txt = "—"
            rows.append((f"{usdc:>8,.0f}", f"{est_in:.6f}", f"${eff_price:,.2f}" if eff_price else "—", slip_txt))
        except Exception:
            rows.append((f"{usdc:>8,.0f}", "—", "—", "—"))

    lines = [f"Slippage curve: {token_in} → {token_out}"]
    if mid_usd:
        lines.append(f"Baseline (mid): ${mid_usd:,.2f} per 1{token_in}")
    if summary_line:
        lines.append(summary_line)
    lines.append("")
    lines.append(f"{'Size (USDC)':>12} | {'Amount In (sym)':>16} | {'Eff. Price':>11} | {'Slippage vs mid':>15}")
    for a,b,c,d in rows:
        lines.append(f"{a:>12} | {b:>16} | {c:>11} | {d:>15}")
    update.message.reply_text("\n".join(lines))

# -------- planner/runner --------
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
        update.message.reply_text("Default cooldowns (seconds):\n" + _fmt_kv(defaults)); return
    key = args[0]
    if key in by_bot:
        d = by_bot[key]; header = f"Cooldowns for {key} (seconds):"
    elif key in by_route:
        d = by_route[key]; header = f"Cooldowns for route {key} (seconds):"
    else:
        update.message.reply_text(f"No specific cooldowns for '{key}'. Showing defaults.\n" + _fmt_kv(defaults)); return
    update.message.reply_text(header + "\n" + _fmt_kv(d))

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
        txh = getattr(txr, "tx_hash", "0x")
        filled = getattr(txr, "filled_text", "")
        gas_used = getattr(txr, "gas_used", "—")
        explorer = getattr(txr, "explorer_url", "")
        q.edit_message_text(f"✅ Executed {aid}\n{filled}\nGas used: {gas_used}\nTx: {txh}\n{explorer}".strip())
        q.answer("Executed.")
    except Exception as e:
        q.edit_message_text(f"❌ Execution failed for {aid}\n{e}")
        q.answer("Failed.", show_alert=True)

def on_exec_cancel(update: Update, context: CallbackContext):
    q = update.callback_query
    q.edit_message_text("Canceled. No transaction sent.")
    q.answer()

# ---------- main ----------
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

    dp.add_handler(CommandHandler("prices", cmd_prices))
    dp.add_handler(CommandHandler("balances", cmd_balances))
    dp.add_handler(CommandHandler("slippage", cmd_slippage, pass_args=True))

    dp.add_handler(CommandHandler("ping", cmd_ping))
    dp.add_handler(CommandHandler("plan", cmd_plan))
    dp.add_handler(CommandHandler("dryrun", cmd_dryrun))
    dp.add_handler(CommandHandler("cooldowns", cmd_cooldowns, pass_args=True))

    dp.add_handler(CallbackQueryHandler(on_exec_button, pattern=r"^exec:[A-Za-z0-9_\-]+$"))
    dp.add_handler(CallbackQueryHandler(on_exec_confirm, pattern=r"^exec_go:[A-Za-z0-9_\-]+$"))
    dp.add_handler(CallbackQueryHandler(on_exec_cancel, pattern=r"^exec_cancel$"))
    dp.add_error_handler(_log_error)
    dp.add_handler(MessageHandler(Filters.all, _log_update), group=-1)

    log.info("Handlers registered: /start /strat /help /version /sanity /assets /prices /balances /slippage /ping /plan /dryrun /cooldowns")
    up.start_polling(clean=True)
    log.info("Telegram bot started")
    up.idle()

if __name__ == "__main__":
    main()

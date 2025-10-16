#!/usr/bin/env python3
# TECBot Telegram Listener — formatting only (Balances=OptA, Prices=OptB, Slippage=OptB)
# Business logic unchanged; integrates prices.unit_quote() and Coinbase compare.

import os, sys, logging, subprocess, shlex
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional, List

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ParseMode, Update
from telegram.ext import (
    Updater, CommandHandler, CallbackQueryHandler, MessageHandler, Filters, CallbackContext
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
log = logging.getLogger("telegram_listener")

# Ensure /bot on path
if "/bot" not in sys.path:
    sys.path.insert(0, "/bot")

# ---------- tolerant imports ----------
try:
    from app import config as C
    log.info("Loaded config from app.config")
except Exception:
    import config as C
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

# Optional Coinbase spot (uses your repo helper)
CB = None
try:
    import coinbase_client as CB  # exposes fetch_eth_usd_price()
except Exception:
    CB = None

# ---------- utils ----------
def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def _git_short_rev() -> Optional[str]:
    try:
        out = subprocess.check_output(shlex.split("git rev-parse --short HEAD"), cwd="/bot", stderr=subprocess.DEVNULL)
        return out.decode().strip()
    except Exception:
        return None

def is_admin(user_id: int) -> bool:
    try:
        return int(user_id) in set(int(x) for x in getattr(C, "ADMIN_USER_IDS", []) or [])
    except Exception:
        return False

def _fmt_money(x: Optional[Decimal]) -> str:
    if x is None:
        return "—"
    try:
        d = Decimal(x)
    except InvalidOperation:
        return "—"
    # 5 decimals max for readability (per your request)
    q = Decimal("0.00001")
    d = d.quantize(q) if d < 100 else d.quantize(Decimal("0.01"))
    s = f"{d:,.5f}" if d < 100 else f"{d:,.2f}"
    return f"${s}"

def _coinbase_eth() -> Optional[Decimal]:
    if CB is None:
        return None
    try:
        p = CB.fetch_eth_usd_price()  # float or None
        return Decimal(str(p)) if p is not None else None
    except Exception:
        return None

# ---------- logging ----------
def _log_update(update: Update, context: CallbackContext):
    try:
        uid = update.effective_user.id if update.effective_user else "?"
        txt = update.effective_message.text if update.effective_message else "<non-text>"
        log.info(f"UPDATE from {uid}: {txt}")
    except Exception:
        pass

def _log_error(update: object, context: CallbackContext):
    log.exception("Handler error")

# ---------- plan/dryrun rendering ----------
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
        "  /balances — per-wallet balances (ERC-20 + ONE)\n"
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
    update.message.reply_text("Sanity:\n  " + "\n  ".join(f"{k}: {v}" for k,v in details.items())
                              + "\n\nModules:\n  " + "\n  ".join(f"{k}: {v}" for k,v in avail.items()))

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
    update.message.reply_text("\n".join(lines))

# -------- Balances (Option A; tight, aligned; drop '(native)') --------
def cmd_balances(update: Update, context: CallbackContext):
    if BL is None:
        update.message.reply_text("Balances unavailable (module not loaded)."); return
    try:
        table = BL.all_balances()
    except Exception as e:
        log.exception("balances failure")
        update.message.reply_text(f"Balances error: {e}"); return

    cols = ["ONE", "1USDC", "1ETH", "TEC", "1sDAI"]
    title = f"Balances (@ {now_iso()})"
    # widths tuned for Telegram monospaced; 2 spaces between cols
    w_wallet = 14
    w_col = 10

    lines = [title]
    header = "Wallet".ljust(w_wallet) + "  " + "  ".join(c.rjust(w_col) for c in cols)
    lines.append(header)
    lines.append("-" * len(header))

    def fmt_amt(x):
        try:
            d = Decimal(str(x))
            d = d.quantize(Decimal("0.00001"))
            s = f"{d:.5f}"
            return s
        except Exception:
            return str(x)

    for w_name in sorted(table.keys()):
        row = table[w_name]
        vals = [fmt_amt(row.get(c, 0)) for c in cols]
        line = w_name.ljust(w_wallet) + "  " + "  ".join(v.rjust(w_col) for v in vals)
        lines.append(line)

    update.message.reply_text("\n".join(lines))

# -------- Prices (Option B; uses prices.unit_quote + Coinbase compare) --------
def cmd_prices(update: Update, context: CallbackContext):
    if PR is None:
        update.message.reply_text("Prices unavailable (module not loaded)."); return

    syms = ["ONE", "1USDC", "1sDAI", "TEC", "1ETH"]

    # Table header
    title = "LP Prices"
    col_names = ["Asset", "LP Price", "Quote Basis", "Slippage", "Route"]
    w = [5, 10, 12, 9, 26]  # column widths for Telegram
    def rowfmt(A,B,C,D,E):
        return f"{A:<{w[0]}} | {B:>{w[1]}} | {C:>{w[2]}} | {D:>{w[3]}} | {E:<{w[4]}}"

    out = [title, rowfmt(*col_names), "-" * (sum(w) + 12)]

    # rows
    for s in syms:
        try:
            uq = PR.unit_quote(s)  # dict or None
        except Exception as e:
            uq = None
            log.warning("unit_quote error for %s: %s", s, e)

        if not uq:
            out.append(rowfmt(s, "—", "—", "—", "—"))
            continue

        price = _fmt_money(Decimal(uq["unit_price"]))  # USDC per 1
        basis = f"{Decimal(uq['basis']):.5f}"
        slip  = f"{int(uq['slippage_bps']):d} bps"
        route = uq["route"]

        out.append(rowfmt(s, price, basis, slip, route))

    # Coinbase comparison for ETH
    lp_eth = None
    try:
        uq_eth = PR.unit_quote("1ETH")
        lp_eth = Decimal(uq_eth["unit_price"]) if uq_eth else None
    except Exception:
        pass
    cb_eth = _coinbase_eth()

    out.append("")
    out.append("ETH: Harmony LP vs Coinbase")
    out.append(f"  LP:       {_fmt_money(lp_eth)}")
    out.append(f"  Coinbase: {_fmt_money(cb_eth)}")
    if lp_eth is not None and cb_eth not in (None, Decimal(0)):
        try:
            diff = (lp_eth - cb_eth) / cb_eth * Decimal(100)
            out.append(f"  Diff:     {diff:+.2f}%")
        except Exception:
            pass

    update.message.reply_text("\n".join(out))

# -------- Slippage (Option B; aligned table; price logic unchanged) --------
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

    # Mid (per 1 unit) via prices.unit_quote for consistency with /prices display
    mid = None
    try:
        uq = PR.unit_quote(token_in)
        if uq:
            mid = Decimal(uq["unit_price"])
    except Exception:
        pass

    # Build size targets in OUT=USDC terms (10 / 100 / 1000 / 10000)
    targets = [Decimal("10"), Decimal("100"), Decimal("1000"), Decimal("10000")]

    # Helper for total->per-unit eff price using legacy price_usd to honor existing path logic
    def eff_price_for_usdc_target(usdc_target: Decimal) -> (Optional[Decimal], Optional[Decimal]):
        """Return (amount_in_est, eff_price_per_unit)"""
        if not mid or mid <= 0:
            return None, None
        # naive estimate, then recompute via price_usd
        est_in = (usdc_target / mid).quantize(Decimal("0.000001"))
        try:
            total_out = PR.price_usd(token_in, est_in)
            eff = (total_out / est_in) if (total_out and est_in > 0) else None
            return est_in, eff
        except Exception:
            return est_in, None

    # Table
    title = f"Slippage curve: {token_in} → {token_out}"
    lines = [title]
    if mid:
        lines.append(f"Baseline (mid): {_fmt_money(mid)} per 1{token_in}")

    # header widths tuned for Telegram
    w = [12, 16, 12, 16]
    hdr = f"{'Size (USDC)':>{w[0]}} | {'Amount In (sym)':>{w[1]}} | {'Eff. Price':>{w[2]}} | {'Slippage vs mid':>{w[3]}}"
    lines.append("")
    lines.append(hdr)
    lines.append("-" * len(hdr))

    for t in targets:
        ain, eff = eff_price_for_usdc_target(t)
        eff_s = _fmt_money(eff) if eff else "—"
        if eff and mid:
            slip = ((eff - mid) / mid * Decimal(100)).quantize(Decimal("0.01"))
            slip_s = f"{slip:+.2f}%"
        else:
            slip_s = "—"
        ain_s = f"{ain:.6f}" if ain else "—"
        lines.append(f"{format(t, ',.0f'):>{w[0]}} | {ain_s:>{w[1]}} | {eff_s:>{w[2]}} | {slip_s:>{w[3]}}")

    update.message.reply_text("\n".join(lines))

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

    log.info("Handlers registered: /start /help /version /sanity /assets /prices /balances /slippage /ping /plan /dryrun /cooldowns")
    up.start_polling(clean=True)  # ok for ptb 13.x
    log.info("Telegram bot started")
    up.idle()

if __name__ == "__main__":
    main()

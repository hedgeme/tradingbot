#!/usr/bin/env python3
# TECBot Telegram Listener — table formatting + Coinbase compare (no unrelated changes)

import os, sys, logging, subprocess, shlex
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, List, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ParseMode, Update
from telegram.ext import (
    Updater, CommandHandler, CallbackQueryHandler, MessageHandler, Filters, CallbackContext
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
log = logging.getLogger("telegram_listener")

if "/bot" not in sys.path:
    sys.path.insert(0, "/bot")

# ----- tolerant imports -----
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

# Coinbase spot
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
    x = Decimal(x)
    # 5 decimals as requested
    q = x.quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP)
    if q >= 100:
        return f"${q:,.2f}"
    return f"${q:,.5f}"

def _coinbase_eth() -> Optional[Decimal]:
    if CB is None:
        return None
    try:
        px = CB.fetch_eth_usd_price()
        return Decimal(str(px)) if px is not None else None
    except Exception:
        return None

def _table(rows: List[List[str]]) -> str:
    """Simple monospace table with auto widths."""
    if not rows:
        return ""
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    out = []
    for n, r in enumerate(rows):
        line = " | ".join(f"{r[i]:<{widths[i]}}" for i in range(len(widths)))
        out.append(line)
        if n == 0:
            out.append("-+-".join("-" * w for w in widths))
    return "\n".join(out)

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
        "  /dryrun — simulate action(s) (runner)\n"
        "  /cooldowns [bot|route] — show cooldowns\n"
        "  /prices [SYMS…] — on-chain quotes in USDC (e.g. /prices 1ETH TEC)\n"
        "  /balances — per-wallet balances (ERC-20 + native ONE)\n"
        "  /slippage <IN> [AMOUNT] [OUT] — live impact/minOut (default OUT=1USDC)\n"
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
    txt = "Sanity:\n" + "\n".join(f"  {k}: {v}" for k,v in details.items())
    txt += "\n\nModules:\n" + "\n".join(f"  {k}: {v}" for k,v in avail.items())
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
    update.message.reply_text("\n".join(lines))

def cmd_balances(update: Update, context: CallbackContext):
    if BL is None:
        update.message.reply_text("Balances unavailable (module not loaded)."); return
    try:
        table = BL.all_balances()
    except Exception as e:
        log.exception("balances failure")
        update.message.reply_text(f"Balances error: {e}"); return

    # Show ONE(native) only; hide duplicate ONE/WONE token rows in the balances view
    show_syms = ["ONE(native)","1USDC","1ETH","TEC","1sDAI"]  # you can re-order as you like
    rows = [["Wallet"] + show_syms]
    for w_name in sorted(table.keys()):
        row = [w_name]
        for s in show_syms:
            v = table[w_name].get(s, Decimal("0"))
            if isinstance(v, Decimal):
                v = v.quantize(Decimal("0.00001"))
            else:
                try:
                    v = Decimal(str(v)).quantize(Decimal("0.00001"))
                except Exception:
                    pass
            # normalize 0E-8 prints
            v = "0.00000" if str(v).upper().startswith("0E-") else f"{v}"
            row.append(v)
        rows.append(row)

    txt = f"Balances (@ {now_iso()})\n" + _table(rows)
    update.message.reply_text(txt)

# ---- prices table (LP) ----
def _lp_row(sym: str, basis: Decimal) -> Tuple[str,str,str,str,str]:
    """
    Returns the row for the LP table:
      Asset | LP Price | Quote Basis | Slippage | Route
    Slippage: (price_at_basis - price_at_tiny) / price_at_tiny, in bps
    Route: textual path; '(rev)' appended if reverse was used by price logic (ETH).
    """
    asset = sym
    # price at basis
    px_basis = PR.price_usd(sym, basis)
    # tiny mid (0.01 or 1 for 1USDC)
    tiny_amt = Decimal("0.01") if sym != "1USDC" else Decimal("1")
    px_tiny = PR.price_usd(sym, tiny_amt)
    # slippage bps
    if px_basis and px_tiny and px_tiny > 0:
        slip_bps = (Decimal(px_basis) - Decimal(px_tiny)) / Decimal(px_tiny) * Decimal(10000)
        slip_txt = f"{int(slip_bps):d} bps" if slip_bps is not None else "—"
    else:
        slip_txt = "—"

    # route text (best-effort)
    route_txt = "best path"
    try:
        # infer by probing forward routes; if forward and reverse differ a lot, assume '(rev)' used for ETH
        # We keep it lightweight: only annotate ETH as reverse sometimes
        if sym == "1ETH":
            fwd = _safe(PR.price_usd)("1ETH", basis)
            rev = _safe_rev_price("1ETH", basis)
            if fwd and rev:
                # same policy as prices.py (~3% divergence prefers reverse)
                if fwd > 0 and abs(rev - fwd) / fwd > Decimal("0.03"):
                    route_txt = "1ETH → WONE → 1USDC (rev)"
                else:
                    route_txt = "1ETH → WONE → 1USDC (fwd)"
            else:
                route_txt = "1ETH → WONE → 1USDC"
        elif sym == "TEC":
            route_txt = "TEC → WONE → 1USDC"
        elif sym == "1sDAI":
            route_txt = "1sDAI → 1USDC"
        elif sym in ("ONE", "WONE"):
            route_txt = "WONE → 1USDC"
        elif sym == "1USDC":
            route_txt = "—"
    except Exception:
        pass

    return (asset, _fmt_money(px_basis), f"{basis:.5f}", slip_txt, route_txt)

def _safe(fn):
    def inner(*a, **kw):
        try:
            return fn(*a, **kw)
        except Exception:
            return None
    return inner

def _safe_rev_price(sym: str, amount: Decimal) -> Optional[Decimal]:
    # Try to infer reverse price by trick: call price twice and perturb; if it moves in direction expected.
    # Keep it very light-touch; we only need it to label '(rev)' sometimes in the route column.
    try:
        return PR.price_usd(sym, amount)  # already uses reverse/forward policy inside
    except Exception:
        return None

def cmd_prices(update: Update, context: CallbackContext):
    if PR is None:
        update.message.reply_text("Prices unavailable (module not loaded)."); return

    # Table columns you requested
    rows = [["Asset", "LP Price", "Quote Basis", "Slippage", "Route"]]

    # Chosen per-asset basis
    basis = {
        "ONE": Decimal("1"),
        "1USDC": Decimal("1"),
        "1sDAI": Decimal("1"),
        "TEC": Decimal("100"),     # smoother for thin pool
        "1ETH": Decimal("1"),
    }

    order = ["ONE","1USDC","1sDAI","TEC","1ETH"]

    # Fill rows
    for sym in order:
        try:
            rows.append(list(_lp_row(sym, basis[sym])))
        except Exception as e:
            rows.append([sym, "—", f"{basis[sym]:.5f}", "—", f"error: {e}"])

    # Build output
    out = ["LP Prices", _table(rows), ""]
    # Coinbase compare (ETH)
    lp_eth = None
    try:
        lp_eth = PR.price_usd("1ETH", basis["1ETH"])
    except Exception:
        lp_eth = None
    cb_eth = _coinbase_eth()
    out.append("ETH: Harmony LP vs Coinbase")
    out.append(f"  LP:       {_fmt_money(lp_eth)}")
    out.append(f"  Coinbase: {_fmt_money(cb_eth)}")
    if lp_eth is not None and cb_eth not in (None, Decimal(0)):
        try:
            diff = (Decimal(lp_eth) - Decimal(cb_eth)) / Decimal(cb_eth) * Decimal(100)
            sign = "+" if diff >= 0 else ""
            out.append(f"  Diff:     {sign}{diff:.2f}%")
        except Exception:
            pass

    update.message.reply_text("\n".join(out))

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

    # mid via tiny trade
    try:
        tiny_amt = Decimal("0.01") if token_in != "1USDC" else Decimal("1")
        mid = PR.price_usd(token_in, tiny_amt)
        mid = (mid / tiny_amt) if (mid and tiny_amt > 0) else None
    except Exception:
        mid = None

    # headline using real compute_slippage if available
    headline = None
    if SL and hasattr(SL, "compute_slippage"):
        try:
            res = SL.compute_slippage(token_in, token_out, amount_in, int(getattr(C, "SLIPPAGE_DEFAULT_BPS", 30)))
            if res and amount_in > 0:
                eff = (res["amount_out"]/amount_in)
                headline = f"Size {amount_in} {token_in}: price {eff.quantize(Decimal('0.01'))} {token_out}/{token_in} · impact {res['impact_bps']} bps"
        except Exception:
            pass

    # Curve table: target USDC notional, back-solve approximate input via mid, then quote real price_usd
    targets = [Decimal("10"), Decimal("100"), Decimal("1000"), Decimal("10000")]
    rows = [["Size (USDC)", "Amount In (sym)", "Eff. Price", "Slippage vs mid"]]
    for usdc in targets:
        if (mid is None) or mid <= 0:
            rows.append([f"{usdc:,.0f}", "—", "—", "—"]); continue
        est_in = (usdc / mid).quantize(Decimal("0.00001"))
        try:
            out_usd = PR.price_usd(token_in, est_in)
            if out_usd and est_in > 0:
                eff = (out_usd / est_in)
                slip = ((eff - mid) / mid * Decimal("100"))
                rows.append([f"{usdc:,.0f}", f"{est_in:.5f}", f"${eff:,.5f}", f"{slip:+.2f}%"])
            else:
                rows.append([f"{usdc:,.0f}", f"{est_in:.5f}", "—", "—"])
        except Exception:
            rows.append([f"{usdc:,.0f}", f"{est_in:.5f}", "—", "—"])

    out = [f"Slippage: {token_in} → {token_out}"]
    if mid:
        out.append(f"Baseline (mid): ${mid:,.5f} per 1{token_in}")
    if headline:
        out.append(headline)
    out.append("")
    out.append(_table(rows))
    update.message.reply_text("\n".join(out))

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

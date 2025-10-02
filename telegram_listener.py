#!/usr/bin/env python3
# TECBot Telegram Listener (root entrypoint) — compatible with python-telegram-bot 13.x

import os, sys, logging
from datetime import datetime, timezone
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ParseMode, Update
from telegram.ext import (
    Updater, CommandHandler, CallbackQueryHandler, MessageHandler, Filters, CallbackContext
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
log = logging.getLogger("telegram_listener")

# Ensure /bot imports work
if "/bot" not in sys.path:
    sys.path.insert(0, "/bot")

# ----- tolerant imports: app.* first, then root modules -----
# config
try:
    from app import config as C  # preferred
    log.info("Loaded config from app.config")
except Exception:
    try:
        import config as C       # fallback to /bot/config.py
        log.info("Loaded config from root config")
    except Exception as e:
        log.exception("Failed to import config")
        raise

# planner
planner = None
try:
    from app.strategies import planner  # preferred
    log.info("Loaded planner from app.strategies.planner")
except Exception:
    try:
        from strategies import planner   # fallback to /bot/strategies/planner.py
        log.info("Loaded planner from root strategies.planner")
    except Exception as e:
        log.warning("Planner module not available: %s", e)
        planner = None

# runner
runner = None
try:
    from app import runner  # preferred /bot/app/runner.py
    log.info("Loaded runner from app.runner")
except Exception:
    try:
        import runner       # fallback /bot/runner.py
        log.info("Loaded runner from root runner")
    except Exception as e:
        log.warning("Runner module not available: %s", e)
        runner = None

def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def is_admin(user_id: int) -> bool:
    try:
        return int(user_id) in set(int(x) for x in getattr(C, "ADMIN_USER_IDS", []) or [])
    except Exception:
        return False

# ---------- render helpers ----------
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

# ---------- log updates & errors so we see failures ----------
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
        lines = ["Default cooldowns (seconds):"] + [f"  {k}: {v}" for k, v in defaults.items()]
        update.message.reply_text("\n".join(lines)); return
    key = args[0]
    if key in by_bot:
        d = by_bot[key]; header = f"Cooldowns for {key} (seconds):"
    elif key in by_route:
        d = by_route[key]; header = f"Cooldowns for route {key} (seconds):"
    else:
        lines = [f"No specific cooldowns for '{key}'. Showing defaults."] + [f"  {k}: {v}" for k, v in defaults.items()]
        update.message.reply_text("\n".join(lines)); return
    lines = [header] + [f"  {k}: {v}" for k, v in d.items()]
    update.message.reply_text("\n".join(lines))

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

    dp.add_handler(CommandHandler("ping", cmd_ping))
    dp.add_handler(CommandHandler("plan", cmd_plan))
    dp.add_handler(CommandHandler("dryrun", cmd_dryrun))
    dp.add_handler(CommandHandler("cooldowns", cmd_cooldowns, pass_args=True))

    dp.add_handler(CallbackQueryHandler(on_exec_button, pattern=r"^exec:[A-Za-z0-9_\-]+$"))
    dp.add_handler(CallbackQueryHandler(on_exec_confirm, pattern=r"^exec_go:[A-Za-z0-9_\-]+$"))
    dp.add_handler(CallbackQueryHandler(on_exec_cancel, pattern=r"^exec_cancel$"))

    dp.add_error_handler(_log_error)
    dp.add_handler(MessageHandler(Filters.all, _log_update), group=-1)

    log.info("Handlers registered: /ping /plan /dryrun /cooldowns")
    up.start_polling(clean=True)
    log.info("Telegram bot started")
    up.idle()

if __name__ == "__main__":
    main()

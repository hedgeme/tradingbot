#!/usr/bin/env python3
# telegram_listener.py — TECBot Telegram interface (v0.2)
# Compatible with python-telegram-bot==13.15

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, ParseMode
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, CallbackContext, Filters

# --- Local modules ---
# We only *import if present* to avoid crashes during rollout.
# These modules are part of your repo; if any is missing, handlers will degrade gracefully.
try:
    from app import config as C
except Exception:
    # Minimal shim if config import fails (shouldn't happen in production)
    class _Shim:
        ADMIN_USER_IDS = []
        DRYRUN_ENABLED = True
        COOLDOWNS_DEFAULTS = {"price_refresh": 15, "trade_retry": 30, "alert_throttle": 60}
        COOLDOWNS_BY_BOT = {}
        COOLDOWNS_BY_ROUTE = {}
    C = _Shim()

# Optional strategy & runner interfaces
try:
    # Expected interface:
    #   planner.build_plan_snapshot() -> Dict[str, List[Action]]
    #   where Action has attributes: action_id, bot, route_human, amount_in_text, reason, limits_text, priority
    from app.strategies import planner
except Exception:
    planner = None

try:
    # Expected interface:
    #   runner.build_dryrun() -> List[DryRunResult]
    #   DryRunResult has: action_id, bot, path_text, amount_in_text, quote_out_text,
    #   impact_bps, slippage_bps, min_out_text, gas_estimate, allowance_ok, nonce,
    #   tx_preview_text, confirm_blob (opaque payload to execute)
    from app import runner
except Exception:
    runner = None

# Optional existing command helpers (if you already have them)
try:
    from app.preflight import run_preflight
except Exception:
    run_preflight = None

try:
    from app.price_feed import prices_snapshot_table  # Optional pretty printer you may have
except Exception:
    prices_snapshot_table = None

try:
    from app.alert import send_alert  # not used here but kept for parity
except Exception:
    send_alert = None

# --- Constants / State ---
STATE_DIR = Path("/bot/state")
STATE_DIR.mkdir(parents=True, exist_ok=True)
EXEC_LOCKS_PATH = STATE_DIR / "exec_locks.json"

BOT_VERSION = os.getenv("TECBOT_VERSION", "v0.1.0-ops")

# --- Utilities ---

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def is_admin(user_id: int) -> bool:
    try:
        return int(user_id) in set(int(x) for x in getattr(C, "ADMIN_USER_IDS", []) or [])
    except Exception:
        return False

def _read_exec_locks() -> Dict[str, float]:
    if EXEC_LOCKS_PATH.exists():
        try:
            return json.loads(EXEC_LOCKS_PATH.read_text())
        except Exception:
            return {}
    return {}

def _write_exec_locks(d: Dict[str, float]) -> None:
    tmp = EXEC_LOCKS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(d))
    tmp.replace(EXEC_LOCKS_PATH)

def _lock_once(key: str, ttl_sec: int = 180) -> bool:
    """Return True if we acquired the lock; False if key is already locked within TTL."""
    locks = _read_exec_locks()
    now = time.time()
    # purge old
    locks = {k: v for k, v in locks.items() if now - v < ttl_sec}
    if key in locks:
        _write_exec_locks(locks)
        return False
    locks[key] = now
    _write_exec_locks(locks)
    return True

# --- Render helpers ---

def render_plan(actions_by_bot: Dict[str, List[object]]) -> str:
    if not actions_by_bot or not any(actions_by_bot.values()):
        return f"Plan (preview @ {now_iso()})\nNo actions proposed."

    lines: List[str] = [f"Plan (preview @ {now_iso()})"]
    for bot, actions in actions_by_bot.items():
        if not actions:
            continue
        lines.append(f"\nBot: {bot}")
        for a in actions:
            # Expect attributes; fall back defensively
            action_id = getattr(a, "action_id", "NA")
            prio = getattr(a, "priority", "-")
            route_human = getattr(a, "route_human", "(route)")
            amount_in_text = getattr(a, "amount_in_text", "(amount)")
            reason = getattr(a, "reason", "")
            limits = getattr(a, "limits_text", "")
            lines.append(f"- Action #{action_id}  PRIORITY:{prio}\n"
                         f"  Route : {route_human}\n"
                         f"  Size  : {amount_in_text}\n"
                         f"  Rationale:\n    • {reason if reason else 'n/a'}\n"
                         f"  Limits:\n    • {limits if limits else 'n/a'}")
    return "\n".join(lines)

def render_dryrun(results: List[object]) -> str:
    if not results:
        return f"Dry-run (@ {now_iso()}): no executable actions."
    lines: List[str] = [f"Dry-run (tick @ {now_iso()})\n"]
    for r in results:
        action_id = getattr(r, "action_id", "NA")
        bot = getattr(r, "bot", "NA")
        path_text = getattr(r, "path_text", "(path)")
        amount_in_text = getattr(r, "amount_in_text", "(amount)")
        quote_out_text = getattr(r, "quote_out_text", "(quote)")
        impact_bps = getattr(r, "impact_bps", None)
        slippage_bps = getattr(r, "slippage_bps", None)
        min_out_text = getattr(r, "min_out_text", "(minOut)")
        gas_est = getattr(r, "gas_estimate", "—")
        allowance_ok = getattr(r, "allowance_ok", False)
        nonce = getattr(r, "nonce", "—")
        tx_preview = getattr(r, "tx_preview_text", "(tx preview)")

        lines.append(
            f"Action #{action_id} — {bot}\n"
            f"Path     : {path_text}\n"
            f"AmountIn : {amount_in_text}\n"
            f"QuoteOut : {quote_out_text}"
        )
        if impact_bps is not None:
            lines.append(f"Impact   : {impact_bps:.2f} bps")
        if slippage_bps is not None:
            lines.append(f"Slippage : {slippage_bps} bps → minOut {min_out_text}")
        else:
            lines.append(f"minOut   : {min_out_text}")
        lines.append(
            f"Gas Est  : {gas_est}\n"
            f"Allowance: {'OK' if allowance_ok else 'NEEDED'}\n"
            f"Nonce    : {nonce}\n"
            f"Would send:\n{tx_preview}\n"
        )
    return "\n".join(lines).strip()

# --- Command handlers ---

def cmd_ping(update: Update, context: CallbackContext) -> None:
    ip_path = Path("/bot/db/public_ip.txt")
    ip_txt = ip_path.read_text().strip() if ip_path.exists() else "unknown"
    update.message.reply_text(f"pong · IP: {ip_txt} · {BOT_VERSION}")

def cmd_plan(update: Update, context: CallbackContext) -> None:
    if planner is None:
        update.message.reply_text(
            "Plan error: planner module not available. Ensure app/strategies/planner.py exposes build_plan_snapshot()."
        )
        return
    try:
        snapshot = planner.build_plan_snapshot()
    except Exception as e:
        update.message.reply_text(f"Plan error: {e}")
        return
    update.message.reply_text(render_plan(snapshot))

def cmd_dryrun(update: Update, context: CallbackContext) -> None:
    if not getattr(C, "DRYRUN_ENABLED", True):
        update.message.reply_text(
            "Dry-run is currently disabled by config. Set DRYRUN_ENABLED=True in app/config.py."
        )
        return
    if runner is None or not hasattr(runner, "build_dryrun"):
        update.message.reply_text(
            "Dry-run unavailable: runner.build_dryrun() not found. Implement it to simulate execution."
        )
        return
    try:
        results = runner.build_dryrun()
    except Exception as e:
        update.message.reply_text(f"Dry-run error: {e}")
        return

    if not results:
        update.message.reply_text("Dry-run: no executable actions.")
        return

    # Render summary
    text = render_dryrun(results)

    # Inline buttons: one Execute per action
    keyboard_rows = []
    for r in results:
        action_id = getattr(r, "action_id", None)
        if not action_id:
            continue
        keyboard_rows.append([InlineKeyboardButton(f"▶️ Execute {action_id}", callback_data=f"exec:{action_id}")])
    keyboard_rows.append([InlineKeyboardButton("❌ Cancel", callback_data="exec_cancel")])

    update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard_rows), parse_mode=ParseMode.HTML)

def cmd_cooldowns(update: Update, context: CallbackContext) -> None:
    args = context.args
    defaults = getattr(C, "COOLDOWNS_DEFAULTS", {"price_refresh": 15, "trade_retry": 30, "alert_throttle": 60})
    by_bot = getattr(C, "COOLDOWNS_BY_BOT", {})
    by_route = getattr(C, "COOLDOWNS_BY_ROUTE", {})

    if not args:
        lines = ["Default cooldowns (seconds):"]
        for k, v in defaults.items():
            lines.append(f"  {k}: {v}")
        update.message.reply_text("\n".join(lines))
        return

    key = args[0]
    if key in by_bot:
        d = by_bot[key]
        header = f"Cooldowns for {key} (seconds):"
    elif key in by_route:
        d = by_route[key]
        header = f"Cooldowns for route {key} (seconds):"
    else:
        lines = [f"No specific cooldowns for '{key}'. Showing defaults."]
        for k, v in defaults.items():
            lines.append(f"  {k}: {v}")
        update.message.reply_text("\n".join(lines))
        return

    lines = [header] + [f"  {k}: {v}" for k, v in d.items()]
    update.message.reply_text("\n".join(lines))

# --- Callback query handlers for Execute flow ---

def on_exec_button(update: Update, context: CallbackContext) -> None:
    """First press: show confirmation for a specific action."""
    query = update.callback_query
    data = query.data or ""
    if not data.startswith("exec:"):
        query.answer()
        return

    if not is_admin(query.from_user.id):
        query.answer("Not authorized.", show_alert=True)
        return

    action_id = data.split(":", 1)[1]
    # idempotency pre-lock (prevent spam after confirm, too)
    lock_key = f"preconfirm:{action_id}"
    if not _lock_once(lock_key, ttl_sec=180):
        query.answer("Already pending.", show_alert=False)
        return

    text = f"Confirm execution: Action #{action_id}\nAre you sure?"
    keyboard = [[
        InlineKeyboardButton("✅ Confirm", callback_data=f"exec_go:{action_id}"),
        InlineKeyboardButton("❌ Abort", callback_data="exec_cancel"),
    ]]
    query.edit_message_text(text)
    query.edit_message_reply_markup(InlineKeyboardMarkup(keyboard))
    query.answer()

def on_exec_confirm(update: Update, context: CallbackContext) -> None:
    """Second press: actually execute the prepared tx using runner.execute_action(action_id)."""
    query = update.callback_query
    data = query.data or ""
    if not data.startswith("exec_go:"):
        query.answer()
        return

    if not is_admin(query.from_user.id):
        query.answer("Not authorized.", show_alert=True)
        return

    if runner is None or not hasattr(runner, "execute_action"):
        query.answer("Execution backend missing.", show_alert=True)
        return

    action_id = data.split(":", 1)[1]

    # Idempotency lock: prevent double-send
    lock_key = f"exec:{action_id}"
    if not _lock_once(lock_key, ttl_sec=180):
        query.answer("Already processed.", show_alert=False)
        return

    try:
        # Execute with exactly the params prepared by dry-run (runner should cache/lookup by action_id)
        txr = runner.execute_action(action_id)
        # txr should include tx hash, filled amounts, gas, explorer URL, etc.
        tx_hash = getattr(txr, "tx_hash", "0x")
        filled_text = getattr(txr, "filled_text", "")
        gas_used = getattr(txr, "gas_used", "—")
        explorer = getattr(txr, "explorer_url", "")
        msg = (
            f"✅ Executed {action_id}\n"
            f"{filled_text}\n"
            f"Gas used: {gas_used}\n"
            f"Tx: {tx_hash}\n"
            f"{explorer}"
        ).strip()
        query.edit_message_text(msg)
        query.answer("Executed.")
    except Exception as e:
        query.edit_message_text(f"❌ Execution failed for {action_id}\n{e}")
        query.answer("Failed.", show_alert=True)

def on_exec_cancel(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    query.edit_message_text("Canceled. No transaction sent.")
    query.answer()

# --- Bootstrapping (only used if this module is run directly) ---

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or getattr(C, "TELEGRAM_BOT_TOKEN", None)
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN not set in environment or app/config.py")

    updater = Updater(token=token, use_context=True)
    dp = updater.dispatcher

    # Core commands
    dp.add_handler(CommandHandler("ping", cmd_ping))
    dp.add_handler(CommandHandler("plan", cmd_plan))
    dp.add_handler(CommandHandler("dryrun", cmd_dryrun))
    dp.add_handler(CommandHandler("cooldowns", cmd_cooldowns, pass_args=True))

    # Keep your existing handlers for /balances, /sanity, /preflight, /prices here.
    # Example stubs (uncomment and point to your existing implementations):
    # dp.add_handler(CommandHandler("preflight", lambda u, c: u.message.reply_text(run_preflight() if run_preflight else "preflight not wired")))

    # Callback buttons
    dp.add_handler(CallbackQueryHandler(on_exec_button, pattern=r"^exec:[A-Za-z0-9_\-]+$"))
    dp.add_handler(CallbackQueryHandler(on_exec_confirm, pattern=r"^exec_go:[A-Za-z0-9_\-]+$"))
    dp.add_handler(CallbackQueryHandler(on_exec_cancel, pattern=r"^exec_cancel$"))

    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()

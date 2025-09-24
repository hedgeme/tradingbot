# /bot/app/telegram_listener.py
# TECBot Telegram Listener (PTB 13.x)
# - /sanity fixed & robust
# - /dryrun -> "Execute now" per strategy (forced, admin-only, 60s TTL, idempotent)
# - Commands: start, help, ping, balances, sanity, prices, plan, dryrun, disable, enable, cooldowns, version, report

import os
import sys
import time
import logging
import hashlib
from pathlib import Path
from decimal import Decimal

from telegram import (
    ParseMode,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Updater,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    Filters,
)

# -----------------------------
# Config
# -----------------------------
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("BOT_TOKEN") or ""
if not BOT_TOKEN:
    print("ERROR: TELEGRAM_BOT_TOKEN not set in environment.", file=sys.stderr)

# Set your admin Telegram user IDs here
ADMIN_CHAT_IDS = {
    1539031664,  # Esteban
    # add more IDs if needed
}

# Version/Checksum settings
REPO_ROOT = Path("/bot")  # adjust if your repo path differs on server
VERSION_FILE = REPO_ROOT / "VERSION"
CHECKSUM_FILES = [
    REPO_ROOT / "app" / "telegram_listener.py",
    REPO_ROOT / "app" / "price_feed.py",
    REPO_ROOT / "app" / "wallet.py",
    REPO_ROOT / "app" / "preflight.py",
]

# Dryrun plan cache settings
PLAN_TTL_SECONDS = 60  # plan "Execute now" button validity
PLAN_CACHE = {}        # {strategy: {"plan": dict, "ts": int, "used": bool, "plan_id": str}}

# Logger
logger = logging.getLogger("telegram_listener")
logger.setLevel(logging.INFO)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s:%(name)s:%(message)s"))
logger.addHandler(_handler)

# -----------------------------
# Helpers: module access
# -----------------------------
def _is_admin(update_or_query) -> bool:
    chat = getattr(update_or_query, "effective_chat", None)
    if chat and chat.id in ADMIN_CHAT_IDS:
        return True
    # for callback queries
    user = getattr(getattr(update_or_query, "callback_query", None), "from_user", None)
    if user and user.id in ADMIN_CHAT_IDS:
        return True
    return False

def _mk_plan_id(plan_payload: dict) -> str:
    base = repr(sorted(plan_payload.items())).encode()
    return hashlib.sha256(base).hexdigest()[:8]

def _import_or_none(modpath, attr=None):
    try:
        mod = __import__(modpath, fromlist=[attr] if attr else [])
        return getattr(mod, attr) if attr else mod
    except Exception as e:
        logger.debug("Import failed %s.%s: %s", modpath, attr or "", e)
        return None

# Optional modules
preflight_run_sanity = _import_or_none("app.preflight", "run_sanity")
strategy_manager = _import_or_none("app.strategy_manager")
wallet_mod = _import_or_none("app.wallet")
price_feed = _import_or_none("app.price_feed")

def get_strategies():
    # Expect strategy_manager.list_strategies() or .get_strategies()
    if strategy_manager:
        for name in ("list_strategies", "get_strategies"):
            fn = getattr(strategy_manager, name, None)
            if callable(fn):
                try:
                    return fn()
                except Exception as e:
                    logger.warning("strategy_manager.%s error: %s", name, e)
    return ["sdai-arb", "eth-arb", "tec-rebal", "usdc-hedge"]  # fallback list; adjust to your setup

# -----------------------------
# Commands
# -----------------------------
def cmd_start(update, context):
    text = (
        "TECBot online.\n"
        "Type /help for available commands."
    )
    context.bot.send_message(chat_id=update.effective_chat.id, text=text)

def cmd_help(update, context):
    text = (
        "*TECBot Commands*\n"
        "/ping - Health check\n"
        "/balances - Show wallet balances\n"
        "/sanity - Quick balance sanity check\n"
        "/prices - Show token prices\n"
        "/plan [all|<strategy>] - Preview next-tick trades (no execution)\n"
        "/dryrun [all|<strategy>] - Simulate next tick; tap button to execute\n"
        "/disable <strategy>|all - Disable strategy/all\n"
        "/enable <strategy>|all - Enable strategy/all\n"
        "/cooldowns - Show or set cooldowns (set/off)\n"
        "/version - Show bot version and config checksum\n"
        "/report - Show per-bot and total PnL summary"
    )
    context.bot.send_message(chat_id=update.effective_chat.id, text=text, parse_mode=ParseMode.MARKDOWN)

def cmd_ping(update, context):
    context.bot.send_message(chat_id=update.effective_chat.id, text="pong")

def cmd_balances(update, context):
    chat_id = update.effective_chat.id
    try:
        # Expect a helper in wallet module
        fn = None
        for name in ("get_all_wallet_balances", "get_balances", "balances"):
            cand = getattr(wallet_mod, name, None) if wallet_mod else None
            if callable(cand):
                fn = cand
                break
        if not fn:
            raise RuntimeError("wallet.get_all_wallet_balances() not found")

        data = fn()  # expected: {wallet: {SYMBOL: Decimal|float|str, ...}, ...}
        lines = ["*Balances*"]
        for wallet, assets in data.items():
            lines.append(f"\n*{wallet}*")
            for sym, amt in assets.items():
                lines.append(f"  {sym}  {amt}")
        msg = "\n".join(lines)
        context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        context.bot.send_message(chat_id=chat_id, text=f"❌ /balances failed: {e}")
        logger.exception("/balances error")

def cmd_sanity(update, context):
    """Quick balance sanity (thresholds)"""
    chat_id = update.effective_chat.id
    try:
        context.bot.send_message(chat_id=chat_id, text="Running sanity checks…")
        if not callable(preflight_run_sanity):
            raise RuntimeError("preflight.run_sanity() not found")
        result = preflight_run_sanity()
        ok = "✅ All good" if result.get("ok") else "⚠️ Attention needed"
        lines = [f"*Sanity Check* {ok}", ""]
        for item in result.get("items", []):
            status_icon = "✅" if item.get("status") == "ok" else "❌"
            lines.append(f"{status_icon} *{item['wallet']}*")
            for c in item.get("checks", []):
                ico = "✅" if c.get("ok") else "❌"
                lines.append(f"  {ico} {c['asset']}: have {c['have']} | need {c['need']}")
            lines.append("")
        msg = "\n".join(lines).strip() or "No data."
        context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)
        logger.info("/sanity replied")
    except Exception as e:
        context.bot.send_message(chat_id=chat_id, text=f"❌ /sanity failed: {e}")
        logger.exception("/sanity error")

def cmd_prices(update, context):
    chat_id = update.effective_chat.id
    try:
        # Expect price_feed.get_prices() -> dict or list
        fn = getattr(price_feed, "get_prices", None) if price_feed else None
        if not callable(fn):
            raise RuntimeError("price_feed.get_prices() not found")
        prices = fn()
        lines = ["*Prices*"]
        if isinstance(prices, dict):
            for k, v in prices.items():
                lines.append(f"{k}: {v}")
        else:
            for row in prices:
                lines.append(str(row))
        context.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        context.bot.send_message(chat_id=chat_id, text=f"❌ /prices failed: {e}")
        logger.exception("/prices error")

def cmd_plan(update, context):
    """Preview intentions (no execution)."""
    args = [a.lower() for a in context.args]
    target = args[0] if args else "all"
    strategies = get_strategies() if target == "all" else [target]

    blocks = []
    for strat in strategies:
        try:
            # Expect strategy_manager.preview() or simulate_tick() read-only
            sim_fn = getattr(strategy_manager, "simulate_tick", None) if strategy_manager else None
            if not callable(sim_fn):
                raise RuntimeError("strategy_manager.simulate_tick() not found")
            sim = sim_fn(strat)
            if sim.get("would_broadcast"):
                blocks.append(
                    f"[{strat}] READY — {sim.get('action','') or 'would trade'} "
                    f"(edge {sim.get('edge','?')}, slip {sim.get('slippage_limit','?')})"
                )
            else:
                reason = sim.get("reason", "not ready")
                blocks.append(f"[{strat}] NOT READY — {reason}")
        except Exception as e:
            blocks.append(f"[{strat}] ❌ plan error: {e}")
            logger.exception("/plan error for %s", strat)

    context.bot.send_message(chat_id=update.effective_chat.id, text="\n".join(blocks) or "No strategies found.")

def _render_dryrun_block(strat: str, sim: dict, plan_id: str, ts: int):
    age = int(time.time()) - ts
    if sim.get("would_broadcast"):
        text = (
            f"[{strat}] ✓ DRYRUN OK\n"
            f"• Sim: {sim.get('size','?')} → {sim.get('est_out','?')} • Slip {sim.get('slippage_limit','?')} • Gas ~{sim.get('gas_est','?')}\n"
            f"• Would broadcast: YES\n"
            f"• plan_id: {plan_id} (valid {max(0, PLAN_TTL_SECONDS-age)}s)"
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"Execute now ({strat})", callback_data=f"exec:{strat}:{plan_id}")
        ]])
    else:
        text = (
            f"[{strat}] ✗ DRYRUN FAIL\n"
            f"• Reason: {sim.get('reason','unknown')}\n"
            f"• Would broadcast: NO"
        )
        kb = None
    return text, kb

def cmd_dryrun(update, context):
    """Full simulation; renders per-strategy 'Execute now' buttons with 60s TTL."""
    args = [a.lower() for a in context.args]
    target = args[0] if args else "all"
    strategies = get_strategies() if target == "all" else [target]

    sim_fn = getattr(strategy_manager, "simulate_tick", None) if strategy_manager else None
    if not callable(sim_fn):
        context.bot.send_message(chat_id=update.effective_chat.id, text="❌ simulate_tick() not available in strategy_manager.")
        return

    blocks, buttons = [], []
    now = int(time.time())

    for strat in strategies:
        try:
            sim = sim_fn(strat)
            plan_payload = {
                "strategy": strat,
                "size": str(sim.get("size","")),
                "est_out": str(sim.get("est_out","")),
                "slippage_limit": str(sim.get("slippage_limit","")),
                "route": str(sim.get("route","")),
            }
            plan_id = _mk_plan_id(plan_payload)
            PLAN_CACHE[strat] = {"plan": plan_payload, "ts": now, "used": False, "plan_id": plan_id}
            text, kb = _render_dryrun_block(strat, sim, plan_id, now)
            blocks.append(text)
            if kb and kb.inline_keyboard:
                buttons.extend(kb.inline_keyboard)  # one row per strategy
        except Exception as e:
            blocks.append(f"[{strat}] ❌ Dry-run error: {e}")
            logger.exception("/dryrun error for %s", strat)

    reply_markup = InlineKeyboardMarkup(buttons) if buttons else None
    context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="\n\n".join(blocks) or "No strategies found.",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

def cb_execute_now(update, context):
    """Callback for per-strategy forced execution from a fresh dryrun plan."""
    q = update.callback_query
    try:
        if not _is_admin(update):
            q.answer("Admin only.", show_alert=True)
            return

        data = q.data  # format: exec:<strategy>:<plan_id>
        try:
            _, strat, plan_id = data.split(":", 2)
        except Exception:
            q.answer("Invalid button payload.", show_alert=True)
            return

        entry = PLAN_CACHE.get(strat)
        now = int(time.time())

        if not entry or entry.get("plan_id") != plan_id:
            q.answer("Plan not found. Please /dryrun again.", show_alert=True)
            return
        if entry.get("used"):
            q.answer("Already executed.", show_alert=True)
            return
        if now - entry["ts"] > PLAN_TTL_SECONDS:
            q.answer("Plan expired. Please /dryrun again.", show_alert=True)
            return

        # Mark used BEFORE sending (idempotent)
        entry["used"] = True
        plan = entry["plan"]

        exec_forced = getattr(strategy_manager, "execute_forced", None) if strategy_manager else None
        if not callable(exec_forced):
            q.answer("Forced execution not available.", show_alert=True)
            return

        tx = exec_forced(strat, plan=plan)  # should honor router-level slippage guard

        msg = (
            f"[{strat}] FORCED EXECUTION\n"
            f"• Using plan_id {plan_id} (age {now - entry['ts']}s)\n"
            f"• Broadcasting: {plan.get('size','?')} → {plan.get('est_out','?')} (max slippage {plan.get('slippage_limit','?')})\n"
            f"• Tx: {tx.get('hash','?')}\n"
            f"• Result: {tx.get('status','?')}"
        )
        q.edit_message_text(text=msg)
    except Exception as e:
        q.answer("Execution error", show_alert=True)
        logger.exception("cb_execute_now error: %s", e)
        context.bot.send_message(chat_id=update.effective_chat.id, text=f"❌ Execute error: {e}")

def cmd_disable(update, context):
    args = [a.lower() for a in context.args]
    if not args:
        context.bot.send_message(chat_id=update.effective_chat.id, text="Usage: /disable <strategy> | /disable all")
        return
    if not strategy_manager:
        context.bot.send_message(chat_id=update.effective_chat.id, text="❌ strategy_manager not available.")
        return
    try:
        if args[0] == "all":
            fn = getattr(strategy_manager, "disable_all", None)
            if callable(fn): fn()
            context.bot.send_message(chat_id=update.effective_chat.id, text="✅ All strategies disabled")
        else:
            fn = getattr(strategy_manager, "disable", None)
            if callable(fn): fn(args[0])
            context.bot.send_message(chat_id=update.effective_chat.id, text=f"✅ {args[0]} disabled")
    except Exception as e:
        context.bot.send_message(chat_id=update.effective_chat.id, text=f"❌ /disable failed: {e}")
        logger.exception("/disable error")

def cmd_enable(update, context):
    args = [a.lower() for a in context.args]
    if not args:
        context.bot.send_message(chat_id=update.effective_chat.id, text="Usage: /enable <strategy> | /enable all")
        return
    if not strategy_manager:
        context.bot.send_message(chat_id=update.effective_chat.id, text="❌ strategy_manager not available.")
        return
    try:
        if args[0] == "all":
            fn = getattr(strategy_manager, "enable_all", None)
            if callable(fn): fn()
            context.bot.send_message(chat_id=update.effective_chat.id, text="✅ All strategies enabled")
        else:
            fn = getattr(strategy_manager, "enable", None)
            if callable(fn): fn(args[0])
            context.bot.send_message(chat_id=update.effective_chat.id, text=f"✅ {args[0]} enabled")
    except Exception as e:
        context.bot.send_message(chat_id=update.effective_chat.id, text=f"❌ /enable failed: {e}")
        logger.exception("/enable error")

def cmd_cooldowns(update, context):
    args = [a.lower() for a in context.args]
    chat_id = update.effective_chat.id
    if not strategy_manager:
        context.bot.send_message(chat_id=chat_id, text="❌ strategy_manager not available.")
        return

    try:
        if not args:
            fn = getattr(strategy_manager, "cooldowns", None)
            if not callable(fn):
                raise RuntimeError("strategy_manager.cooldowns() not found")
            info = fn()  # expected: {strategy: {"seconds": int, "next_in": int}, ...}
            if not info:
                context.bot.send_message(chat_id=chat_id, text="No cooldowns configured.")
                return
            lines = []
            for s, v in info.items():
                lines.append(f"{s}: {v.get('seconds','0')}s (next in {v.get('next_in','0')}s)")
            context.bot.send_message(chat_id=chat_id, text="\n".join(lines))
            return

        if args[0] == "set":
            if len(args) < 3:
                context.bot.send_message(chat_id=chat_id, text="Usage: /cooldowns set <strategy> <seconds>")
                return
            s, seconds = args[1], int(args[2])
            fn = getattr(strategy_manager, "set_cooldown", None)
            if not callable(fn):
                raise RuntimeError("strategy_manager.set_cooldown() not found")
            fn(s, seconds)
            context.bot.send_message(chat_id=chat_id, text=f"✅ Cooldown for {s} set to {seconds}s")
        elif args[0] == "off":
            if len(args) < 2:
                context.bot.send_message(chat_id=chat_id, text="Usage: /cooldowns off <strategy>")
                return
            s = args[1]
            fn = getattr(strategy_manager, "set_cooldown", None)
            if not callable(fn):
                raise RuntimeError("strategy_manager.set_cooldown() not found")
            fn(s, 0)
            context.bot.send_message(chat_id=chat_id, text=f"✅ Cooldown for {s} disabled")
        else:
            context.bot.send_message(chat_id=chat_id, text="Usage:\n/cooldowns\n/cooldowns set <strategy> <seconds>\n/cooldowns off <strategy>")
    except Exception as e:
        context.bot.send_message(chat_id=chat_id, text=f"❌ /cooldowns failed: {e}")
        logger.exception("/cooldowns error")

def cmd_version(update, context):
    chat_id = update.effective_chat.id
    try:
        try:
            ver = VERSION_FILE.read_text().strip()
        except Exception:
            ver = "unknown"
        h = hashlib.sha256()
        for p in CHECKSUM_FILES:
            try:
                h.update(p.read_bytes())
            except Exception:
                pass
        checksum = h.hexdigest()[:8]
        context.bot.send_message(
            chat_id=chat_id,
            text=f"tecbot {ver}\nConfig checksum: {checksum}\nPython {sys.version.split()[0]} • PTB 13.x",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        context.bot.send_message(chat_id=chat_id, text=f"❌ /version failed: {e}")
        logger.exception("/version error")

def cmd_report(update, context):
    chat_id = update.effective_chat.id
    try:
        # Expect a report generator in strategy_manager or a separate module
        report_fn = getattr(strategy_manager, "report_24h", None) if strategy_manager else None
        if not callable(report_fn):
            # Basic stub output if not implemented
            context.bot.send_message(chat_id=chat_id, text="(report) Not implemented yet.")
            return

        rep = report_fn()  # expected to return a formatted string OR dict we can render
        if isinstance(rep, str):
            context.bot.send_message(chat_id=chat_id, text=rep)
        else:
            # naive render
            lines = ["Report (last 24h)"]
            for k, v in rep.items():
                lines.append(f"{k}: {v}")
            context.bot.send_message(chat_id=chat_id, text="\n".join(lines))
    except Exception as e:
        context.bot.send_message(chat_id=chat_id, text=f"❌ /report failed: {e}")
        logger.exception("/report error")

# -----------------------------
# Register & run
# -----------------------------
def main():
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    # Command handlers — register BEFORE any generic MessageHandlers
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("help", cmd_help))
    dp.add_handler(CommandHandler("ping", cmd_ping))
    dp.add_handler(CommandHandler("balances", cmd_balances))
    dp.add_handler(CommandHandler("sanity", cmd_sanity))
    dp.add_handler(CommandHandler("prices", cmd_prices))
    dp.add_handler(CommandHandler("plan", cmd_plan))
    dp.add_handler(CommandHandler("dryrun", cmd_dryrun))
    dp.add_handler(CommandHandler("disable", cmd_disable))
    dp.add_handler(CommandHandler("enable", cmd_enable))
    dp.add_handler(CommandHandler("cooldowns", cmd_cooldowns))
    dp.add_handler(CommandHandler("version", cmd_version))
    dp.add_handler(CommandHandler("report", cmd_report))

    # Callback for "Execute now" buttons from /dryrun
    dp.add_handler(CallbackQueryHandler(cb_execute_now, pattern=r"^exec:"))

    # IMPORTANT: If you have a generic text handler, make sure it does NOT capture commands:
    # dp.add_handler(MessageHandler(Filters.text & ~Filters.command, your_text_handler))

    logger.info("Telegram bot started")
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()

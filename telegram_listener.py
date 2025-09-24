# /bot/app/telegram_listener.py
# Only differences from the last version: /prices now prints ETH comparison (Harmony vs Coinbase)

import os
import sys
import time
import logging
import hashlib
from decimal import Decimal
from pathlib import Path

from telegram import ParseMode, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, Filters

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("BOT_TOKEN") or ""
if not BOT_TOKEN:
    print("ERROR: TELEGRAM_BOT_TOKEN not set", file=sys.stderr)

try:
    from app.config import ADMIN_CHAT_IDS as CFG_ADMINS
    ADMIN_CHAT_IDS = set(CFG_ADMINS) if isinstance(CFG_ADMINS, (set, list, tuple)) else {1539031664}
except Exception:
    ADMIN_CHAT_IDS = {1539031664}

REPO_ROOT = Path("/bot")
VERSION_FILE = REPO_ROOT / "VERSION"
CHECKSUM_FILES = [
    REPO_ROOT / "app" / "telegram_listener.py",
    REPO_ROOT / "app" / "preflight.py",
    REPO_ROOT / "app" / "wallet.py",
    REPO_ROOT / "app" / "price_feed.py",
    REPO_ROOT / "app" / "config.py",
]

PLAN_TTL_SECONDS = 60
PLAN_CACHE = {}

logger = logging.getLogger("telegram_listener")
logger.setLevel(logging.INFO)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s:%(name)s:%(message)s"))
logger.addHandler(_handler)

def _try_import(mod, attr=None):
    try:
        m = __import__(mod, fromlist=[attr] if attr else [])
        return getattr(m, attr) if attr else m
    except Exception:
        return None

price_feed = _try_import("app.price_feed")
preflight = _try_import("app.preflight")
wallet = _try_import("app.wallet")
config = _try_import("app.config")
strategy_manager = _try_import("app.strategy_manager")

def _is_admin(update_or_query) -> bool:
    chat = getattr(update_or_query, "effective_chat", None)
    if chat and chat.id in ADMIN_CHAT_IDS:
        return True
    user = getattr(getattr(update_or_query, "callback_query", None), "from_user", None)
    return bool(user and user.id in ADMIN_CHAT_IDS)

def _mk_plan_id(plan_payload: dict) -> str:
    return hashlib.sha256(repr(sorted(plan_payload.items())).encode()).hexdigest()[:8]

def _decimals(sym: str) -> int:
    try:
        return int(getattr(config, "DECIMALS", {}).get(sym, 18))
    except Exception:
        return 18

def _d(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))

def cmd_start(update, context):
    context.bot.send_message(update.effective_chat.id, "TECBot online. Type /help for commands.")

def cmd_help(update, context):
    context.bot.send_message(
        update.effective_chat.id,
        (
            "*TECBot Commands*\n"
            "/ping — Health check\n"
            "/balances — Show wallet balances\n"
            "/sanity — Quick balance sanity check\n"
            "/prices — Show prices (Quoter) + ETH Coinbase compare\n"
            "/plan [all|<strategy>] — Preview intentions\n"
            "/dryrun [all|<strategy>] — Sim; tap button to execute\n"
            "/disable <strategy>|all — Disable\n"
            "/enable <strategy>|all — Enable\n"
            "/cooldowns — Show/set cooldowns\n"
            "/version — Show version & checksum\n"
            "/report — Per-bot & total PnL (if implemented)"
        ),
        parse_mode=ParseMode.MARKDOWN,
    )

def cmd_ping(update, context):
    context.bot.send_message(update.effective_chat.id, "pong")

def cmd_balances(update, context):
    chat_id = update.effective_chat.id
    try:
        WALLETS = getattr(config, "WALLETS", {})
        TOKENS = getattr(config, "TOKENS", {})
        if not WALLETS:
            raise RuntimeError("No WALLETS in config.py")

        lines = ["*Balances*"]
        ONE_DEC = Decimal(10) ** 18

        for wname, waddr in WALLETS.items():
            if not waddr:
                continue
            lines.append(f"\n*{wname}*")

            try:
                one_wei = wallet.get_native_balance_wei(waddr)
                one = _d(one_wei) / ONE_DEC
                lines.append(f"  ONE  {one.normalize()}")
            except Exception as e:
                lines.append(f"  ONE  error: {e}")

            for sym, token_addr in TOKENS.items():
                if sym == "WONE":
                    continue
                try:
                    raw = wallet.get_erc20_balance_wei(token_addr, waddr)
                    dec = Decimal(10) ** _decimals(sym)
                    amt = _d(raw) / dec
                    lines.append(f"  {sym}  {amt.normalize()}")
                except Exception as e:
                    lines.append(f"  {sym}  error: {e}")

        context.bot.send_message(chat_id, "\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        context.bot.send_message(chat_id, f"❌ /balances failed: {e}")
        logger.exception("/balances error")

def cmd_sanity(update, context):
    chat_id = update.effective_chat.id
    try:
        if not preflight or not callable(getattr(preflight, "run_sanity", None)):
            raise RuntimeError("preflight.run_sanity() not available")
        context.bot.send_message(chat_id, "Running sanity checks…")
        result = preflight.run_sanity()
        ok = "✅ All good" if result.get("ok") else "⚠️ Attention needed"
        lines = [f"*Sanity Check* {ok}", ""]
        for item in result.get("items", []):
            status_icon = "✅" if item.get("status") == "ok" else "❌"
            lines.append(f"{status_icon} *{item.get('wallet','?')}*")
            for c in item.get("checks", []):
                ico = "✅" if c.get("ok") else "❌"
                lines.append(f"  {ico} {c.get('asset','?')}: have {c.get('have','?')} | need {c.get('need','?')}")
            lines.append("")
        context.bot.send_message(chat_id, "\n".join(lines).strip(), parse_mode=ParseMode.MARKDOWN)
        logger.info("/sanity replied")
    except Exception as e:
        context.bot.send_message(chat_id, f"❌ /sanity failed: {e}")
        logger.exception("/sanity error")

def cmd_prices(update, context):
    """Quoter-only for all tokens; ETH additionally shows Coinbase USD comparison."""
    chat_id = update.effective_chat.id
    try:
        if not price_feed or not callable(getattr(price_feed, "get_prices", None)):
            raise RuntimeError("price_feed.get_prices() not available (ensure Quoter-backed get_prices exists).")
        snap = price_feed.get_prices()
        prices = snap.get("prices", {})
        if not isinstance(prices, dict) or not prices:
            raise RuntimeError("Quoter returned empty prices")

        lines = [f"*Prices* _({snap.get('via','routes')})_"]
        preferred = ["TEC", "1sDAI", "1ETH", "WONE", "1USDC"]
        shown = set()
        for key in preferred:
            if key in prices:
                lines.append(f"{key}: {prices[key]}")
                shown.add(key)
        for k, v in prices.items():
            if k not in shown:
                lines.append(f"{k}: {v}")

        # ETH comparison formatting (if present)
        cmp_map = snap.get("comparisons", {})
        eth_cmp = cmp_map.get("1ETH")
        if eth_cmp:
            lines.append(
                f"_1ETH compare_: Harmony {eth_cmp.get('harmony_1USDC')} | "
                f"Coinbase {eth_cmp.get('coinbase_usd')} | "
                f"Δ {eth_cmp.get('premium_pct')}%"
            )

        context.bot.send_message(chat_id, "\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        context.bot.send_message(chat_id, f"❌ /prices failed: {e}")
        logger.exception("/prices error")

# (The rest of the listener stays exactly as in the previous version — omitted here for brevity)
# --- START unchanged handlers ---

def cmd_plan(update, context):
    if not strategy_manager or not callable(getattr(strategy_manager, "simulate_tick", None)):
        context.bot.send_message(update.effective_chat.id, "Plan/preview not wired yet (simulate_tick missing).")
        return
    args = [a.lower() for a in context.args]
    target = args[0] if args else "all"
    fn = strategy_manager.simulate_tick
    strategies = getattr(strategy_manager, "list_strategies", lambda: ["sdai-arb","eth-arb","tec-rebal","usdc-hedge"])()
    selected = strategies if target == "all" else [target]
    blocks = []
    for strat in selected:
        try:
            sim = fn(strat)
            if sim.get("would_broadcast"):
                blocks.append(f"[{strat}] READY — {sim.get('action','trade')} (edge {sim.get('edge','?')})")
            else:
                blocks.append(f"[{strat}] NOT READY — {sim.get('reason','not ready')}")
        except Exception as e:
            blocks.append(f"[{strat}] ❌ plan error: {e}")
    context.bot.send_message(update.effective_chat.id, "\n".join(blocks) or "No strategies found.")

def _render_dryrun_block(strat: str, sim: dict, plan_id: str, ts: int):
    age = int(time.time()) - ts
    if sim.get("would_broadcast"):
        text = (
            f"[{strat}] ✓ DRYRUN OK\n"
            f"• Sim: {sim.get('size','?')} → {sim.get('est_out','?')} • Slip {sim.get('slippage_limit','?')} • Gas ~{sim.get('gas_est','?')}\n"
            f"• Would broadcast: YES\n"
            f"• plan_id: {plan_id} (valid {max(0, PLAN_TTL_SECONDS-age)}s)"
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"Execute now ({strat})", callback_data=f"exec:{strat}:{plan_id}")]])
    else:
        text = f"[{strat}] ✗ DRYRUN FAIL\n• Reason: {sim.get('reason','unknown')}\n• Would broadcast: NO"
        kb = None
    return text, kb

def cmd_dryrun(update, context):
    if not strategy_manager or not callable(getattr(strategy_manager, "simulate_tick", None)):
        context.bot.send_message(update.effective_chat.id, "Dryrun not wired yet (simulate_tick missing).")
        return
    args = [a.lower() for a in context.args]
    target = args[0] if args else "all"
    sim_fn = strategy_manager.simulate_tick
    strategies = getattr(strategy_manager, "list_strategies", lambda: ["sdai-arb","eth-arb","tec-rebal","usdc-hedge"])()
    selected = strategies if target == "all" else [target]

    blocks, buttons = [], []
    now = int(time.time())
    for strat in selected:
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
                buttons.extend(kb.inline_keyboard)
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
    q = update.callback_query
    try:
        if not _is_admin(update):
            q.answer("Admin only.", show_alert=True)
            return

        try:
            _, strat, plan_id = q.data.split(":", 2)
        except Exception:
            q.answer("Invalid payload.", show_alert=True)
            return

        entry = PLAN_CACHE.get(strat)
        now = int(time.time())
        if not entry or entry.get("plan_id") != plan_id:
            q.answer("Plan not found. Please /dryrun again.", show_alert=True); return
        if entry.get("used"):
            q.answer("Already executed.", show_alert=True); return
        if now - entry["ts"] > PLAN_TTL_SECONDS:
            q.answer("Plan expired. Please /dryrun again.", show_alert=True); return

        exec_forced = getattr(strategy_manager, "execute_forced", None) if strategy_manager else None
        if not callable(exec_forced):
            q.answer("Forced execution not available.", show_alert=True); return

        entry["used"] = True
        plan = entry["plan"]
        tx = exec_forced(strat, plan=plan)
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
    if not strategy_manager:
        context.bot.send_message(update.effective_chat.id, "❌ strategy_manager not available."); return
    args = [a.lower() for a in context.args]
    if not args:
        context.bot.send_message(update.effective_chat.id, "Usage: /disable <strategy> | /disable all"); return
    try:
        if args[0] == "all":
            fn = getattr(strategy_manager, "disable_all", None);  fn and fn()
            context.bot.send_message(update.effective_chat.id, "✅ All strategies disabled")
        else:
            fn = getattr(strategy_manager, "disable", None);      fn and fn(args[0])
            context.bot.send_message(update.effective_chat.id, f"✅ {args[0]} disabled")
    except Exception as e:
        context.bot.send_message(update.effective_chat.id, f"❌ /disable failed: {e}")
        logger.exception("/disable error")

def cmd_enable(update, context):
    if not strategy_manager:
        context.bot.send_message(update.effective_chat.id, "❌ strategy_manager not available."); return
    args = [a.lower() for a in context.args]
    if not args:
        context.bot.send_message(update.effective_chat.id, "Usage: /enable <strategy> | /enable all"); return
    try:
        if args[0] == "all":
            fn = getattr(strategy_manager, "enable_all", None); fn and fn()
            context.bot.send_message(update.effective_chat.id, "✅ All strategies enabled")
        else:
            fn = getattr(strategy_manager, "enable", None);     fn and fn(args[0])
            context.bot.send_message(update.effective_chat.id, f"✅ {args[0]} enabled")
    except Exception as e:
        context.bot.send_message(update.effective_chat.id, f"❌ /enable failed: {e}")
        logger.exception("/enable error")

def cmd_cooldowns(update, context):
    if not strategy_manager:
        context.bot.send_message(update.effective_chat.id, "❌ strategy_manager not available."); return
    args = [a.lower() for a in context.args]
    chat_id = update.effective_chat.id
    try:
        if not args:
            fn = getattr(strategy_manager, "cooldowns", None)
            if not callable(fn): raise RuntimeError("strategy_manager.cooldowns() not found")
            info = fn()
            if not info: context.bot.send_message(chat_id, "No cooldowns configured."); return
            lines = [f"{s}: {v.get('seconds','0')}s (next in {v.get('next_in','0')}s)" for s, v in info.items()]
            context.bot.send_message(chat_id, "\n".join(lines)); return
        if args[0] == "set":
            if len(args) < 3: context.bot.send_message(chat_id, "Usage: /cooldowns set <strategy> <seconds>"); return
            s, seconds = args[1], int(args[2])
            fn = getattr(strategy_manager, "set_cooldown", None)
            if not callable(fn): raise RuntimeError("strategy_manager.set_cooldown() not found")
            fn(s, seconds); context.bot.send_message(chat_id, f"✅ Cooldown for {s} set to {seconds}s")
        elif args[0] == "off":
            if len(args) < 2: context.bot.send_message(chat_id, "Usage: /cooldowns off <strategy>"); return
            s = args[1]
            fn = getattr(strategy_manager, "set_cooldown", None)
            if not callable(fn): raise RuntimeError("strategy_manager.set_cooldown() not found")
            fn(s, 0); context.bot.send_message(chat_id, f"✅ Cooldown for {s} disabled")
        else:
            context.bot.send_message(chat_id, "Usage:\n/cooldowns\n/cooldowns set <strategy> <seconds>\n/cooldowns off <strategy>")
    except Exception as e:
        context.bot.send_message(chat_id, f"❌ /cooldowns failed: {e}")
        logger.exception("/cooldowns error")

def cmd_version(update, context):
    chat_id = update.effective_chat.id
    try:
        ver = "unknown"
        try: ver = VERSION_FILE.read_text().strip()
        except Exception: pass
        h = hashlib.sha256()
        for p in CHECKSUM_FILES:
            try: h.update(p.read_bytes())
            except Exception: pass
        checksum = h.hexdigest()[:8]
        context.bot.send_message(
            chat_id,
            f"tecbot {ver}\nConfig checksum: {checksum}\nPython {sys.version.split()[0]} • PTB 13.x",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        context.bot.send_message(chat_id, f"❌ /version failed: {e}")
        logger.exception("/version error")

def cmd_report(update, context):
    chat_id = update.effective_chat.id
    try:
        report_fn = getattr(strategy_manager, "report_24h", None) if strategy_manager else None
        if not callable(report_fn):
            context.bot.send_message(chat_id, "(report) Not implemented yet."); return
        rep = report_fn()
        if isinstance(rep, str):
            context.bot.send_message(chat_id, rep)
        else:
            lines = ["Report (last 24h)"]
            for k, v in rep.items():
                lines.append(f"{k}: {v}")
            context.bot.send_message(chat_id, "\n".join(lines))
    except Exception as e:
        context.bot.send_message(chat_id, f"❌ /report failed: {e}")
        logger.exception("/report error")

def main():
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

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

    dp.add_handler(CallbackQueryHandler(cb_execute_now, pattern=r"^exec:"))

    logger.info("Telegram bot started")
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()

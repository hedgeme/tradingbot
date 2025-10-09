#!/usr/bin/env python3
# TECBot Telegram Listener — python-telegram-bot 13.x, chain-forward (QuoterV2)
# This file expects:
#   - app/prices.py   with price_usd(sym, Decimal) and helpers
#   - app/slippage.py with compute_slippage(token_in, token_out, Decimal, bps)
#   - app/balances.py with all_balances() -> {wallet: {sym: Decimal/float}}
#   - app/strategies/planner.py (optional)
#   - runner.py or app/runner.py (optional)

import os, sys, logging, subprocess, shlex
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional, Dict, Any, List

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ParseMode, Update
from telegram.ext import (
    Updater, CommandHandler, CallbackQueryHandler, MessageHandler, Filters, CallbackContext
)

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
log = logging.getLogger("telegram_listener")

# Ensure /bot imports work (root and app.* both supported)
if "/bot" not in sys.path:
    sys.path.insert(0, "/bot")

# ---------- Tolerant imports (prefer app.*, else root) ----------
# config
try:
    from app import config as C
    log.info("Loaded config from app.config")
except Exception:
    try:
        import config as C
        log.info("Loaded config from root config")
    except Exception:
        log.exception("Failed to import config")
        raise

# optional modules (prices, balances, slippage)
PR = BL = SL = None
try:
    from app import prices as PR
    log.info("Loaded prices from app.prices")
except Exception:
    try:
        import prices as PR
        log.info("Loaded prices from root prices")
    except Exception as e:
        log.warning("prices module not available: %s", e)

try:
    from app import balances as BL
    log.info("Loaded balances from app.balances")
except Exception:
    try:
        import balances as BL
        log.info("Loaded balances from root balances")
    except Exception as e:
        log.warning("balances module not available: %s", e)

try:
    from app import slippage as SL
    log.info("Loaded slippage from app.slippage")
except Exception:
    try:
        import slippage as SL
        log.info("Loaded slippage from root slippage")
    except Exception as e:
        log.warning("slippage module not available: %s", e)

# optional planner/runner (you may have stubs)
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

# ---------- Utilities ----------
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

def _fmt_kv(d: Dict[str, Any], indent: int = 2) -> str:
    pad = " " * indent
    return "\n".join(f"{pad}{k}: {v}" for k, v in d.items())

def _format_decimal(x: Any, places: int = 8) -> str:
    try:
        if isinstance(x, Decimal):
            q = Decimal(10) ** -places
            return f"{x.quantize(q):,f}"
        if isinstance(x, (int, float)):
            return f"{x:,.{places}f}"
        return str(x)
    except Exception:
        return str(x)

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

def _render_balances_table(data: Dict[str, Dict[str, Any]]) -> str:
    # Collect all symbols that appear (to keep columns consistent)
    syms = set()
    for _, rows in data.items():
        syms.update(rows.keys())
    syms = ["ONE(native)"] + sorted([s for s in syms if s != "ONE(native)"])

    name_w = max(12, max(len(w) for w in data.keys()))
    col_w  = 14

    lines = []
    lines.append(f"Balances (@ {now_iso()})")
    header = ("Wallet".ljust(name_w)) + "  " + "  ".join(s.rjust(col_w) for s in syms)
    lines.append(header)
    lines.append("-" * len(header))

    for w in sorted(data.keys()):
        row = data[w]
        vals = []
        for s in syms:
            v = row.get(s, 0)
            vals.append(_format_decimal(v).rjust(col_w))
        lines.append(w.ljust(name_w) + "  " + "  ".join(vals))
    return "```\n" + "\n".join(lines) + "\n```"

def _render_assets(tokens: Dict[str, str], wallets: Dict[str, str]) -> str:
    # Token table
    t_sym_w = max(5, max((len(k) for k in tokens.keys()), default=5))
    t_addr_w = max(42, max((len(v) for v in tokens.values()), default=42))
    lines = [f"Assets (@ {now_iso()})", "```"]
    lines.append("TOKENS".ljust(t_sym_w) + "  " + "ADDRESS".ljust(t_addr_w))
    lines.append("-" * (t_sym_w + 2 + t_addr_w))
    for s, a in sorted(tokens.items()):
        lines.append(s.ljust(t_sym_w) + "  " + a.ljust(t_addr_w))
    lines.append("")
    # Wallet table
    w_name_w = max(10, max((len(k) for k in wallets.keys()), default=10))
    w_addr_w = max(42, max((len(v) for v in wallets.values()), default=42))
    lines.append("WALLETS".ljust(w_name_w) + "  " + "ADDRESS".ljust(w_addr_w))
    lines.append("-" * (w_name_w + 2 + w_addr_w))
    for s, a in sorted(wallets.items()):
        lines.append(s.ljust(w_name_w) + "  " + a.ljust(w_addr_w))
    lines.append("```")
    return "\n".join(lines)

# ---------- Meta logging ----------
def _log_update(update: Update, context: CallbackContext):
    try:
        uid = update.effective_user.id if update.effective_user else "?"
        txt = update.effective_message.text if update.effective_message else "<non-text>"
        log.info(f"UPDATE from {uid}: {txt}")
    except Exception:
        pass

def _log_error(update: object, context: CallbackContext):
    log.exception("Handler error")

# ---------- Commands (meta/system) ----------
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
        "  /slippage <IN> [AMOUNT] [OUT] — live quote/minOut (default OUT=1USDC, AMOUNT=1)\n"
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
    update.message.reply_text(_render_assets(tokens, wallets), parse_mode=ParseMode.MARKDOWN)

# ---------- Commands (on-chain) ----------
def cmd_prices(update: Update, context: CallbackContext):
    if PR is None:
        update.message.reply_text("Prices unavailable (module not loaded)."); return
    args = (context.args or [])
    syms = [s.upper() for s in args] if args else list(getattr(C, "TOKENS", {}).keys())
    # cap to something readable in chat
    if len(syms) > 8:
        syms = syms[:8]
    lines = [f"Prices (@ {now_iso()}) — USD per 1 token"]
    try:
        for s in syms:
            try:
                px = PR.price_usd(s, Decimal("1"))
                lines.append(f"  {s}: {'—' if px is None else f'${px}'}")
            except Exception as e:
                lines.append(f"  {s}: error ({e})")
    except Exception as e:
        log.exception("prices failure")
        update.message.reply_text(f"Prices error: {e}"); return
    update.message.reply_text("\n".join(lines))

def cmd_balances(update: Update, context: CallbackContext):
    if BL is None:
        update.message.reply_text("Balances unavailable (module not loaded)."); return
    try:
        data = BL.all_balances()
    except Exception as e:
        log.exception("balances failure")
        update.message.reply_text(f"Balances error: {e}"); return
    update.message.reply_text(_render_balances_table(data), parse_mode=ParseMode.MARKDOWN)

def _parse_decimal(s: str, default: Decimal) -> Decimal:
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return default

def cmd_slippage(update: Update, context: CallbackContext):
    if SL is None:
        update.message.reply_text("Slippage unavailable (module not loaded)."); return
    args = context.args or []
    default_bps = int(getattr(C, "SLIPPAGE_DEFAULT_BPS", 30))

    if not args:
        update.message.reply_text(
            "Usage: /slippage <TOKEN_IN> [AMOUNT] [TOKEN_OUT]\n"
            "Examples:\n"
            "  /slippage 1ETH            (defaults: 1 unit to 1USDC)\n"
            "  /slippage 1ETH 0.5 1USDC  (0.5 1ETH to 1USDC)"
        ); return

    token_in = args[0].upper()
    amount = _parse_decimal(args[1], Decimal("1")) if len(args) >= 2 else Decimal("1")
    token_out = args[2].upper() if len(args) >= 3 else "1USDC"

    try:
        res = SL.compute_slippage(token_in, token_out, amount, default_bps)
    except Exception as e:
        log.exception("slippage failure")
        update.message.reply_text(f"Slippage error: {e}"); return
    if not res:
        update.message.reply_text(f"No route for {token_in} → {token_out}. Check POOLS_V3."); return

    # res keys from app/slippage.py: amount_out_fmt, min_out_fmt, impact_bps, slippage_bps, path_text
    lines = [
        f"Slippage — {token_in} → {token_out}",
        f"Size     : {amount} {token_in}",
        f"Route    : {res.get('path_text','')}",
        f"QuoteOut : {res['amount_out_fmt']} {token_out}",
        f"minOut   : {res['min_out_fmt']} {token_out}  (tolerance {res['slippage_bps']} bps)",
    ]
    if res.get("impact_bps") is not None:
        lines.append(f"Impact   : {res['impact_bps']} bps")
    update.message.reply_text("\n".join(lines))

# ---------- Commands (planner/runner) ----------
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

# ---------- Main ----------
def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or getattr(C, "TELEGRAM_BOT_TOKEN", None)
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN not set in environment or config")

    up = Updater(token=token, use_context=True)
    dp = up.dispatcher

    # Core & on-chain
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("strat", cmd_start))  # alias
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

    # Callbacks & diagnostics
    dp.add_handler(CallbackQueryHandler(on_exec_button, pattern=r"^exec:[A-Za-z0-9_\-]+$"))
    dp.add_handler(CallbackQueryHandler(on_exec_confirm, pattern=r"^exec_go:[A-Za-z0-9_\-]+$"))
    dp.add_handler(CallbackQueryHandler(on_exec_cancel, pattern=r"^exec_cancel$"))
    dp.add_error_handler(_log_error)
    dp.add_handler(MessageHandler(Filters.all, _log_update), group=-1)

    log.info("Handlers registered: /start /strat /help /version /sanity /assets /prices /balances /slippage /ping /plan /dryrun /cooldowns")
    up.start_polling(clean=True)  # deprecation warning is harmless on v13
    log.info("Telegram bot started")
    up.idle()

if __name__ == "__main__":
    main()

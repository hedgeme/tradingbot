#!/usr/bin/env python3
# TECBot Telegram Listener — balances Option C (5dp), Coinbase fix, clearer price notes

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

# Ensure /bot on sys.path so both root modules and app.* work
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
        x = Decimal(x)
    except InvalidOperation:
        return "—"
    if x >= 100:
        return f"${x:,.2f}"
    if x < Decimal("0.1"):
        return f"${x:,.6f}"
    return f"${x:,.4f}"

def _coinbase_eth() -> Optional[Decimal]:
    """
    Fetch ETH/USD from Coinbase using your repo's coinbase_client.py
    (function: fetch_eth_usd_price()).
    """
    try:
        import coinbase_client as _cb
        px = _cb.fetch_eth_usd_price()
        return Decimal(str(px)) if px is not None else None
    except Exception as e:
        log.debug("coinbase fetch failed: %s", e)
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

# ---------- balances (Option C, 5 decimals, fixed-width) ----------
def cmd_balances(update: Update, context: CallbackContext):
    if BL is None:
        update.message.reply_text("Balances unavailable (module not loaded)."); return
    try:
        table = BL.all_balances()
    except Exception as e:
        log.exception("balances failure")
        update.message.reply_text(f"Balances error: {e}"); return

    # Display order you requested
    cols = ["ONE(native)","1USDC","1ETH","TEC","1sDAI","WONE","ONE"]

    def fmt5(x):
        try:
            d = Decimal(str(x))
            s = f"{d:.5f}"
            return "0.00000" if s.upper() == "0E-8" else s
        except Exception:
            return str(x)

    lines = [f"Balances (@ {now_iso()})"]
    # Per-wallet blocks, single line each (Option C)
    for w_name in sorted(table.keys()):
        row = table[w_name]
        parts = [f"{c} {fmt5(row.get(c, 0))}" for c in cols]
        lines.append(f"{w_name}")
        lines.append("  " + "   ".join(parts))
        lines.append("")  # blank line between wallets

    # Wrap in <pre> so Telegram keeps monospaced alignment
    body = "\n".join(lines).rstrip()
    update.message.reply_text(f"<pre>{body}</pre>", parse_mode=ParseMode.HTML)

# ---- ETH forward vs reverse tiny-probe (for Notes) ----
def _eth_forward_reverse_note() -> str:
    """
    Probe 1ETH↔USDC both directions via WONE using QuoterV2.
    Returns one concise line for /prices Notes.
    """
    try:
        from web3 import Web3
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

        # forward: sell 1 ETH
        amt_eth = Decimal("1")
        dec_e = PR._dec("1ETH"); dec_u = PR._dec("1USDC")
        wei_in = int(amt_eth * (Decimal(10)**dec_e))
        path_fwd = (Web3.to_bytes(hexstr=addr("1ETH")) + fee3(3000) +
                    Web3.to_bytes(hexstr=addr("WONE")) + fee3(3000) +
                    Web3.to_bytes(hexstr=addr("1USDC")))
        out_f = q.functions.quoteExactInput(path_fwd, wei_in).call()[0]
        fwd_px = (Decimal(out_f) / (Decimal(10)**dec_u)) / amt_eth

        # reverse: buy ETH with 1,000 USDC (invert)
        amt_usdc = Decimal("1000")
        wei_usdc = int(amt_usdc * (Decimal(10)**dec_u))
        path_rev = (Web3.to_bytes(hexstr=addr("1USDC")) + fee3(3000) +
                    Web3.to_bytes(hexstr=addr("WONE")) + fee3(3000) +
                    Web3.to_bytes(hexstr=addr("1ETH")))
        out_r = q.functions.quoteExactInput(path_rev, wei_usdc).call()[0]
        eth_out = Decimal(out_r) / (Decimal(10)**dec_e)
        rev_px = (amt_usdc / eth_out) if eth_out > 0 else None

        if rev_px is None:
            return f"1ETH: forward ${fwd_px:,.6f} (reverse probe failed)"
        return f"1ETH: forward {fwd_px:,.6f} vs reverse {rev_px:,.6f} — using reverse if they diverge"
    except Exception as e:
        return f"1ETH note probe error: {e}"

def _eth_basis_note(lp_eth_usd: Optional[Decimal]) -> Optional[str]:
    """
    Adds a clear basis line for the LP ETH price and optional impact vs 'mid' estimate.
    """
    try:
        if lp_eth_usd is None or PR is None:
            return None
        # mid estimate from tiny size
        tiny = Decimal("0.01")
        tiny_total = PR.price_usd("1ETH", tiny)  # USDC value for 0.01 ETH
        if not tiny_total:
            return f"1ETH LP price is a live on-chain quote for trading exactly 1.00000 1ETH via 1ETH → WONE → 1USDC."
        mid = tiny_total / tiny
        impact_bps = (Decimal(lp_eth_usd) - mid) / mid * Decimal(10000)
        return ( "1ETH LP price is a live on-chain quote for trading exactly 1.00000 1ETH via 1ETH → WONE → 1USDC. "
                 f"(impact vs tiny-size mid ≈ {impact_bps:.0f} bps)" )
    except Exception:
        return "1ETH LP price is a live on-chain quote for trading exactly 1.00000 1ETH via 1ETH → WONE → 1USDC."

def cmd_prices(update: Update, context: CallbackContext):
    if PR is None:
        update.message.reply_text("Prices unavailable (module not loaded)."); return

    # Your preferred display order
    syms = ["ONE","1USDC","1sDAI","TEC","1ETH"]
    vals = {}
    err_notes: List[str] = []

    for s in syms:
        try:
            v = PR.price_usd(s, Decimal("1"))
            if s == "ONE" and v is None:
                # If native ONE isn't priced, mirror WONE for display
                v = PR.price_usd("WONE", Decimal("1"))
            vals[s] = v
        except Exception as e:
            vals[s] = None
            err_notes.append(f"{s}: error ({e})")

    # Header block you like
    out = ["LP Prices"]
    for s in syms:
        if s == "ONE" and vals[s] is None:
            continue
        out.append(f"  {s:<5} {_fmt_money(vals[s])}")

    # Coinbase compare (ETH)
    lp_eth = vals.get("1ETH")
    cb_eth = _coinbase_eth()
    out += ["", "ETH: Harmony LP vs Coinbase"]
    out.append(f"  LP:       {_fmt_money(lp_eth)}")
    out.append(f"  Coinbase: {_fmt_money(cb_eth)}")
    if lp_eth is not None and cb_eth not in (None, Decimal(0)):
        try:
            diff = (Decimal(lp_eth) - Decimal(cb_eth)) / Decimal(cb_eth) * Decimal(100)
            sign = "+" if diff >= 0 else ""
            out.append(f"  Diff:     {sign}{diff:.2f}%")
        except Exception:
            pass

    # Notes: basis + concise forward/reverse
    out.append("")
    out.append("Notes:")
    basis = _eth_basis_note(lp_eth)
    if basis:
        out.append(f"  - {basis}")
    out.append(f"  - {_eth_forward_reverse_note()}")
    for n in err_notes:
        out.append(f"  - {n}")

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
    try:
        amount_in = Decimal(args[1]) if len(args) >= 2 else Decimal("1")
    except InvalidOperation:
        update.message.reply_text("Bad amount. Example: /slippage 1ETH 0.5 1USDC"); return
    token_out = args[2].upper() if len(args) >= 3 else "1USDC"

    # Mid (per 1 unit of token_in)
    try:
        mid_total = PR.price_usd(token_in, Decimal("1"))
        mid = mid_total if isinstance(mid_total, Decimal) else Decimal(str(mid_total)) if mid_total is not None else None
    except Exception:
        mid = None

    # Headline at requested size (if SL available)
    headline = None
    if SL and hasattr(SL, "compute_slippage"):
        try:
            res = SL.compute_slippage(token_in, token_out, amount_in, int(getattr(C, "SLIPPAGE_DEFAULT_BPS", 30)))
            if res:
                px = (res["amount_out"]/amount_in) if amount_in > 0 else None
                px_txt = f"{px:,.2f}" if px is not None else "—"
                headline = f"Size {amount_in} {token_in}: price {px_txt} {token_out}/{token_in} · impact {res['impact_bps']} bps"
        except Exception as e:
            log.debug("slippage headline calc failed: %s", e)

    # Curve (USDC targets), aligned table
    targets = [Decimal("10"), Decimal("100"), Decimal("1000"), Decimal("10000")]
    rows = []
    for usdc in targets:
        if mid and mid > 0:
            est_in = (usdc / mid).quantize(Decimal("0.000001"))
        else:
            est_in = Decimal("0")
        try:
            px_total = PR.price_usd(token_in, est_in)  # total USDC for est_in
            eff = (px_total / est_in) if (px_total and est_in > 0) else None
            slip = ((eff - mid) / mid * Decimal("100")) if (eff and mid) else None
            rows.append((f"{usdc:,.0f}", f"{est_in:.6f}", f"{eff:,.2f}" if eff else "—", f"{slip:+.2f}%" if slip is not None else "—"))
        except Exception:
            rows.append((f"{usdc:,.0f}", "—", "—", "—"))

    # Pretty fixed-width table (monospace) for readability
    col1, col2, col3, col4 = "Size (USDC)", "Amount In (sym)", "Eff. Price", "Slippage vs mid"
    w1, w2, w3, w4 = 12, 16, 12, 16
    line_hdr = f"{col1:>{w1}} | {col2:>{w2}} | {col3:>{w3}} | {col4:>{w4}}"
    line_sep = "-" * len(line_hdr)
    tbl = [line_hdr, line_sep]
    for a,b,c,d in rows:
        tbl.append(f"{a:>{w1}} | {b:>{w2}} | {('$'+c) if c!='—' else '—':>{w3}} | {d:>{w4}}")

    out = [f"Slippage curve: {token_in} → {token_out}"]
    if mid:
        out.append(f"Baseline (mid): ${mid:,.2f} per 1{token_in}")
    if headline:
        out.append(headline)
    out.append("")
    out.extend(tbl)

    # Send monospace so columns line up perfectly
    body = "\n".join(out)
    update.message.reply_text(f"<pre>{body}</pre>", parse_mode=ParseMode.HTML)

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
    up.start_polling(clean=True)  # ok for ptb 13.x
    log.info("Telegram bot started")
    up.idle()

if __name__ == "__main__":
    main()

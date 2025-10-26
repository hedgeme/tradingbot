#!/usr/bin/env python3
# TECBot Telegram Listener
# - Preserves stable table formats for /prices /balances /slippage
# - Adds /trade (full end-to-end trade wizard with execution)
# - Adds /withdraw (send funds to treasury wallet)
# - Leaves /plan and /dryrun handlers in place for internal use
#
# NOTE: You MUST wire the placeholder tx helpers at the bottom:
#   _tw_quote_swap, _tw_send_swap, _tw_send_approval, _wd_send_transfer
#
# SECURITY:
# - execute trade and withdraw require admin check (is_admin)

import os, sys, logging, subprocess, shlex
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Optional, List, Dict, Any, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ParseMode, Update
from telegram.ext import (
    Updater, CommandHandler, CallbackQueryHandler, MessageHandler, Filters, CallbackContext
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
log = logging.getLogger("telegram_listener")

# ensure /bot on path
if "/bot" not in sys.path:
    sys.path.insert(0, "/bot")

# tolerant imports
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

# coinbase spot helper for /prices
def _coinbase_eth() -> Optional[Decimal]:
    try:
        import coinbase_client  # must expose fetch_eth_usd_price()
        val = coinbase_client.fetch_eth_usd_price()
        return Decimal(str(val)) if val is not None else None
    except Exception:
        return None

# ---------- core utils ----------

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def is_admin(user_id: int) -> bool:
    try:
        return int(user_id) in set(int(x) for x in getattr(C, "ADMIN_USER_IDS", []) or [])
    except Exception:
        return False

def _git_short_rev() -> Optional[str]:
    try:
        out = subprocess.check_output(
            shlex.split("git rev-parse --short HEAD"),
            cwd="/bot",
            stderr=subprocess.DEVNULL,
        )
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
        return f"${x:,.5f}".rstrip("0").rstrip(".") if "." in f"{x:.5f}" else f"${x:.5f}"
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

# ---------- render helpers for /plan and /dryrun (kept as-is) ----------

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

# ---------- core commands (unchanged behavior / formatting) ----------

def cmd_start(update: Update, context: CallbackContext):
    update.message.reply_text(
        "TECBot online.\n"
        "Try: /help\n"
        "Core: /trade /cooldowns /ping\n"
        "Funds: /withdraw\n"
        "On-chain: /prices [SYMS…] /balances /slippage <IN> [AMOUNT] [OUT]\n"
        "Meta: /version /sanity /assets"
    )

def cmd_help(update: Update, context: CallbackContext):
    update.message.reply_text(
        "Commands:\n"
        "  /ping — health check\n"
        "  /trade — manual trade wizard (wallet, route, amount, slippage, execute)\n"
        "  /withdraw — withdraw funds to treasury wallet\n"
        "  /cooldowns [bot|route] — show cooldowns\n"
        "  /prices [SYMS…] — on-chain quotes in USDC\n"
        "  /balances — per-wallet balances (ERC-20 + ONE)\n"
        "  /slippage <IN> [AMOUNT] [OUT] — live impact/minOut\n"
        "  /assets — configured tokens & wallets\n"
        "  /version — code version\n"
        "  /sanity — config/modules sanity\n"
        "  (/plan, /dryrun still exist internally but are not exposed)"
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

    cols = ["ONE", "1USDC", "1ETH", "TEC", "1sDAI"]
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
        for c in cols[1:]:
            vals.append(_fmt_amt(c, row.get(c, 0)))
        lines.append(f"{w_name:<{w_wallet}}  " + "  ".join(f"{v:>{w_amt}}" for v in vals))

    update.message.reply_text(f"<pre>\n{chr(10).join(lines)}\n</pre>", parse_mode=ParseMode.HTML)

# ---------- ETH route helper for /prices (unchanged logic/format intent) ----------

def _eth_best_side_and_route() -> (Optional[Decimal], str):
    if PR is None:
        return None, "fwd"
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

        dec_e = PR._dec("1ETH")
        dec_u = PR._dec("1USDC")
        choices = []
        for usdc_in in (Decimal("25"), Decimal("50"), Decimal("100"), Decimal("250")):
            wei = int(usdc_in * (Decimal(10)**dec_u))
            path = (Web3.to_bytes(hexstr=addr("1USDC")) + fee3(3000) +
                    Web3.to_bytes(hexstr=addr("WONE"))  + fee3(3000) +
                    Web3.to_bytes(hexstr=addr("1ETH")))
            out = q.functions.quoteExactInput(path, wei).call()[0]
            eth_out = Decimal(out) / (Decimal(10)**dec_e)
            if eth_out > 0:
                choices.append(usdc_in / eth_out)
        rev = min(choices) if choices else None

        wei_in = int(Decimal("1") * (Decimal(10)**dec_e))
        path_f = (Web3.to_bytes(hexstr=addr("1ETH")) + fee3(3000) +
                  Web3.to_bytes(hexstr=addr("WONE")) + fee3(3000) +
                  Web3.to_bytes(hexstr=addr("1USDC")))
        out_f = q.functions.quoteExactInput(path_f, wei_in).call()[0]
        fwd = (Decimal(out_f) / (Decimal(10)**dec_u)) if out_f else None

        if rev is not None and fwd is not None:
            cb = _coinbase_eth()
            if cb:
                d_rev = abs(rev - cb)
                d_fwd = abs(fwd - cb)
                return (rev, "rev") if d_rev <= d_fwd else (fwd, "fwd")
            if abs(rev - fwd) / max(Decimal("1"), rev) > Decimal("0.05"):
                return rev, "rev"
            return fwd, "fwd"
        return (rev or fwd), ("rev" if rev else "fwd")
    except Exception:
        return None, "fwd"

def cmd_prices(update: Update, context: CallbackContext):
    if PR is None:
        update.message.reply_text("Prices unavailable (module not loaded)."); return

    syms = ["ONE", "1USDC", "1sDAI", "TEC", "1ETH"]
    w_asset, w_lp, w_basis, w_slip, w_route = 6, 11, 12, 9, 27

    header = (
        f"{'Asset':<{w_asset}} | {'LP Price':>{w_lp}} | {'Quote Basis':>{w_basis}} | "
        f"{'Slippage':>{w_slip}} | {'Route':<{w_route}}"
    )
    sep = "-" * (len(header) + 0)
    lines = ["LP Prices", header, sep]

    cb_eth = _coinbase_eth()
    lp_eth_pref, side = _eth_best_side_and_route()

    eth_lp_val = None

    for s in syms:
        route_text = "—"
        basis = Decimal("1")
        slip_txt = "0 bps"
        price: Optional[Decimal] = None

        try:
            if s == "ONE":
                price = PR.price_usd("WONE", Decimal("1"))
                route_text = "WONE → 1USDC (fwd)" if price is not None else "—"
            elif s == "1USDC":
                price = Decimal("1")
                route_text = "—"
            elif s == "1sDAI":
                price = PR.price_usd("1sDAI", Decimal("1"))
                route_text = "1SDAI → 1USDC (fwd)" if price is not None else "—"
            elif s == "TEC":
                basis = Decimal("100")
                out = PR.price_usd("TEC", basis)
                price = (out / basis) if out is not None and basis > 0 else None
                route_text = "TEC → WONE → 1USDC"
                slip_txt = "—" if price is None else "  "
            elif s == "1ETH":
                price = lp_eth_pref or PR.price_usd("1ETH", Decimal("1"))
                route_text = ("1USDC → WONE → 1ETH (rev)" if side == "rev"
                              else "1ETH → WONE → 1USDC (fwd)")
            else:
                price = PR.price_usd(s, Decimal("1"))
                route_text = "—" if price is None else "(direct/best)"
        except Exception:
            price = None

        if s == "1ETH":
            try:
                eth_lp_val = Decimal(str(price)) if price is not None else None
            except Exception:
                eth_lp_val = None

        lp_str = _fmt_money(price).rjust(w_lp)
        basis_str = f"{basis:.5f}".rjust(w_basis)
        slip_str = slip_txt.rjust(w_slip)
        lines.append(f"{s:<{w_asset}} | {lp_str} | {basis_str} | {slip_str} | {route_text:<{w_route}}")

    lines += ["", "ETH: Harmony LP vs Coinbase"]
    lines.append(f"  LP:       {_fmt_money(eth_lp_val)}")
    lines.append(f"  Coinbase: {_fmt_money(cb_eth)}")
    if eth_lp_val is not None and cb_eth not in (None, Decimal(0)):
        try:
            diff = (Decimal(eth_lp_val) - Decimal(cb_eth)) / Decimal(cb_eth) * Decimal(100)
            lines.append(f"  Diff:     {diff:+.2f}%")
        except Exception:
            pass

    update.message.reply_text(f"<pre>\n{chr(10).join(lines)}\n</pre>", parse_mode=ParseMode.HTML)

# ---------- /slippage (unchanged table style) ----------

def _mid_usdc_per_unit(token_in: str) -> Optional[Decimal]:
    if PR is None:
        return None
    t = token_in.upper()
    try:
        if t == "1ETH":
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
            dec_e = PR._dec("1ETH")
            dec_u = PR._dec("1USDC")
            choices = []
            for usdc_in in (Decimal("25"), Decimal("50"), Decimal("100"), Decimal("250")):
                wei = int(usdc_in * (Decimal(10)**dec_u))
                path = (Web3.to_bytes(hexstr=addr("1USDC")) + fee3(3000) +
                        Web3.to_bytes(hexstr=addr("WONE"))  + fee3(3000) +
                        Web3.to_bytes(hexstr=addr("1ETH")))
                out = q.functions.quoteExactInput(path, wei).call()[0]
                eth_out = Decimal(out) / (Decimal(10)**dec_e)
                if eth_out > 0:
                    choices.append(usdc_in / eth_out)
            return min(choices) if choices else None
        if t == "ONE":
            return PR.price_usd("WONE", Decimal("1"))
        return PR.price_usd(t, Decimal("1"))
    except Exception:
        return None

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

    w1, w2, w3, w4 = 12, 16, 12, 16
    mid = _mid_usdc_per_unit(token_in)

    targets = [Decimal("10"), Decimal("100"), Decimal("1000"), Decimal("10000")]
    rows = []
    for usdc in targets:
        if mid and mid > 0:
            est_in = (usdc / mid).quantize(Decimal("0.000001"))
        else:
            est_in = Decimal("0")
        try:
            if token_in == "ONE":
                px_usd = PR.price_usd("WONE", est_in)
            else:
                px_usd = PR.price_usd(token_in, est_in)
            eff = (px_usd / est_in) if (px_usd and est_in > 0) else None
            slip = ((eff - mid) / mid * Decimal("100")) if (eff and mid) else None
            rows.append(
                (
                    f"{usdc:,.0f}",
                    f"{est_in:.6f}",
                    f"{eff:,.2f}" if eff else "—",
                    f"{slip:+.2f}%" if slip is not None else "—",
                )
            )
        except Exception:
            rows.append((f"{usdc:,.0f}", "—", "—", "—"))

    col1, col2, col3, col4 = "Size (USDC)", "Amount In (sym)", "Eff. Price", "Slippage vs mid"
    line_hdr = f"{col1:>{w1}} | {col2:>{w2}} | {col3:>{w3}} | {col4:>{w4}}"
    line_sep = "-" * len(line_hdr)
    tbl = [line_hdr, line_sep]
    for a, b, c, d in rows:
        tbl.append(f"{a:>{w1}} | {b:>{w2}} | {('$'+c) if c!='—' else '—':>{w3}} | {d:>{w4}}")

    out = [f"Slippage curve: {token_in} → {token_out}"]
    if mid:
        out.append(f"Baseline (mid): ${mid:,.2f} per 1{token_in}")
    out.append("")
    out.extend(tbl)
    update.message.reply_text(
        f"<pre>\n{chr(10).join(out)}\n</pre>", parse_mode=ParseMode.HTML
    )

# ---------- cooldowns ----------

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

# ---------- ping ----------

def cmd_ping(update: Update, context: CallbackContext):
    ip_txt = "unknown"
    try:
        with open("/bot/db/public_ip.txt", "r") as f:
            ip_txt = f.read().strip() or "unknown"
    except Exception:
        pass
    ver = os.getenv("TECBOT_VERSION", getattr(C, "TECBOT_VERSION", "v0.1.0-ops"))
    update.message.reply_text(f"pong · IP: {ip_txt} · {ver}")

# ---------- plan/dryrun (kept for internal visibility, not in BotFather) ----------

def cmd_plan(update: Update, context: CallbackContext):
    if planner is None or not hasattr(planner, "build_plan_snapshot"):
        update.message.reply_text("Plan error: planner module not available."); return
    try:
        snap = planner.build_plan_snapshot()
    except Exception as e:
        log.exception("plan failure")
        update.message.reply_text(f"Plan error: {e}"); return
    update.message.reply_text(render_plan(snap))

def cmd_dryrun(update: Update, context: CallbackContext):
    if not getattr(C, "DRYRUN_ENABLED", True):
        update.message.reply_text("Dry-run is disabled."); return
    if runner is None or not all(hasattr(runner, n) for n in ("build_dryrun","execute_action")):
        update.message.reply_text("Dry-run unavailable: runner hooks missing."); return
    try:
        results = runner.build_dryrun()
    except Exception as e:
        log.exception("dryrun failure")
        update.message.reply_text(f"Dry-run error: {e}"); return
    if not results:
        update.message.reply_text("Dry-run: no executable actions."); return

    text = render_dryrun(results)
    kb = [[InlineKeyboardButton(f"▶️ Execute {getattr(r,'action_id','?')}",
                                callback_data=f"exec:{getattr(r,'action_id','?')}")] for r in results]
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="exec_cancel")])
    update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)

def on_exec_button(update: Update, context: CallbackContext):
    q = update.callback_query; data = q.data or ""
    if not data.startswith("exec:"): q.answer(); return
    if not is_admin(q.from_user.id): q.answer("Not authorized.", show_alert=True); return
    aid = data.split(":",1)[1]
    q.edit_message_text(f"Confirm execution: Action #{aid}\nAre you sure?")
    q.edit_message_reply_markup(
        InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm", callback_data=f"exec_go:{aid}"),
             InlineKeyboardButton("❌ Abort", callback_data="exec_cancel")]
        ])
    )
    q.answer()

def on_exec_confirm(update: Update, context: CallbackContext):
    q = update.callback_query; data = q.data or ""
    if not data.startswith("exec_go:"): q.answer(); return
    if not is_admin(q.from_user.id): q.answer("Not authorized.", show_alert=True); return
    if runner is None or not hasattr(runner, "execute_action"):
        q.answer("Execution backend missing.", show_alert=True); return
    aid = data.split(":",1)[1]
    try:
        txr = runner.execute_action(aid)
        txh = getattr(txr, "tx_hash", "0x")
        filled = getattr(txr, "filled_text", "")
        gas_used = getattr(txr, "gas_used", "—")
        explorer = getattr(txr, "explorer_url", "")
        q.edit_message_text(
            f"✅ Executed {aid}\n{filled}\nGas used: {gas_used}\nTx: {txh}\n{explorer}".strip()
        )
        q.answer("Executed.")
    except Exception as e:
        q.edit_message_text(f"❌ Execution failed for {aid}\n{e}")
        q.answer("Failed.", show_alert=True)

def on_exec_cancel(update: Update, context: CallbackContext):
    q = update.callback_query
    q.edit_message_text("Canceled. No transaction sent.")
    q.answer()

# ============================================================================
# /trade — full flow (wallet → from → to → route → amount → slippage → quote → exec)
# ============================================================================

# Per-chat wizard state for trading
_TW: Dict[int, Dict[str, Any]] = {}

TREASURY_ADDR = "0x360c48a44f513b5781854588d2f1A40E90093c60"  # also reused in /withdraw

def _tw_state(chat_id: int) -> Dict[str, Any]:
    st = _TW.get(chat_id)
    if not st:
        st = {
            "wallet": None,
            "from": None,
            "to": None,
            "force_via": None,
            "route_tokens": None,   # ["1USDC","WONE","1ETH"]
            "route_fees": None,     # [500,3000]
            "amount": None,         # "1500.00"
            "slip_bps": None,       # int
            "pending_custom_amount": False,
            "pending_custom_slip": False,
        }
        _TW[chat_id] = st
    return st

def _tw_clear(chat_id: int):
    if chat_id in _TW:
        del _TW[chat_id]

def _kb_wallets() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("tecbot_usdc", callback_data="tw|w|tecbot_usdc"),
         InlineKeyboardButton("tecbot_sdai", callback_data="tw|w|tecbot_sdai")],
        [InlineKeyboardButton("tecbot_eth", callback_data="tw|w|tecbot_eth"),
         InlineKeyboardButton("tecbot_tec", callback_data="tw|w|tecbot_tec")],
        [InlineKeyboardButton("Cancel", callback_data="tw|x")]
    ]
    return InlineKeyboardMarkup(rows)

def _wallet_balance_for(sym: str, wallet_name: str) -> Optional[Decimal]:
    """
    Pull a single wallet's balance for a symbol from BL (balances).
    """
    if BL is None:
        return None
    try:
        table = BL.all_balances()  # { wallet_name: {sym: amt,...}, ... }
    except Exception:
        return None
    row = table.get(wallet_name, {})
    # handle ONE specially like balances does
    if sym.upper() in ["ONE", "WONE"]:
        return _resolve_one_value(row)
    val = row.get(sym, None)
    if val is None:
        # try strict/upper
        for k, v in row.items():
            if k.upper() == sym.upper():
                val = v
                break
    try:
        return Decimal(str(val)) if val is not None else None
    except Exception:
        return None

def _kb_tokens(kind: str) -> InlineKeyboardMarkup:
    # Show supported tokens
    syms = list(getattr(C, "TOKENS", {}).keys()) or ["ONE","1USDC","1sDAI","TEC","1ETH","WONE"]
    syms = [s.upper() for s in syms]
    # De-dupe + deterministic
    seen = []
    for s in syms:
        if s not in seen:
            seen.append(s)
    buttons: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for s in seen:
        row.append(InlineKeyboardButton(s, callback_data=f"tw|{kind}|{s}"))
        if len(row) == 3:
            buttons.append(row); row=[]
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("Back", callback_data="tw|back"),
                    InlineKeyboardButton("Cancel", callback_data="tw|x")])
    return InlineKeyboardMarkup(buttons)

def _humanize_route_list(from_sym: str, to_sym: str, force_via: Optional[str], ref_size: Decimal) -> List[Tuple[str, Dict[str,str], List[str], List[int]]]:
    """
    Build candidate route list (for UI).
    Returns list of tuples:
      (label_id, human_info, route_tokens, route_fees)

    human_info contains:
      "display" (tokens with fee %)
      "fee_total_pct"
      "impact_bps" (string or '—')
      "est_out" (string or '—')
    """
    # lazy import the finder so we don't explode if it's not present
    try:
        from app import route_finder as RF
    except Exception:
        import route_finder as RF  # type: ignore

    raw_paths = RF.candidates(from_sym, to_sym, force_via=force_via, max_hops=2, max_routes=3)
    out: List[Tuple[str, Dict[str,str], List[str], List[int]]] = []

    idx = 1
    for lp in raw_paths:
        # lp like ["1USDC","WONE@500","1ETH@3000"]
        # extract clean tokens + fee ints
        tokens_only: List[str] = []
        fees_only: List[int] = []

        prev = None
        for hop in lp:
            if "@" in hop:
                sym, raw = hop.split("@",1)
                tokens_only.append(sym)
                try: fees_only.append(int(raw))
                except: fees_only.append(3000)  # fallback
            else:
                tokens_only.append(hop)
            prev = hop

        # fix first token duplication: tokens_only currently repeats first token at index0 and again later logic, so ensure:
        # e.g. lp: ["1USDC","WONE@500","1ETH@3000"]
        # tokens_only result was ["1USDC","WONE","1ETH"]
        # fees_only        was [500,3000] good.

        # Build human readable
        human_core = RF.humanize_path(lp)
        display_path = human_core["display"]              # "1USDC → WONE@0.05% → 1ETH@0.30%"
        fee_total_pct = human_core["fee_total_pct"]       # "~0.35%"

        # we *could* pre-quote each route for a small reference amount:
        est_out_str = "—"
        impact_bps_str = "—"
        try:
            qd = _tw_quote_swap(
                wallet_key=None,  # just need quoting, no wallet checks
                token_in=from_sym,
                token_out=to_sym,
                route_tokens=tokens_only,
                route_fees=fees_only,
                amount_in=ref_size,
                slip_bps=None,
                check_allowance=False,
            )
            # qd should have "quote_out_human", "impact_bps"
            est_out_str = qd.get("quote_out_human","—")
            ibps = qd.get("impact_bps", None)
            if ibps is not None:
                impact_bps_str = f"{ibps:.2f} bps"
        except Exception:
            pass

        human_info = {
            "display": display_path,
            "fee_total_pct": fee_total_pct,
            "est_out": est_out_str,
            "impact_bps": impact_bps_str,
        }
        out.append((str(idx), human_info, tokens_only, fees_only))
        idx += 1

    return out

def _kb_routes_ui(from_sym: str, to_sym: str, force_via: Optional[str]) -> Tuple[str, InlineKeyboardMarkup, List[Tuple[str,Dict[str,str],List[str],List[int]]]]:
    """
    Build the route selection text and keyboard.
    Returns (header_text, keyboard, candidates_info_list)
    The callback_data will be "tw|route|<idx>"
    """
    header = f"Route for {from_sym} → {to_sym}\n"
    if force_via:
        header += f"Forced via {force_via}\n"
    else:
        header += "No direct pool — using intermediates if needed\n\n"

    # Build candidates and stash them so we can map idx -> path on click
    ref_amt = Decimal("100")  # small reference amount for preview impact
    cand_list = _humanize_route_list(from_sym, to_sym, force_via, ref_amt)

    lines = []
    kbs: List[List[InlineKeyboardButton]] = []

    if not cand_list:
        lines.append("No viable routes found.")
    else:
        for (idx, info, _rtoks, _rfees) in cand_list:
            # show each route like:
            # 1) 1USDC → WONE@0.05% → 1ETH@0.30%
            #    Impact ~11 bps | Fees ~0.35% | EstOut ~0.4321 1ETH
            l1 = f"{idx}) {info['display']}"
            l2 = (
                f"   Impact {info['impact_bps']} | "
                f"Fees {info['fee_total_pct']} | "
                f"EstOut {info['est_out']}"
            )
            lines.append(l1)
            lines.append(l2)
            kbs.append([InlineKeyboardButton(f"Use route {idx}", callback_data=f"tw|route|{idx}")])

    # force via WONE / 1sDAI
    kbs.append([
        InlineKeyboardButton("Force via WONE", callback_data="tw|force|WONE"),
        InlineKeyboardButton("Force via 1sDAI", callback_data="tw|force|1sDAI"),
    ])
    kbs.append([
        InlineKeyboardButton("Auto (best)", callback_data="tw|route|AUTO"),
    ])
    kbs.append([
        InlineKeyboardButton("Back", callback_data="tw|back"),
        InlineKeyboardButton("Cancel", callback_data="tw|x"),
    ])

    text_block = header + "\n".join(lines)
    return text_block, InlineKeyboardMarkup(kbs), cand_list

def _kb_amount_ui(avail: Optional[Decimal], sym: str) -> Tuple[str, InlineKeyboardMarkup]:
    amt_line = ""
    if avail is not None:
        amt_line = f"Available: { _fmt_amt(sym, avail) } {sym}\n"
    intro = f"Amount to spend (in {sym})\n{amt_line}"
    rows = [
        [InlineKeyboardButton("100", callback_data="tw|amt|100"),
         InlineKeyboardButton("250", callback_data="tw|amt|250"),
         InlineKeyboardButton("500", callback_data="tw|amt|500")],
        [InlineKeyboardButton("1,500", callback_data="tw|amt|1500"),
         InlineKeyboardButton("5,000", callback_data="tw|amt|5000"),
         InlineKeyboardButton("All", callback_data="tw|amt|ALL")],
        [InlineKeyboardButton("Custom…", callback_data="tw|amt|CUSTOM"),
         InlineKeyboardButton("Back", callback_data="tw|back"),
         InlineKeyboardButton("Cancel", callback_data="tw|x")],
    ]
    return intro, InlineKeyboardMarkup(rows)

def _kb_slip_ui() -> Tuple[str, InlineKeyboardMarkup]:
    intro = (
        "Price protection (max slippage)\n\n"
        "This is the MOST you're willing to lose to price movement\n"
        "between quote and execution.\n\n"
        "Example:\n"
        "30 bps = 0.30%\n"
    )
    rows = [
        [InlineKeyboardButton("10 bps (0.10%)", callback_data="tw|slip|10")],
        [InlineKeyboardButton("20 bps (0.20%)", callback_data="tw|slip|20")],
        [InlineKeyboardButton("30 bps (0.30%)", callback_data="tw|slip|30")],
        [InlineKeyboardButton("50 bps (0.50%)", callback_data="tw|slip|50")],
        [InlineKeyboardButton("Custom…", callback_data="tw|slip|CUSTOM"),
         InlineKeyboardButton("Back", callback_data="tw|back"),
         InlineKeyboardButton("Cancel", callback_data="tw|x")],
    ]
    return intro, InlineKeyboardMarkup(rows)

def _quote_confirmation_block(st: Dict[str,Any], qd: Dict[str,Any]) -> Tuple[str, InlineKeyboardMarkup]:
    """
    Build the review card and appropriate buttons based on allowance/slippage.
    qd is dict from _tw_quote_swap().
    """
    # qd must carry:
    # 'path_human'            (string: "1USDC → WONE@0.05% → 1ETH@0.30%")
    # 'amount_in_human'       ("1,500.00 1USDC")
    # 'quote_out_human'       ("0.4321 1ETH")
    # 'impact_bps'            (Decimal or float)
    # 'fee_total_pct'         ("~0.35%")
    # 'slip_text'             ("0.30% max → minOut 0.4308 1ETH")
    # 'gas_units'             (int)
    # 'gas_cost_one'          ("0.00421 ONE")
    # 'gas_cost_gwei_total'   ("~4,210 gwei")
    # 'allowance_ok'          (bool)
    # 'need_approval_amount'  ("1,500.00 1USDC") if not ok
    # 'nonce'                 (int)
    # 'slippage_ok'           (bool)
    # 'price_limit_text'      ("⚠ price already moved ..." or "")
    path_human = qd.get("path_human","?")
    amount_in_human = qd.get("amount_in_human","?")
    quote_out_human = qd.get("quote_out_human","?")
    imp_bps = qd.get("impact_bps", None)
    fee_total_pct = qd.get("fee_total_pct","?")
    slip_text = qd.get("slip_text","?")
    gas_units = qd.get("gas_units","?")
    gas_cost_one = qd.get("gas_cost_one","?")
    gas_cost_gwei_total = qd.get("gas_cost_gwei_total","?")
    nonce = qd.get("nonce","?")
    allowance_ok = qd.get("allowance_ok", False)
    need_approval_amount = qd.get("need_approval_amount","?")
    slippage_ok = qd.get("slippage_ok", True)
    price_limit_text = qd.get("price_limit_text","")

    lines = [
        f"Review Trade — {st.get('wallet','?')}",
        f"Path     : {path_human}",
        f"AmountIn : {amount_in_human}",
        f"QuoteOut : {quote_out_human}",
        f"Impact   : {imp_bps:.2f} bps" if imp_bps is not None else "Impact   : —",
        f"Fees     : {fee_total_pct} total pool fees",
        f"Slippage : {slip_text}",
        f"Gas Est  : {gas_units} gas",
        f"Cost     : {gas_cost_one}  ({gas_cost_gwei_total})",
        f"Nonce    : {nonce}",
    ]

    if not allowance_ok:
        lines.append("Allowance: NOT APPROVED")
        lines.append(f"Required : approve {need_approval_amount}")
    else:
        lines.append("Allowance: OK")

    if price_limit_text:
        lines.append(price_limit_text)

    text_block = "<pre>\n" + "\n".join(lines) + "\n</pre>"

    # Buttons:
    kb_rows: List[List[InlineKeyboardButton]] = []
    if not allowance_ok:
        # first do approval only
        kb_rows.append([
            InlineKeyboardButton("Approve spend first", callback_data="tw|approve|go"),
            InlineKeyboardButton("❌ Cancel", callback_data="tw|x"),
        ])
    elif not slippage_ok:
        kb_rows.append([
            InlineKeyboardButton("Change Slippage", callback_data="tw|back"),
            InlineKeyboardButton("❌ Cancel", callback_data="tw|x"),
        ])
    else:
        kb_rows.append([
            InlineKeyboardButton("✅ Execute Trade", callback_data="tw|exec|go"),
        ])
        kb_rows.append([
            InlineKeyboardButton("◀ Back", callback_data="tw|back"),
            InlineKeyboardButton("❌ Cancel", callback_data="tw|x"),
        ])

    return text_block, InlineKeyboardMarkup(kb_rows)

def cmd_trade(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    _tw_state(chat_id)  # init/reset state
    update.message.reply_text("Trade: choose a wallet", reply_markup=_kb_wallets())

def cb_trade(update: Update, context: CallbackContext):
    q = update.callback_query
    if not q or not q.data:
        return
    chat_id = q.message.chat_id
    st = _tw_state(chat_id)

    try:
        _, op, *rest = q.data.split("|")
    except Exception:
        q.answer("Invalid"); return

    # cancel
    if op == "x":
        _tw_clear(chat_id)
        q.edit_message_text("Canceled. No transaction sent.")
        return

    # wallet select
    if op == "w":
        st["wallet"] = rest[0]
        # next: pick FROM asset
        bal_line = f"From asset (what you are spending)\nWallet: {st['wallet']}"
        q.edit_message_text(bal_line, reply_markup=_kb_tokens("from"))
        return

    # FROM asset
    if op == "from":
        st["from"] = rest[0]
        st["to"] = None
        st["route_tokens"] = None
        st["route_fees"] = None
        st["force_via"] = None
        q.edit_message_text(
            "To asset (what you want to receive)\n"
            "Note: we'll route through other tokens if no direct pool.",
            reply_markup=_kb_tokens("to"),
        )
        return

    # TO asset
    if op == "to":
        st["to"] = rest[0]
        # route selection screen
        header, kb, cand_list = _kb_routes_ui(st["from"], st["to"], st["force_via"])
        # stash candidate list so we can resolve 'Use route N'
        st["cand_list"] = cand_list
        q.edit_message_text(header, reply_markup=kb)
        return

    # force route via WONE / 1sDAI
    if op == "force":
        choice = rest[0]
        st["force_via"] = choice
        header, kb, cand_list = _kb_routes_ui(st["from"], st["to"], st["force_via"])
        st["cand_list"] = cand_list
        q.edit_message_text(header, reply_markup=kb)
        return

    # route pick
    if op == "route":
        sel = rest[0]
        if sel == "AUTO":
            # If AUTO, pick first candidate if available
            cand_list = st.get("cand_list", [])
            if cand_list:
                _id, _info, rtoks, rfees = cand_list[0]
                st["route_tokens"] = rtoks
                st["route_fees"] = rfees
            else:
                st["route_tokens"] = None
                st["route_fees"] = None
        else:
            # find by idx
            cand_list = st.get("cand_list", [])
            match = [c for c in cand_list if c[0] == sel]
            if match:
                _id, _info, rtoks, rfees = match[0]
                st["route_tokens"] = rtoks
                st["route_fees"] = rfees

        # move to AMOUNT
        avail = _wallet_balance_for(st["from"], st["wallet"])
        intro, kb_amt = _kb_amount_ui(avail, st["from"])
        q.edit_message_text(intro, reply_markup=kb_amt)
        return

    # amount pick
    if op == "amt":
        sel = rest[0]
        if sel == "CUSTOM":
            st["pending_custom_amount"] = True
            q.answer("Send a number like 1500 or 1500.25")
            return
        if sel == "ALL":
            avail = _wallet_balance_for(st["from"], st["wallet"])
            st["amount"] = str(avail) if avail is not None else "0"
        else:
            st["amount"] = sel
        # go slippage
        intro, kb_slip = _kb_slip_ui()
        q.edit_message_text(intro, reply_markup=kb_slip)
        return

    # slippage pick
    if op == "slip":
        sel = rest[0]
        if sel == "CUSTOM":
            st["pending_custom_slip"] = True
            q.answer("Send integer bps (e.g. 35 for 0.35%)")
            return
        st["slip_bps"] = int(sel)
        # now build live quote + confirmation card
        qd = _tw_quote_swap(
            wallet_key=st["wallet"],
            token_in=st["from"],
            token_out=st["to"],
            route_tokens=st["route_tokens"],
            route_fees=st["route_fees"],
            amount_in=Decimal(str(st["amount"])),
            slip_bps=st["slip_bps"],
            check_allowance=True,
        )
        st["last_quote"] = qd
        text_block, kb_conf = _quote_confirmation_block(st, qd)
        q.edit_message_text(text_block, reply_markup=kb_conf, parse_mode=ParseMode.HTML)
        return

    # approve (not unlimited, only trade amount)
    if op == "approve":
        if not is_admin(q.from_user.id):
            q.answer("Not authorized.", show_alert=True); return
        # send approval tx
        try:
            _tw_send_approval(
                wallet_key=st["wallet"],
                token_in=st["from"],
                amount_in=Decimal(str(st["amount"])),
                route_tokens=st["route_tokens"],
                route_fees=st["route_fees"],
            )
            # after approval: requote
            qd = _tw_quote_swap(
                wallet_key=st["wallet"],
                token_in=st["from"],
                token_out=st["to"],
                route_tokens=st["route_tokens"],
                route_fees=st["route_fees"],
                amount_in=Decimal(str(st["amount"])),
                slip_bps=st["slip_bps"],
                check_allowance=True,
            )
            st["last_quote"] = qd
            text_block, kb_conf = _quote_confirmation_block(st, qd)
            q.edit_message_text(text_block, reply_markup=kb_conf, parse_mode=ParseMode.HTML)
            q.answer("Approval submitted.")
        except Exception as e:
            q.edit_message_text(f"Approval failed: {e}")
            q.answer("Approval failed.", show_alert=True)
        return

    # execute final trade
    if op == "exec":
        if not is_admin(q.from_user.id):
            q.answer("Not authorized.", show_alert=True); return

        # final re-quote & send
        qd = _tw_quote_swap(
            wallet_key=st["wallet"],
            token_in=st["from"],
            token_out=st["to"],
            route_tokens=st["route_tokens"],
            route_fees=st["route_fees"],
            amount_in=Decimal(str(st["amount"])),
            slip_bps=st["slip_bps"],
            check_allowance=True,
        )
        st["last_quote"] = qd

        if (not qd.get("allowance_ok", False)) or (not qd.get("slippage_ok", True)):
            # show card again with warnings
            text_block, kb_conf = _quote_confirmation_block(st, qd)
            q.edit_message_text(text_block, reply_markup=kb_conf, parse_mode=ParseMode.HTML)
            q.answer("Cannot execute (allowance/slippage).", show_alert=True)
            return

        try:
            txr = _tw_send_swap(
                wallet_key=st["wallet"],
                token_in=st["from"],
                token_out=st["to"],
                route_tokens=st["route_tokens"],
                route_fees=st["route_fees"],
                amount_in=Decimal(str(st["amount"])),
                slip_bps=st["slip_bps"],
            )
            # txr should contain:
            # 'tx_hash', 'gas_used', 'gas_cost_one', 'gas_cost_gwei_total',
            # 'filled_in_human', 'filled_out_human'
            msg_lines = [
                "✅ Trade sent",
                "",
                f"Wallet : {st['wallet']}",
                f"Spent  : {txr.get('filled_in_human','?')}",
                f"Got    : {txr.get('filled_out_human','?')} (minOut enforced)",
                "",
                f"Gas used: {txr.get('gas_used','?')}",
                f"Cost    : {txr.get('gas_cost_one','?')} ({txr.get('gas_cost_gwei_total','?')})",
                "",
                "Tx hash:",
                f"{txr.get('tx_hash','0x')}",
            ]
            explorer = txr.get("explorer_url","")
            if explorer:
                msg_lines.append("")
                msg_lines.append(explorer)
            q.edit_message_text("\n".join(msg_lines))
            q.answer("Executed.")
        except Exception as e:
            q.edit_message_text(f"❌ Trade failed\n{e}")
            q.answer("Trade failed.", show_alert=True)

        _tw_clear(chat_id)
        return

    # back: step back in wizard
    if op == "back":
        # priority unwind: slippage -> amount -> route -> to -> from -> wallet
        if st.get("slip_bps") is not None:
            st["slip_bps"] = None
            intro, kb_slip = _kb_slip_ui()
            q.edit_message_text(intro, reply_markup=kb_slip)
            return
        if st.get("amount") is not None:
            st["amount"] = None
            avail = _wallet_balance_for(st["from"], st["wallet"])
            intro, kb_amt = _kb_amount_ui(avail, st["from"])
            q.edit_message_text(intro, reply_markup=kb_amt)
            return
        if st.get("route_tokens") is not None or st.get("to"):
            st["route_tokens"] = None
            st["route_fees"] = None
            header, kb, cand_list = _kb_routes_ui(st["from"], st["to"], st["force_via"])
            st["cand_list"] = cand_list
            q.edit_message_text(header, reply_markup=kb)
            return
        if st.get("to"):
            st["to"] = None
            q.edit_message_text(
                "To asset (what you want to receive)\n"
                "Note: we'll route through other tokens if no direct pool.",
                reply_markup=_kb_tokens("to"),
            )
            return
        if st.get("from"):
            st["from"] = None
            q.edit_message_text(
                f"From asset (what you are spending)\nWallet: {st['wallet']}",
                reply_markup=_kb_tokens("from"),
            )
            return
        if st.get("wallet"):
            st["wallet"] = None
            q.edit_message_text("Trade: choose a wallet", reply_markup=_kb_wallets())
            return

        q.edit_message_text("Trade: choose a wallet", reply_markup=_kb_wallets())
        return

    q.answer("Unhandled")

def msg_text_trade(update: Update, context: CallbackContext):
    """
    Capture custom amount and custom slippage bps when requested.
    """
    chat_id = update.effective_chat.id
    st = _tw_state(chat_id)
    txt = (update.message.text or "").strip()

    # custom amount
    if st.get("pending_custom_amount"):
        st["pending_custom_amount"] = False
        st["amount"] = txt
        intro, kb_slip = _kb_slip_ui()
        update.message.reply_text(intro, reply_markup=kb_slip)
        return

    # custom slippage
    if st.get("pending_custom_slip"):
        st["pending_custom_slip"] = False
        try:
            st["slip_bps"] = int(txt)
        except Exception:
            update.message.reply_text("Send integer bps (e.g. 35 for 0.35%)")
            return
        # now build quote + confirmation
        qd = _tw_quote_swap(
            wallet_key=st["wallet"],
            token_in=st["from"],
            token_out=st["to"],
            route_tokens=st["route_tokens"],
            route_fees=st["route_fees"],
            amount_in=Decimal(str(st["amount"])),
            slip_bps=st["slip_bps"],
            check_allowance=True,
        )
        st["last_quote"] = qd
        text_block, kb_conf = _quote_confirmation_block(st, qd)
        update.message.reply_html(text_block, reply_markup=kb_conf)
        return

    # if message isn't part of wizard inputs, just log it
    _log_update(update, context)

# ============================================================================
# /withdraw — wallet -> asset -> amount -> confirm -> send
# ============================================================================

_WD: Dict[int, Dict[str,Any]] = {}

def _wd_state(chat_id: int) -> Dict[str,Any]:
    st = _WD.get(chat_id)
    if not st:
        st = {
            "wallet": None,
            "asset": None,
            "amount": None,
            "pending_custom_amount": False,
        }
        _WD[chat_id] = st
    return st

def _wd_clear(chat_id: int):
    if chat_id in _WD:
        del _WD[chat_id]

def _kb_wd_wallets() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("tecbot_usdc", callback_data="wd|w|tecbot_usdc"),
         InlineKeyboardButton("tecbot_sdai", callback_data="wd|w|tecbot_sdai")],
        [InlineKeyboardButton("tecbot_eth", callback_data="wd|w|tecbot_eth"),
         InlineKeyboardButton("tecbot_tec", callback_data="wd|w|tecbot_tec")],
        [InlineKeyboardButton("Cancel", callback_data="wd|x")]
    ]
    return InlineKeyboardMarkup(rows)

def _kb_wd_assets(wallet_name: str) -> Tuple[str, InlineKeyboardMarkup]:
    # show balances for that wallet so you know what's available
    table = {}
    try:
        if BL is not None:
            table = BL.all_balances().get(wallet_name, {})
    except Exception:
        table = {}
    # gather nice summary:
    lines = [f"Which asset to withdraw from {wallet_name}?",
             f"Destination:\n{TREASURY_ADDR}",
             "Balance:"]
    for k,v in table.items():
        amt = _fmt_amt(k, v)
        lines.append(f"{k:<7} {amt}")
    # build buttons for common assets
    syms = ["1USDC","1sDAI","TEC","1ETH","ONE","WONE"]
    row = []
    btn_rows: List[List[InlineKeyboardButton]] = []
    for s in syms:
        row.append(InlineKeyboardButton(s, callback_data=f"wd|asset|{s}"))
        if len(row)==3:
            btn_rows.append(row); row=[]
    if row: btn_rows.append(row)
    btn_rows.append([
        InlineKeyboardButton("Back", callback_data="wd|back"),
        InlineKeyboardButton("Cancel", callback_data="wd|x"),
    ])
    return "\n".join(lines), InlineKeyboardMarkup(btn_rows)

def _kb_wd_amount_ui(avail: Optional[Decimal], sym: str) -> Tuple[str, InlineKeyboardMarkup]:
    amt_line = ""
    if avail is not None:
        amt_line = f"Available: { _fmt_amt(sym, avail) } {sym}\n"
    intro = (
        f"Amount to withdraw ({sym})\n"
        f"Destination:\n{TREASURY_ADDR}\n"
        f"{amt_line}"
    )
    rows = [
        [InlineKeyboardButton("100", callback_data="wd|amt|100"),
         InlineKeyboardButton("250", callback_data="wd|amt|250"),
         InlineKeyboardButton("500", callback_data="wd|amt|500")],
        [InlineKeyboardButton("1,000", callback_data="wd|amt|1000"),
         InlineKeyboardButton("All", callback_data="wd|amt|ALL")],
        [InlineKeyboardButton("Custom…", callback_data="wd|amt|CUSTOM"),
         InlineKeyboardButton("Back", callback_data="wd|back"),
         InlineKeyboardButton("Cancel", callback_data="wd|x")],
    ]
    return intro, InlineKeyboardMarkup(rows)

def _wd_confirm_block(st: Dict[str,Any], qd: Dict[str,Any]) -> Tuple[str, InlineKeyboardMarkup]:
    """
    qd from _wd_quote_transfer, containing:
      'gas_units','gas_cost_one','gas_cost_gwei_total','nonce'
    """
    wallet_key = st.get("wallet","?")
    sym = st.get("asset","?")
    amt = st.get("amount","?")
    gas_units = qd.get("gas_units","?")
    cost_one = qd.get("gas_cost_one","?")
    cost_gwei = qd.get("gas_cost_gwei_total","?")
    nonce = qd.get("nonce","?")

    lines = [
        "Confirm Withdraw",
        "",
        f"Wallet      : {wallet_key}",
        f"Asset       : {sym}",
        f"Amount      : {amt}",
        f"To          : {TREASURY_ADDR}",
        "",
        f"Gas Est     : {gas_units} gas",
        f"Cost        : {cost_one} ({cost_gwei})",
        f"Nonce       : {nonce}",
    ]
    text_block = "<pre>\n" + "\n".join(lines) + "\n</pre>"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Send Withdrawal", callback_data="wd|exec|go")],
        [InlineKeyboardButton("◀ Back", callback_data="wd|back"),
         InlineKeyboardButton("❌ Cancel", callback_data="wd|x")],
    ])
    return text_block, kb

def cmd_withdraw(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    _wd_state(chat_id)
    txt = (
        "Withdraw funds to treasury:\n\n"
        f"Destination:\n{TREASURY_ADDR}\n\n"
        "Select source wallet:"
    )
    update.message.reply_text(txt, reply_markup=_kb_wd_wallets())

def cb_withdraw(update: Update, context: CallbackContext):
    q = update.callback_query
    if not q or not q.data:
        return
    chat_id = q.message.chat_id
    st = _wd_state(chat_id)

    try:
        _, op, *rest = q.data.split("|")
    except Exception:
        q.answer("Invalid"); return

    # cancel
    if op == "x":
        _wd_clear(chat_id)
        q.edit_message_text("Withdrawal canceled.")
        return

    # wallet
    if op == "w":
        st["wallet"] = rest[0]
        txt, kb = _kb_wd_assets(st["wallet"])
        q.edit_message_text(txt, reply_markup=kb)
        return

    # asset
    if op == "asset":
        st["asset"] = rest[0]
        # ask amount
        avail = _wallet_balance_for(st["asset"], st["wallet"])
        intro, kb_amt = _kb_wd_amount_ui(avail, st["asset"])
        q.edit_message_text(intro, reply_markup=kb_amt)
        return

    # amount
    if op == "amt":
        sel = rest[0]
        if sel == "CUSTOM":
            st["pending_custom_amount"] = True
            q.answer("Send a number like 1000 or 1000.25")
            return
        if sel == "ALL":
            avail = _wallet_balance_for(st["asset"], st["wallet"])
            st["amount"] = str(avail) if avail is not None else "0"
        else:
            st["amount"] = sel

        # build confirm (quote gas/nonce)
        qd = _wd_quote_transfer(
            wallet_key=st["wallet"],
            token_sym=st["asset"],
            amount=Decimal(str(st["amount"])),
            to_addr=TREASURY_ADDR,
        )
        st["last_quote"] = qd
        text_block, kb_conf = _wd_confirm_block(st, qd)
        q.edit_message_text(text_block, reply_markup=kb_conf, parse_mode=ParseMode.HTML)
        return

    # exec withdrawal
    if op == "exec":
        # require admin
        if not is_admin(q.from_user.id):
            q.answer("Not authorized.", show_alert=True); return

        qd = _wd_quote_transfer(
            wallet_key=st["wallet"],
            token_sym=st["asset"],
            amount=Decimal(str(st["amount"])),
            to_addr=TREASURY_ADDR,
        )

        try:
            txr = _wd_send_transfer(
                wallet_key=st["wallet"],
                token_sym=st["asset"],
                amount=Decimal(str(st["amount"])),
                to_addr=TREASURY_ADDR,
            )
            msg_lines = [
                "✅ Withdrawal sent",
                "",
                f"From  : {st['wallet']}",
                f"Asset : {st['asset']}",
                f"Amount: {st['amount']}",
                f"To    : {TREASURY_ADDR}",
                "",
                f"Gas used: {txr.get('gas_used','?')}",
                f"Cost    : {txr.get('gas_cost_one','?')} ({txr.get('gas_cost_gwei_total','?')})",
                "",
                "Tx hash:",
                f"{txr.get('tx_hash','0x')}",
            ]
            explorer = txr.get("explorer_url","")
            if explorer:
                msg_lines.append("")
                msg_lines.append(explorer)
            q.edit_message_text("\n".join(msg_lines))
            q.answer("Withdraw sent.")
        except Exception as e:
            q.edit_message_text(f"❌ Withdrawal failed\n{e}")
            q.answer("Withdrawal failed.", show_alert=True)

        _wd_clear(chat_id)
        return

    # back in withdraw flow
    if op == "back":
        if st.get("amount") is not None:
            st["amount"] = None
            avail = _wallet_balance_for(st["asset"], st["wallet"])
            intro, kb_amt = _kb_wd_amount_ui(avail, st["asset"])
            q.edit_message_text(intro, reply_markup=kb_amt)
            return
        if st.get("asset") is not None:
            st["asset"] = None
            txt, kb = _kb_wd_assets(st["wallet"])
            q.edit_message_text(txt, reply_markup=kb)
            return
        if st.get("wallet") is not None:
            st["wallet"] = None
            txt = (
                "Withdraw funds to treasury:\n\n"
                f"Destination:\n{TREASURY_ADDR}\n\n"
                "Select source wallet:"
            )
            q.edit_message_text(txt, reply_markup=_kb_wd_wallets())
            return
        q.edit_message_text("Withdraw canceled.")
        return

    q.answer("Unhandled")

def msg_text_withdraw(update: Update, context: CallbackContext):
    """
    Capture custom amount for withdraw.
    """
    chat_id = update.effective_chat.id
    st = _wd_state(chat_id)
    txt = (update.message.text or "").strip()

    if st.get("pending_custom_amount"):
        st["pending_custom_amount"] = False
        st["amount"] = txt
        qd = _wd_quote_transfer(
            wallet_key=st["wallet"],
            token_sym=st["asset"],
            amount=Decimal(str(st["amount"])),
            to_addr=TREASURY_ADDR,
        )
        st["last_quote"] = qd
        text_block, kb_conf = _wd_confirm_block(st, qd)
        update.message.reply_html(text_block, reply_markup=kb_conf)
        return

    _log_update(update, context)

# ============================================================================
# PLACEHOLDER LOW-LEVEL HOOKS
#
# You must connect these to your chain-specific logic.
# They are used by /trade and /withdraw.
# ============================================================================

def _tw_quote_swap(
    wallet_key: Optional[str],
    token_in: str,
    token_out: str,
    route_tokens: Optional[List[str]],
    route_fees: Optional[List[int]],
    amount_in: Decimal,
    slip_bps: Optional[int],
    check_allowance: bool,
) -> Dict[str,Any]:
    """
    Build quote details for confirmation step.
    You already have similar logic in runner.build_dryrun() to compute:
      - amount_in_human
      - quote_out_human
      - impact_bps
      - minOut based on slip_bps
      - gas estimate, gas cost in ONE
      - allowance_ok, nonce
      - friendly path string with fee % and fee_total_pct
    Return a dict with keys consumed by _quote_confirmation_block().
    """
    # TODO: wire to your quoting / preview logic
    # The stub below is to keep the bot from crashing if run before wiring.
    return {
        "path_human": f"{token_in} → ... → {token_out}",
        "amount_in_human": f"{amount_in} {token_in}",
        "quote_out_human": f"~? {token_out}",
        "impact_bps": Decimal("0"),
        "fee_total_pct": "~0.00%",
        "slip_text": f"{(slip_bps or 0)/100:.2f}% max → minOut ? {token_out}",
        "gas_units": 210000,
        "gas_cost_one": "0.00 ONE",
        "gas_cost_gwei_total": "~0 gwei",
        "allowance_ok": True if not check_allowance else False,
        "need_approval_amount": f"{amount_in} {token_in}",
        "nonce": 0,
        "slippage_ok": True,
        "price_limit_text": "",
    }

def _tw_send_approval(
    wallet_key: str,
    token_in: str,
    amount_in: Decimal,
    route_tokens: Optional[List[str]],
    route_fees: Optional[List[int]],
):
    """
    Send ERC20 approve() for exactly 'amount_in' of token_in to router,
    NOT unlimited.
    """
    # TODO: call your approval tx builder/sender
    return

def _tw_send_swap(
    wallet_key: str,
    token_in: str,
    token_out: str,
    route_tokens: Optional[List[str]],
    route_fees: Optional[List[int]],
    amount_in: Decimal,
    slip_bps: int,
) -> Dict[str,Any]:
    """
    Execute the actual swap now.
    Must:
      - re-quote,
      - apply slip_bps => amountOutMin,
      - sign and send tx,
      - return receipt fields.
    """
    # TODO: call your trade executor / runner.execute_action equivalent
    return {
        "tx_hash": "0x...",
        "filled_in_human": f"{amount_in} {token_in}",
        "filled_out_human": f"~? {token_out}",
        "gas_used": 210000,
        "gas_cost_one": "0.00 ONE",
        "gas_cost_gwei_total": "~0 gwei",
        "explorer_url": "",
    }

def _wd_quote_transfer(
    wallet_key: str,
    token_sym: str,
    amount: Decimal,
    to_addr: str,
) -> Dict[str,Any]:
    """
    Quote gas / nonce for a withdrawal transfer.
    """
    # TODO: call wallet/runner logic to estimate transfer tx
    return {
        "gas_units": 85000,
        "gas_cost_one": "0.00167 ONE",
        "gas_cost_gwei_total": "~1,670 gwei",
        "nonce": 0,
    }

def _wd_send_transfer(
    wallet_key: str,
    token_sym: str,
    amount: Decimal,
    to_addr: str,
) -> Dict[str,Any]:
    """
    Send the actual withdrawal transfer (ERC20 transfer or native ONE).
    """
    # TODO: sign+send transfer
    return {
        "tx_hash": "0x...",
        "gas_used": 86000,
        "gas_cost_one": "0.00169 ONE",
        "gas_cost_gwei_total": "~1,690 gwei",
        "explorer_url": "",
    }

# ---------- main ----------

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or getattr(C, "TELEGRAM_BOT_TOKEN", None)
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN not set")

    up = Updater(token=token, use_context=True)
    dp = up.dispatcher

    # core commands
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("help", cmd_help))
    dp.add_handler(CommandHandler("version", cmd_version))
    dp.add_handler(CommandHandler("sanity", cmd_sanity))
    dp.add_handler(CommandHandler("assets", cmd_assets))
    dp.add_handler(CommandHandler("prices", cmd_prices))
    dp.add_handler(CommandHandler("balances", cmd_balances))
    dp.add_handler(CommandHandler("slippage", cmd_slippage, pass_args=True))
    dp.add_handler(CommandHandler("ping", cmd_ping))
    dp.add_handler(CommandHandler("cooldowns", cmd_cooldowns, pass_args=True))

    # old planner/runner (still callable manually)
    dp.add_handler(CommandHandler("plan", cmd_plan))
    dp.add_handler(CommandHandler("dryrun", cmd_dryrun))

    # trade wizard
    dp.add_handler(CommandHandler("trade", cmd_trade))
    dp.add_handler(CallbackQueryHandler(cb_trade, pattern=r"^tw\|"))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, msg_text_trade), group=1)

    # withdraw wizard
    dp.add_handler(CommandHandler("withdraw", cmd_withdraw))
    dp.add_handler(CallbackQueryHandler(cb_withdraw, pattern=r"^wd\|"))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, msg_text_withdraw), group=2)

    # existing exec callbacks for dryrun-based actions
    dp.add_handler(CallbackQueryHandler(on_exec_button, pattern=r"^exec:[A-Za-z0-9_\-]+$"))
    dp.add_handler(CallbackQueryHandler(on_exec_confirm, pattern=r"^exec_go:[A-Za-z0-9_\-]+$"))
    dp.add_handler(CallbackQueryHandler(on_exec_cancel, pattern=r"^exec_cancel$"))

    dp.add_error_handler(_log_error)
    # group=-1 for catchall logging of other updates
    dp.add_handler(MessageHandler(Filters.all, _log_update), group=-1)

    log.info("Handlers registered: /start /help /version /sanity /assets /prices /balances /slippage /ping /cooldowns /trade /withdraw (/plan /dryrun internal)")
    up.start_polling(clean=True)
    log.info("Telegram bot started")
    up.idle()

if __name__ == "__main__":
    main()

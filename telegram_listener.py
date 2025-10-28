#!/usr/bin/env python3
# TECBot Telegram Listener — now with /trade and /withdraw integration
#
# IMPORTANT:
#   - Existing commands (/prices, /balances, /slippage, /dryrun, etc.) are preserved.
#   - /plan is still registered but you said you'll hide it from BotFather later.
#   - /trade now calls runner.build_manual_quote() for honest quotes.
#   - /trade execute calls runner.execute_manual_quote() (admin-gated).
#   - /withdraw scaffold included (admin-gated send).
#
# You MUST finish the TODOs inside runner._prepare_manual_trade_for_wallet()
# and runner.execute_manual_quote() for gas/allowance/nonce/tx broadcast.
#
import os, sys, logging, subprocess, shlex
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Optional, List, Dict

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
    import config as C  # type: ignore
    log.info("Loaded config from root config")

# optional modules (prices, balances, slippage)
PR = BL = SL = None
try:
    from app import prices as PR      # on-chain Quoter
    log.info("Loaded prices from app.prices")
except Exception as e:
    log.warning("prices module not available: %s", e)

try:
    from app import balances as BL    # ERC20 + native ONE
    log.info("Loaded balances from app.balances")
except Exception as e:
    log.warning("balances module not available: %s", e)

try:
    from app import slippage as SL    # real slippage calc
    log.info("Loaded slippage from app.slippage")
except Exception as e:
    log.warning("slippage module not available: %s", e)

# optional planner/runner
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

# optional Coinbase spot
def _coinbase_eth() -> Optional[Decimal]:
    try:
        import coinbase_client
        val = coinbase_client.fetch_eth_usd_price()
        return Decimal(str(val)) if val is not None else None
    except Exception:
        return None

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

# Money formatting (USD)
def _fmt_money(x: Optional[Decimal]) -> str:
    if x is None:
        return "—"
    x = Decimal(x)
    if x >= 1000:
        return f"${x:,.2f}"
    if x >= 1:
        return f"${x:,.5f}".rstrip("0").rstrip(".") if "." in f"{x:.5f}" else f"${x:.5f}"
    return f"${x:,.5f}"

# Amount formatting for balances (per symbol rule)
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

# Symbol normalize for routing vs display
def _sym_for_route(sym: str) -> str:
    s = sym.upper()
    if s == "ONE":
        return "WONE"
    return s

def _sym_for_display(sym: str) -> str:
    s = sym.upper()
    if s == "WONE":
        return "ONE"
    return s

# ---------- Logging helpers ----------
def _log_update(update: Update, context: CallbackContext):
    try:
        uid = update.effective_user.id if update.effective_user else "?"
        txt = update.effective_message.text if update.effective_message else "<non-text>"
        log.info(f"UPDATE from {uid}: {txt}")
    except Exception:
        pass

def _log_error(update: object, context: CallbackContext):
    log.exception("Handler error")

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
        lines.append(f"Impact   : {imp:.2f} bps" if imp is not None else "Impact   : —")
        lines.append(f"Slippage : {slip} bps → minOut {mino}" if slip is not None else f"minOut   : {mino}")
        lines.append(
            f"Gas Est  : {gas}\n"
            f"Allowance: {'OK' if allow else 'NEEDED'}\n"
            f"Nonce    : {nonce}\n"
            f"Would send:\n{txp}\n"
        )
    return "\n".join(lines).strip()

# ---------- Commands ----------
def cmd_start(update: Update, context: CallbackContext):
    update.message.reply_text(
        "TECBot online.\n"
        "Try: /help\n"
        "Core: /trade /withdraw /balances /prices\n"
        "Debug: /dryrun /ping /sanity\n"
    )

def cmd_help(update: Update, context: CallbackContext):
    update.message.reply_text(
        "Commands:\n"
        "  /ping — health check\n"
        "  /trade — manual trade (wallet, route, size, slippage, execute)\n"
        "  /withdraw — withdraw funds to treasury wallet\n"
        "  /balances — per-wallet balances (ONE, USDC, ETH, TEC, sDAI)\n"
        "  /prices — on-chain quotes in USDC\n"
        "  /slippage <IN> [AMOUNT] [OUT] — impact curve\n"
        "  /cooldowns [bot|route] — show cooldowns\n"
        "  /version — running bot version\n"
        "  /sanity — config/modules sanity\n"
        "  /assets — configured tokens & wallets\n"
        "  /dryrun — strategy dry-run (internal)\n"
        "  /plan — strategy planner preview (internal)\n"
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

# ---- BALANCES ----
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

# ---- price helpers ----
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

        lp_str = _fmt_money(price).rjust(w_lp)
        basis_str = f"{basis:.5f}".rjust(w_basis)
        slip_str = slip_txt.rjust(w_slip)
        lines.append(f"{s:<{w_asset}} | {lp_str} | {basis_str} | {slip_str} | {route_text:<{w_route}}")

    eth_lp_display = None
    try:
        eth_lp_display = next((Decimal(lines[i].split("|")[1].strip().replace("$","").replace(",",""))
                               for i in range(len(lines))
                               if lines[i].startswith("1ETH ")), None)
    except Exception:
        eth_lp_display = None

    lines += ["", "ETH: Harmony LP vs Coinbase"]
    lines.append(f"  LP:       {_fmt_money(eth_lp_display)}")
    lines.append(f"  Coinbase: {_fmt_money(cb_eth)}")
    if eth_lp_display is not None and cb_eth not in (None, Decimal(0)):
        try:
            diff = (Decimal(eth_lp_display) - Decimal(cb_eth)) / Decimal(cb_eth) * Decimal(100)
            lines.append(f"  Diff:     {diff:+.2f}%")
        except Exception:
            pass

    update.message.reply_text(f"<pre>\n{chr(10).join(lines)}\n</pre>", parse_mode=ParseMode.HTML)

# ---- SLIPPAGE TABLE ----
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
            rows.append((f"{usdc:,.0f}", f"{est_in:.6f}", f"{eff:,.2f}" if eff else "—", f"{slip:+.2f}%" if slip is not None else "—"))
        except Exception:
            rows.append((f"{usdc:,.0f}", "—", "—", "—"))

    col1, col2, col3, col4 = "Size (USDC)", "Amount In (sym)", "Eff. Price", "Slippage vs mid"
    line_hdr = f"{col1:>{w1}} | {col2:>{w2}} | {col3:>{w3}} | {col4:>{w4}}"
    line_sep = "-" * len(line_hdr)
    tbl = [line_hdr, line_sep]
    for a,b,c,d in rows:
        tbl.append(f"{a:>{w1}} | {b:>{w2}} | {('$'+c) if c!='—' else '—':>{w3}} | {d:>{w4}}")

    out = [f"Slippage curve: {token_in} → {token_out}"]
    if mid:
        out.append(f"Baseline (mid): ${mid:,.2f} per 1{token_in}")
    out.append("")
    out.extend(tbl)
    update.message.reply_text(f"<pre>\n{chr(10).join(out)}\n</pre>", parse_mode=ParseMode.HTML)

# ---------- Core bot health ----------
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

# -----------------------------------------------------------------------------
# /trade wizard state mgmt
# -----------------------------------------------------------------------------

# We track per-user temp state in memory (simple dict).
_TRADE_STATE: Dict[int, Dict[str, str]] = {}

def cmd_trade(update: Update, context: CallbackContext):
    uid = update.effective_user.id
    _TRADE_STATE[uid] = {
        "step": "wallet",
        "wallet": "",
        "from": "",
        "to": "",
        "amount": "",
        "slip_bps": str(getattr(C, "SLIPPAGE_DEFAULT_BPS", 30)),  # default
    }

    # wallet choices = configured bot wallets
    wallets = getattr(C, "WALLETS", {})
    kb = [[InlineKeyboardButton(name, callback_data=f"tw_wallet:{name}")]
          for name in sorted(wallets.keys())]
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="tw_cancel")])

    update.message.reply_text(
        "Select wallet:",
        reply_markup=InlineKeyboardMarkup(kb)
    )

def _tw_reply_edit(q, text, kb=None, html=False):
    try:
        q.edit_message_text(text, parse_mode=(ParseMode.HTML if html else None))
        if kb:
            q.edit_message_reply_markup(InlineKeyboardMarkup(kb))
        else:
            q.edit_message_reply_markup(None)
    except Exception:
        # fallback for messages that cannot be edited (first step)
        q.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb) if kb else None, parse_mode=(ParseMode.HTML if html else None))

def _tw_require_state(uid):
    st = _TRADE_STATE.get(uid)
    if not st:
        st = {
            "step": "wallet",
            "wallet": "",
            "from": "",
            "to": "",
            "amount": "",
            "slip_bps": str(getattr(C, "SLIPPAGE_DEFAULT_BPS", 30)),
        }
        _TRADE_STATE[uid] = st
    return st

def _tw_assets_keyboard(uid, which):
    # which in ("from","to")
    # show core symbols you support
    # NOTE: show ONE (user-facing), not WONE
    syms = ["ONE", "1USDC", "1sDAI", "TEC", "1ETH"]
    kb = []
    for s in syms:
        kb.append([InlineKeyboardButton(s, callback_data=f"tw_{which}:{s}")])
    kb.append([InlineKeyboardButton("⬅ Back", callback_data="tw_back_wallet" if which=="from" else "tw_back_from")])
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="tw_cancel")])
    return kb

def _tw_slip_keyboard(uid):
    # Offer preset slippage options in human terms.
    # We store them as bps.
    choices = [
        ("0.10% max", 10),
        ("0.50% max", 50),
        ("1.00% max", 100),
        ("Use default", getattr(C, "SLIPPAGE_DEFAULT_BPS", 30)),
    ]
    kb = []
    for label, bps in choices:
        kb.append([InlineKeyboardButton(f"{label}", callback_data=f"tw_slip:{bps}")])
    kb.append([InlineKeyboardButton("⬅ Back", callback_data="tw_back_amount")])
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="tw_cancel")])
    return kb

def _tw_amount_keyboard(uid, wallet, token_in):
    # Show balance for convenience + "All"
    bal_display = "?"
    if BL:
        try:
            table = BL.all_balances()
            row = table.get(wallet, {})
            # try both native ONE and token_in specifically
            if token_in.upper() == "ONE":
                bal_display = str(_resolve_one_value(row))
            else:
                bal_display = str(row.get(token_in.upper(), "0"))
        except Exception:
            pass

    kb = [
        [InlineKeyboardButton(f"All ({bal_display} {token_in})", callback_data="tw_amt_all")],
        [InlineKeyboardButton("⬅ Back", callback_data="tw_back_to")],
        [InlineKeyboardButton("❌ Cancel", callback_data="tw_cancel")],
    ]
    return kb, bal_display

def _tw_render_manual_quote(uid, st):
    """
    Call runner.build_manual_quote() with the user's selections
    and render a preview block. This is where we remove the fake
    '~? 1USDC' behavior.
    """
    if runner is None or not hasattr(runner, "build_manual_quote"):
        return ("Runner manual quote not available.", None, None)

    wallet_key   = st["wallet"]
    token_in_ui  = st["from"]
    token_out_ui = st["to"]
    amt_str      = st["amount"]
    slip_bps     = int(st["slip_bps"])

    if not (wallet_key and token_in_ui and token_out_ui and amt_str):
        return ("Trade is incomplete.", None, None)

    try:
        amt_dec = Decimal(amt_str)
    except Exception:
        return ("Bad amount.", None, None)

    mq = runner.build_manual_quote(
        wallet_key=wallet_key,
        token_in=token_in_ui,
        token_out=token_out_ui,
        amount_in=amt_dec,
        slippage_bps=slip_bps,
    )

    # Build the Telegram review text
    path     = getattr(mq, "path_text", f"{token_in_ui} → {token_out_ui}")
    ain_txt  = getattr(mq, "amount_in_text", f"{amt_str} {token_in_ui}")
    qout_txt = getattr(mq, "quote_out_text", f"? {token_out_ui}")
    imp_bps  = getattr(mq, "impact_bps", None)
    slip_bps_val = getattr(mq, "slippage_bps", slip_bps)
    min_out  = getattr(mq, "min_out_text", f"? {token_out_ui}")

    gas_est  = getattr(mq, "gas_estimate", "—")
    nonce    = getattr(mq, "nonce", "—")
    allow_ok = getattr(mq, "allowance_ok", False)
    tx_prev  = getattr(mq, "tx_preview_text", "(tx preview)")
    slip_ok  = getattr(mq, "slippage_ok", True)
    need_appr_txt = getattr(mq, "approval_required_amount_text", None)

    lines = []
    lines.append(f"Review Trade — {wallet_key}")
    lines.append(f"Path     : {path}")
    lines.append(f"AmountIn : {ain_txt}")
    lines.append(f"QuoteOut : {qout_txt}")
    if imp_bps is not None:
        lines.append(f"Impact   : {imp_bps:.2f} bps")
    else:
        lines.append("Impact   : —")
    # Slippage / minOut line, in human %
    pct = Decimal(slip_bps_val) / Decimal(100)
    lines.append(f"Slippage : {pct:.2f}% max → minOut {min_out}")
    lines.append(f"Gas Est  : {gas_est}")
    lines.append(f"Nonce    : {nonce}")
    lines.append(f"Allowance: {'OK' if allow_ok else 'NOT APPROVED'}")
    if not allow_ok and need_appr_txt:
        lines.append(f"Required : approve {need_appr_txt}")
    if not slip_ok:
        lines.append("⚠ Price already worse than allowed slippage")

    lines.append("")
    lines.append("Would send:")
    lines.append(tx_prev)

    txt = "\n".join(lines)

    # Keyboard:
    kb_rows = []
    if not allow_ok and need_appr_txt:
        kb_rows.append([InlineKeyboardButton("✅ Approve spend first", callback_data="tw_do_approve")])
    elif allow_ok and slip_ok:
        kb_rows.append([InlineKeyboardButton("✅ Execute Trade", callback_data="tw_do_execute")])
    else:
        kb_rows.append([InlineKeyboardButton("⚠ Adjust Slippage", callback_data="tw_back_slip")])

    kb_rows.append([InlineKeyboardButton("❌ Cancel", callback_data="tw_cancel")])
    return (txt, kb_rows, mq)

def _tw_set_amount(uid, st, amount_text):
    st["amount"] = amount_text
    st["step"] = "slip"
    return True

def _tw_handle_wallet(q, uid, wallet):
    st = _tw_require_state(uid)
    st["wallet"] = wallet
    st["step"] = "from"
    kb = _tw_assets_keyboard(uid, "from")
    _tw_reply_edit(q, f"Wallet: {wallet}\n\nSelect FROM asset:", kb)

def _tw_handle_from(q, uid, sym):
    st = _tw_require_state(uid)
    st["from"] = sym
    st["step"] = "to"
    kb = _tw_assets_keyboard(uid, "to")
    _tw_reply_edit(q, f"FROM: {sym}\n\nSelect TO asset:", kb)

def _tw_handle_to(q, uid, sym):
    st = _tw_require_state(uid)
    st["to"] = sym
    st["step"] = "amount"
    kb, bal_disp = _tw_amount_keyboard(uid, st["wallet"], st["from"])
    _tw_reply_edit(q,
        f"FROM {st['from']} TO {sym}\n\nEnter amount of {st['from']} to trade.\nBalance: {bal_disp} {st['from']}",
        kb
    )

def _tw_handle_amt_all(q, uid):
    st = _tw_require_state(uid)
    # pull balance for that wallet/from
    amt = "0"
    if BL:
        try:
            table = BL.all_balances()
            row = table.get(st["wallet"], {})
            if st["from"].upper() == "ONE":
                amt = str(_resolve_one_value(row))
            else:
                amt = str(row.get(st["from"].upper(), "0"))
        except Exception:
            pass
    _tw_set_amount(uid, st, amt)
    kb = _tw_slip_keyboard(uid)
    _tw_reply_edit(q,
        f"Amount set to ALL ({amt} {st['from']}).\nSelect slippage limit:",
        kb
    )

def _tw_handle_slip(q, uid, bps_str):
    st = _tw_require_state(uid)
    st["slip_bps"] = bps_str
    st["step"] = "review"

    txt, kb_rows, mq = _tw_render_manual_quote(uid, st)
    _tw_reply_edit(q, txt, kb_rows)

def _tw_handle_back(q, uid, dest):
    st = _tw_require_state(uid)
    if dest == "wallet":
        st["step"] = "wallet"
        wallets = getattr(C, "WALLETS", {})
        kb = [[InlineKeyboardButton(name, callback_data=f"tw_wallet:{name}")]
              for name in sorted(wallets.keys())]
        kb.append([InlineKeyboardButton("❌ Cancel", callback_data="tw_cancel")])
        _tw_reply_edit(q, "Select wallet:", kb)
    elif dest == "from":
        st["step"] = "from"
        kb = _tw_assets_keyboard(uid, "from")
        _tw_reply_edit(q, f"Wallet: {st['wallet']}\n\nSelect FROM asset:", kb)
    elif dest == "to":
        st["step"] = "to"
        kb = _tw_assets_keyboard(uid, "to")
        _tw_reply_edit(q,
            f"FROM: {st['from']}\n\nSelect TO asset:",
            kb
        )
    elif dest == "amount":
        st["step"] = "amount"
        kb, bal_disp = _tw_amount_keyboard(uid, st["wallet"], st["from"])
        _tw_reply_edit(q,
            f"FROM {st['from']} TO {st['to']}\n\nEnter amount of {st['from']} to trade.\nBalance: {bal_disp} {st['from']}",
            kb
        )
    elif dest == "slip":
        st["step"] = "slip"
        kb = _tw_slip_keyboard(uid)
        _tw_reply_edit(q,
            f"Amount: {st.get('amount','?')} {st.get('from','?')}\nSelect slippage limit:",
            kb
        )

def _tw_handle_approve(q, uid):
    # must be admin
    if not is_admin(q.from_user.id):
        q.answer("Not authorized.", show_alert=True); return
    # TODO:
    #   call runner.* helper to send ERC20 approval for EXACT amount.
    #   That helper should exist alongside execute_manual_quote.
    q.edit_message_text("Approval sent (TODO hook). Now re-run /trade to execute.")
    q.answer("Approval tx (placeholder).")

def _tw_handle_execute(q, uid):
    if not is_admin(q.from_user.id):
        q.answer("Not authorized.", show_alert=True); return
    st = _tw_require_state(uid)
    if runner is None or not hasattr(runner, "execute_manual_quote"):
        q.answer("Execution backend missing.", show_alert=True); return
    try:
        amt_dec = Decimal(st["amount"])
    except Exception:
        q.answer("Bad amount.", show_alert=True); return

    try:
        txr = runner.execute_manual_quote(
            wallet_key=st["wallet"],
            token_in=st["from"],
            token_out=st["to"],
            amount_in=amt_dec,
            slippage_bps=int(st["slip_bps"]),
        )
        txh = txr.get("tx_hash","0x")
        filled = txr.get("filled_text","")
        gas_used = txr.get("gas_used","—")
        explorer = txr.get("explorer_url","")
        q.edit_message_text(f"✅ Executed manual trade\n{filled}\nGas used: {gas_used}\nTx: {txh}\n{explorer}".strip())
        q.answer("Executed.")
    except Exception as e:
        q.edit_message_text(f"❌ Execution failed\n{e}")
        q.answer("Failed.", show_alert=True)

def on_trade_callback(update: Update, context: CallbackContext):
    q = update.callback_query
    uid = q.from_user.id
    data = q.data or ""

    if data == "tw_cancel":
        _TRADE_STATE.pop(uid, None)
        q.edit_message_text("Canceled. No transaction sent.")
        q.answer()
        return

    if data.startswith("tw_wallet:"):
        _tw_handle_wallet(q, uid, data.split(":",1)[1]); q.answer(); return

    if data.startswith("tw_from:"):
        _tw_handle_from(q, uid, data.split(":",1)[1]); q.answer(); return

    if data.startswith("tw_to:"):
        _tw_handle_to(q, uid, data.split(":",1)[1]); q.answer(); return

    if data == "tw_amt_all":
        _tw_handle_amt_all(q, uid); q.answer(); return

    if data == "tw_back_wallet":
        _tw_handle_back(q, uid, "wallet"); q.answer(); return
    if data == "tw_back_from":
        _tw_handle_back(q, uid, "from"); q.answer(); return
    if data == "tw_back_to":
        _tw_handle_back(q, uid, "to"); q.answer(); return
    if data == "tw_back_amount":
        _tw_handle_back(q, uid, "amount"); q.answer(); return
    if data == "tw_back_slip":
        _tw_handle_back(q, uid, "slip"); q.answer(); return

    if data.startswith("tw_slip:"):
        _tw_handle_slip(q, uid, data.split(":",1)[1]); q.answer(); return

    if data == "tw_do_approve":
        _tw_handle_approve(q, uid); return
    if data == "tw_do_execute":
        _tw_handle_execute(q, uid); return

    # unknown
    q.answer()

# -----------------------------------------------------------------------------
# /withdraw (basic scaffold)
# -----------------------------------------------------------------------------

_WITHDRAW_STATE: Dict[int, Dict[str,str]] = {}
TREASURY_ADDR = "0x360c48a44f513b5781854588d2f1A40E90093c60"

def cmd_withdraw(update: Update, context: CallbackContext):
    uid = update.effective_user.id
    _WITHDRAW_STATE[uid] = {
        "step": "wallet",
        "wallet": "",
        "asset": "",
        "amount": "",
    }
    wallets = getattr(C, "WALLETS", {})
    kb = [[InlineKeyboardButton(name, callback_data=f"wd_wallet:{name}")]
          for name in sorted(wallets.keys())]
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="wd_cancel")])

    update.message.reply_text(
        f"Withdraw to treasury:\n{TREASURY_ADDR}\n\nSelect wallet:",
        reply_markup=InlineKeyboardMarkup(kb)
    )

def _wd_reply_edit(q, text, kb=None):
    try:
        q.edit_message_text(text)
        if kb:
            q.edit_message_reply_markup(InlineKeyboardMarkup(kb))
        else:
            q.edit_message_reply_markup(None)
    except Exception:
        q.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb) if kb else None)

def _wd_require_state(uid):
    st = _WITHDRAW_STATE.get(uid)
    if not st:
        st = {"step":"wallet","wallet":"","asset":"","amount":""}
        _WITHDRAW_STATE[uid] = st
    return st

def _wd_assets_keyboard():
    syms = ["ONE", "1USDC", "1sDAI", "TEC", "1ETH"]
    kb = [[InlineKeyboardButton(s, callback_data=f"wd_asset:{s}")] for s in syms]
    kb.append([InlineKeyboardButton("⬅ Back", callback_data="wd_back_wallet")])
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="wd_cancel")])
    return kb

def _wd_amount_keyboard(uid, wallet, token):
    bal_display = "?"
    if BL:
        try:
            table = BL.all_balances()
            row = table.get(wallet, {})
            if token.upper() == "ONE":
                bal_display = str(_resolve_one_value(row))
            else:
                bal_display = str(row.get(token.upper(), "0"))
        except Exception:
            pass
    kb = [
        [InlineKeyboardButton(f"All ({bal_display} {token})", callback_data="wd_amt_all")],
        [InlineKeyboardButton("⬅ Back", callback_data="wd_back_asset")],
        [InlineKeyboardButton("❌ Cancel", callback_data="wd_cancel")],
    ]
    return kb, bal_display

def _wd_handle_wallet(q, uid, wallet):
    st = _wd_require_state(uid)
    st["wallet"] = wallet
    st["step"] = "asset"
    kb = _wd_assets_keyboard()
    _wd_reply_edit(q,
        f"Treasury: {TREASURY_ADDR}\nWallet: {wallet}\n\nSelect asset:",
        kb
    )

def _wd_handle_asset(q, uid, asset):
    st = _wd_require_state(uid)
    st["asset"] = asset
    st["step"] = "amount"
    kb, bal_disp = _wd_amount_keyboard(uid, st["wallet"], asset)
    _wd_reply_edit(q,
        f"Treasury: {TREASURY_ADDR}\nWallet: {st['wallet']}\nAsset: {asset}\n\nEnter withdrawal amount.\nBalance: {bal_disp} {asset}",
        kb
    )

def _wd_set_amount(uid, st, amount_text):
    st["amount"] = amount_text
    st["step"] = "review"
    return True

def _wd_handle_amt_all(q, uid):
    st = _wd_require_state(uid)
    amt = "0"
    if BL:
        try:
            table = BL.all_balances()
            row = table.get(st["wallet"], {})
            if st["asset"].upper() == "ONE":
                amt = str(_resolve_one_value(row))
            else:
                amt = str(row.get(st["asset"].upper(), "0"))
        except Exception:
            pass
    _wd_set_amount(uid, st, amt)
    _wd_render_review(q, uid)

def _wd_render_review(q, uid):
    st = _wd_require_state(uid)
    txt = (
        "Withdraw Review\n"
        f"From Wallet : {st['wallet']}\n"
        f"Asset       : {st['asset']}\n"
        f"Amount      : {st['amount']} {st['asset']}\n"
        f"To          : {TREASURY_ADDR}\n\n"
        "Gas and nonce will be estimated before send."
    )
    kb = [
        [InlineKeyboardButton("✅ Send Withdrawal", callback_data="wd_do_send")],
        [InlineKeyboardButton("⬅ Back", callback_data="wd_back_amount")],
        [InlineKeyboardButton("❌ Cancel", callback_data="wd_cancel")],
    ]
    _wd_reply_edit(q, txt, kb)

def _wd_handle_back(q, uid, dest):
    st = _wd_require_state(uid)
    if dest == "wallet":
        st["step"] = "wallet"
        wallets = getattr(C, "WALLETS", {})
        kb = [[InlineKeyboardButton(name, callback_data=f"wd_wallet:{name}")]
              for name in sorted(wallets.keys())]
        kb.append([InlineKeyboardButton("❌ Cancel", callback_data="wd_cancel")])
        _wd_reply_edit(q,
            f"Treasury: {TREASURY_ADDR}\nSelect wallet:",
            kb
        )
    elif dest == "asset":
        st["step"] = "asset"
        kb = _wd_assets_keyboard()
        _wd_reply_edit(q,
            f"Treasury: {TREASURY_ADDR}\nWallet: {st['wallet']}\n\nSelect asset:",
            kb
        )
    elif dest == "amount":
        st["step"] = "amount"
        kb, bal_disp = _wd_amount_keyboard(uid, st["wallet"], st["asset"])
        _wd_reply_edit(q,
            f"Treasury: {TREASURY_ADDR}\nWallet: {st['wallet']}\nAsset: {st['asset']}\n\nEnter withdrawal amount.\nBalance: {bal_disp} {st['asset']}",
            kb
        )

def _wd_handle_send(q, uid):
    if not is_admin(q.from_user.id):
        q.answer("Not authorized.", show_alert=True); return
    st = _wd_require_state(uid)
    # TODO:
    #   this needs a runner/transfer helper that:
    #   - creates a transfer tx from st["wallet"] to TREASURY_ADDR
    #   - for ONE use native transfer; for ERC20 use transfer()
    #   - signs+sends
    #   - returns tx hash + gas used + explorer URL
    q.edit_message_text(
        "✅ Withdrawal sent (TODO hook)\n"
        f"{st['amount']} {st['asset']} -> {TREASURY_ADDR}\n"
        "Tx: 0x..."
    )
    q.answer("Sent.")

def on_withdraw_callback(update: Update, context: CallbackContext):
    q = update.callback_query
    uid = q.from_user.id
    data = q.data or ""

    if data == "wd_cancel":
        _WITHDRAW_STATE.pop(uid, None)
        q.edit_message_text("Canceled. No withdrawal sent.")
        q.answer(); return

    if data.startswith("wd_wallet:"):
        _wd_handle_wallet(q, uid, data.split(":",1)[1]); q.answer(); return

    if data.startswith("wd_asset:"):
        _wd_handle_asset(q, uid, data.split(":",1)[1]); q.answer(); return

    if data == "wd_amt_all":
        _wd_handle_amt_all(q, uid); q.answer(); return

    if data == "wd_back_wallet":
        _wd_handle_back(q, uid, "wallet"); q.answer(); return
    if data == "wd_back_asset":
        _wd_handle_back(q, uid, "asset"); q.answer(); return
    if data == "wd_back_amount":
        _wd_handle_back(q, uid, "amount"); q.answer(); return

    if data == "wd_do_send":
        _wd_handle_send(q, uid); return

    # unknown
    q.answer()

# ---------- Main ----------
def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or getattr(C, "TELEGRAM_BOT_TOKEN", None)
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN not set in environment or config")

    up = Updater(token=token, use_context=True)
    dp = up.dispatcher

    # Core & on-chain (unchanged from working bot, plus /trade /withdraw)
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

    # NEW
    dp.add_handler(CommandHandler("trade", cmd_trade))
    dp.add_handler(CommandHandler("withdraw", cmd_withdraw))

    # Callbacks
    dp.add_handler(CallbackQueryHandler(on_exec_button, pattern=r"^exec:[A-Za-z0-9_\-]+$"))
    dp.add_handler(CallbackQueryHandler(on_exec_confirm, pattern=r"^exec_go:[A-Za-z0-9_\-]+$"))
    dp.add_handler(CallbackQueryHandler(on_exec_cancel, pattern=r"^exec_cancel$"))

    # NEW callback groups
    dp.add_handler(CallbackQueryHandler(on_trade_callback, pattern=r"^tw_"))
    dp.add_handler(CallbackQueryHandler(on_withdraw_callback, pattern=r"^wd_"))

    dp.add_error_handler(_log_error)
    dp.add_handler(MessageHandler(Filters.all, _log_update), group=-1)

    log.info("Handlers registered: /start /help /version /sanity /assets /prices /balances /slippage /ping /plan /dryrun /cooldowns /trade /withdraw")
    up.start_polling(clean=True)
    log.info("Telegram bot started")
    up.idle()

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# TECBot Telegram Listener — formatting stable, tolerant ONE resolver, Coinbase compare (mid fix in /slippage)

import os, sys, logging, subprocess, shlex
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Optional, List, Dict, Any

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

# Optional Coinbase spot (expects coinbase_client.fetch_eth_usd_price)
def _coinbase_eth() -> Optional[Decimal]:
    try:
        import coinbase_client  # must be in repo (fetch_eth_usd_price -> float|None)
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

# Money formatting for prices (USD)
def _fmt_money(x: Optional[Decimal]) -> str:
    if x is None:
        return "—"
    x = Decimal(x)
    if x >= 1000:
        return f"${x:,.2f}"
    if x >= 1:
        return f"${x:,.5f}".rstrip("0").rstrip(".") if "." in f"{x:.5f}" else f"${x:.5f}"
    # small values, keep 5 decimals
    return f"${x:,.5f}"

# Amount formatting for balances (per symbol rule)
def _fmt_amt(sym: str, val) -> str:
    try:
        d = Decimal(str(val))
    except Exception:
        return str(val)
    if sym.upper() == "1ETH":
        # Keep high precision for 1ETH
        q = Decimal("0.00000001")
        return f"{d.quantize(q, rounding=ROUND_DOWN):f}"
    # Everything else to hundredths for tightness
    q = Decimal("0.01")
    return f"{d.quantize(q, rounding=ROUND_DOWN):.2f}"

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
        "  /sanity — config/modules sanity\n"
        "  /trade — fast wizard to stage a manual action (Advanced path)"
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

# ---- BALANCES (Option A: header tightened, tolerant ONE resolver, wider columns) ----
_ONE_KEY_ORDER = [
    "ONE(native)", "ONE (native)", "ONE_NATIVE", "NATIVE_ONE", "NATIVE",  # common variants
    "ONE", "WONE"  # fallbacks if above not present
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

# ---- ETH reverse tiny-probe (display helper for /prices only; unchanged) ----
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

# ---- PRICES (Option B) ----
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

    lines += ["", "ETH: Harmony LP vs Coinbase"]
    lines.append(f"  LP:       {_fmt_money(next((Decimal(lines[i].split('|')[1].strip().replace('$','').replace(',','')) for i in range(len(lines)) if lines[i].startswith('1ETH ')), None))}")
    lines.append(f"  Coinbase: {_fmt_money(cb_eth)}")

    update.message.reply_text(f"<pre>\n{chr(10).join(lines)}\n</pre>", parse_mode=ParseMode.HTML)

# ---- Mid helpers for /slippage ----
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

# ---- SLIPPAGE (Option B table) ----
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

# ---------- Commands (planner/runner/meta) ----------
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

# ==== TRADE WIZARD (new) =====================================================

# Small per-chat wizard state (does not affect existing planner/runner until final stage)
_TW: Dict[int, Dict[str, Any]] = {}

def _tw_state(chat_id: int) -> Dict[str, Any]:
    st = _TW.get(chat_id)
    if not st:
        st = {"wallet": None, "from": None, "to": None, "force_via": None, "route": None, "amount": None, "slip_bps": None}
        _TW[chat_id] = st
    return st

def _kb_wallets() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("tecbot_usdc", callback_data="tw|w|tecbot_usdc"),
         InlineKeyboardButton("tecbot_sdai", callback_data="tw|w|tecbot_sdai")],
        [InlineKeyboardButton("tecbot_eth", callback_data="tw|w|tecbot_eth"),
         InlineKeyboardButton("tecbot_tec", callback_data="tw|w|tecbot_tec")],
        [InlineKeyboardButton("Cancel", callback_data="tw|x")]
    ]
    return InlineKeyboardMarkup(rows)

def _kb_tokens(kind: str) -> InlineKeyboardMarkup:
    syms = list(getattr(C, "TOKENS", {}).keys()) or ["ONE","1USDC","1sDAI","TEC","1ETH"]
    syms = [s.upper() for s in syms]
    rows, row = [], []
    for s in sorted(set(syms)):
        row.append(InlineKeyboardButton(s, callback_data=f"tw|{kind}|{s}"))
        if len(row) == 3:
            rows.append(row); row=[]
    if row: rows.append(row)
    rows.append([InlineKeyboardButton("Back", callback_data="tw|back"), InlineKeyboardButton("Cancel", callback_data="tw|x")])
    return InlineKeyboardMarkup(rows)

def _kb_routes(from_sym: str, to_sym: str, force_via: Optional[str]) -> InlineKeyboardMarkup:
    # Lazy import (works whether file is in app/ or root/)
    try:
        from app import route_finder as RF
    except Exception:
        import route_finder as RF  # type: ignore
    cands = RF.candidates(from_sym, to_sym, force_via=force_via, max_hops=2, max_routes=3)
    buttons = [[InlineKeyboardButton("Best route (auto)", callback_data="tw|route|AUTO")]]
    if not cands:
        buttons.append([InlineKeyboardButton("No direct pool — will route via intermediates", callback_data="tw|noop")])
    else:
        for p in cands:
            label = " → ".join(p)
            buttons.append([InlineKeyboardButton(label, callback_data=f"tw|route|{label}")])
    buttons.append([InlineKeyboardButton("Advanced path…", callback_data="tw|adv")])
    buttons.append([InlineKeyboardButton("Back", callback_data="tw|back"), InlineKeyboardButton("Cancel", callback_data="tw|x")])
    return InlineKeyboardMarkup(buttons)

def _kb_advanced(force_via: Optional[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("Force via WONE", callback_data="tw|force|WONE"),
             InlineKeyboardButton("Force via 1sDAI", callback_data="tw|force|1sDAI")]]
    row2 = []
    if force_via:
        row2.append(InlineKeyboardButton("Clear constraint", callback_data="tw|force|CLEAR"))
    row2 += [InlineKeyboardButton("Back", callback_data="tw|back"), InlineKeyboardButton("Cancel", callback_data="tw|x")]
    rows.append(row2)
    return InlineKeyboardMarkup(rows)

def _kb_amount() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("100", callback_data="tw|amt|100"),
         InlineKeyboardButton("250", callback_data="tw|amt|250"),
         InlineKeyboardButton("500", callback_data="tw|amt|500")],
        [InlineKeyboardButton("1,500", callback_data="tw|amt|1500"),
         InlineKeyboardButton("5,000", callback_data="tw|amt|5000"),
         InlineKeyboardButton("10,000", callback_data="tw|amt|10000")],
        [InlineKeyboardButton("Custom…", callback_data="tw|amt|CUSTOM"),
         InlineKeyboardButton("Back", callback_data="tw|back"),
         InlineKeyboardButton("Cancel", callback_data="tw|x")],
    ]
    return InlineKeyboardMarkup(rows)

def _kb_slip() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("10 bps", callback_data="tw|slip|10"),
         InlineKeyboardButton("20 bps", callback_data="tw|slip|20"),
         InlineKeyboardButton("30 bps", callback_data="tw|slip|30"),
         InlineKeyboardButton("50 bps", callback_data="tw|slip|50")],
        [InlineKeyboardButton("Custom…", callback_data="tw|slip|CUSTOM"),
         InlineKeyboardButton("Back", callback_data="tw|back"),
         InlineKeyboardButton("Cancel", callback_data="tw|x")],
    ]
    return InlineKeyboardMarkup(rows)

def _kb_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Stage Action", callback_data="tw|ok"),
         InlineKeyboardButton("Edit", callback_data="tw|back")],
        [InlineKeyboardButton("➡️ /dryrun", callback_data="tw|dryrun"),
         InlineKeyboardButton("Cancel", callback_data="tw|x")]
    ])

def cmd_trade(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    _tw_state(chat_id)  # init
    update.message.reply_text("Trade wizard: choose a wallet", reply_markup=_kb_wallets())

def cb_trade(update: Update, context: CallbackContext):
    q = update.callback_query
    if not q or not q.data: return
    chat_id = q.message.chat_id
    st = _tw_state(chat_id)

    try:
        _, op, *rest = q.data.split("|")
    except Exception:
        q.answer("Invalid"); return

    if op == "x":
        _TW.pop(chat_id, None)
        q.edit_message_text("Trade wizard canceled."); return

    if op == "w":
        st["wallet"] = rest[0]
        q.edit_message_text("From asset:", reply_markup=_kb_tokens("from")); return

    if op == "from":
        st["from"] = rest[0]; st["to"]=None; st["route"]=None; st["force_via"]=None
        q.edit_message_text("To asset:", reply_markup=_kb_tokens("to")); return

    if op == "to":
        st["to"] = rest[0]; st["route"]=None
        header = f"Route selection for {st['from']} → {st['to']}\n"
        q.edit_message_text(header + "No direct pool — will route via intermediates",
                            reply_markup=_kb_routes(st["from"], st["to"], st["force_via"]))
        return

    if op == "adv":
        q.edit_message_text("Force an intermediate (optional)", reply_markup=_kb_advanced(st.get("force_via"))); return

    if op == "force":
        choice = rest[0]
        if choice == "CLEAR":
            st["force_via"] = None
        else:
            st["force_via"] = choice
        header = f"Route selection for {st['from']} → {st['to']}\n"
        lbl = f"Advanced route (forced via {st['force_via']})" if st["force_via"] else "No direct pool — will route via intermediates"
        q.edit_message_text(header + lbl, reply_markup=_kb_routes(st["from"], st["to"], st["force_via"])); return

    if op == "route":
        sel = rest[0]
        st["route"] = None if sel == "AUTO" else sel.split(" → ")
        q.edit_message_text("Amount:", reply_markup=_kb_amount()); return

    if op == "amt":
        sel = rest[0]
        if sel == "CUSTOM":
            q.answer("Send a number like 1500 or 1500.25")
            context.user_data["tw_await_amt"] = True
            return
        st["amount"] = sel
        q.edit_message_text("Slippage (bps, max):", reply_markup=_kb_slip()); return

    if op == "slip":
        sel = rest[0]
        if sel == "CUSTOM":
            q.answer("Send integer bps, e.g., 35 for 0.35%")
            context.user_data["tw_await_slip"] = True
            return
        st["slip_bps"] = int(sel)
        q.edit_message_text(_tw_preview(st), reply_markup=_kb_confirm(), parse_mode=ParseMode.HTML); return

    if op == "ok":
        # Try to stage into planner if available; otherwise just confirm
        try:
            if planner and hasattr(planner, "add_manual_action"):
                planner.add_manual_action(
                    wallet_key=st["wallet"],
                    token_from=st["from"],
                    token_to=st["to"],
                    path_tokens=st["route"],   # or None for auto
                    amount_text=str(st["amount"]),
                    slippage_bps=int(st["slip_bps"]),
                    force_via=st.get("force_via")
                )
                q.edit_message_text(_tw_preview(st) + "\n\nStaged as a planned action. Use /dryrun to simulate.",
                                    parse_mode=ParseMode.HTML)
            else:
                q.edit_message_text(_tw_preview(st) + "\n\nStaged (local). Use /dryrun to simulate.",
                                    parse_mode=ParseMode.HTML)
        except Exception as e:
            q.edit_message_text(f"Failed to stage: {e}")
        _TW.pop(chat_id, None)
        return

    if op == "dryrun":
        # convenience: invoke /dryrun handler path
        fake_update = Update(q.update_id, message=None, callback_query=q)  # reuse q to keep chat context
        # We can’t directly call handlers with Update hack; just tell user:
        q.answer("Run /dryrun now to simulate.", show_alert=False)
        return

    if op == "back":
        # simple back chain
        if st.get("slip_bps") is not None:
            st["slip_bps"]=None; q.edit_message_text("Slippage (bps, max):", reply_markup=_kb_slip()); return
        if st.get("amount") is not None:
            st["amount"]=None; q.edit_message_text("Amount:", reply_markup=_kb_amount()); return
        if st.get("route") is not None or st.get("to"):
            st["route"]=None
            header = f"Route selection for {st['from']} → {st['to']}\n"
            q.edit_message_text(header + "No direct pool — will route via intermediates",
                                reply_markup=_kb_routes(st["from"], st["to"], st["force_via"]))
            return
        if st.get("from"):
            st["to"]=None; st["from"]=None
            q.edit_message_text("From asset:", reply_markup=_kb_tokens("from")); return
        if st.get("wallet"):
            st["wallet"]=None
            q.edit_message_text("Trade wizard: choose a wallet", reply_markup=_kb_wallets()); return
        q.edit_message_text("Trade wizard: choose a wallet", reply_markup=_kb_wallets()); return

def _tw_preview(st: Dict[str, Any]) -> str:
    wallet_lbl = st.get("wallet") or "?"
    path_lbl = "Best route (auto)" if not st.get("route") else " → ".join(st["route"])
    amt_lbl = st.get("amount") or "?"
    slip_lbl = f"{st.get('slip_bps','?')} bps (max)"
    note = f"\n(Advanced constraint: via {st['force_via']})" if st.get("force_via") else ""
    # Keep your compact monospace style
    lines = [
        f"<pre>Review Action — {wallet_lbl}",
        f"From     : {st.get('from','?')}    To: {st.get('to','?')}",
        f"Path     : {path_lbl}",
        f"AmountIn : {amt_lbl} {st.get('from','')}",
        f"Slippage : {slip_lbl}</pre>",
    ]
    return "\n".join(lines) + note

def msg_text_trade(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    st = _tw_state(chat_id)
    text = (update.message.text or "").strip()
    if context.user_data.get("tw_await_amt"):
        context.user_data["tw_await_amt"] = False
        st["amount"] = text
        update.message.reply_text("Slippage (bps, max):", reply_markup=_kb_slip())
        return
    if context.user_data.get("tw_await_slip"):
        context.user_data["tw_await_slip"] = False
        try:
            st["slip_bps"] = int(text)
        except Exception:
            update.message.reply_text("Send integer bps, e.g., 35"); return
        update.message.reply_html(_tw_preview(st), reply_markup=_kb_confirm())

# ==== END TRADE WIZARD ========================================================

# ---------- Main ----------
def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or getattr(C, "TELEGRAM_BOT_TOKEN", None)
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN not set in environment or config")

    up = Updater(token=token, use_context=True)
    dp = up.dispatcher

    # Core & on-chain
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

    # NEW: trade wizard (does not affect existing commands)
    dp.add_handler(CommandHandler("trade", cmd_trade))
    dp.add_handler(CallbackQueryHandler(cb_trade, pattern=r"^tw\|"))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, msg_text_trade), group=1)

    # Callbacks & diagnostics
    dp.add_handler(CallbackQueryHandler(on_exec_button, pattern=r"^exec:[A-Za-z0-9_\-]+$"))
    dp.add_handler(CallbackQueryHandler(on_exec_confirm, pattern=r"^exec_go:[A-Za-z0-9_\-]+$"))
    dp.add_handler(CallbackQueryHandler(on_exec_cancel, pattern=r"^exec_cancel$"))
    dp.add_error_handler(_log_error)
    dp.add_handler(MessageHandler(Filters.all, _log_update), group=-1)

    log.info("Handlers registered: /start /help /version /sanity /assets /prices /balances /slippage /ping /plan /dryrun /cooldowns /trade")
    up.start_polling(clean=True)
    log.info("Telegram bot started")
    up.idle()

if __name__ == "__main__":
    main()

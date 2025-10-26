#!/usr/bin/env python3
# TECBot Telegram Listener
#
# - Keeps your original formatting for prices/balances/slippage.
# - Adds /trade (one-pass trade wizard with execute).
# - Adds /withdraw (move funds to treasury).
#
# IMPORTANT:
#   - /plan and /dryrun handlers are still present so we don't break anything,
#     but you will remove them from BotFather's /setcommands so they don't show.
#
#   - You MUST wire the TODOs for:
#       _get_wallet_balance_for_token(...)
#       _quote_swap_for_review(...)
#       _send_approval_tx(...)
#       _execute_swap_now(...)
#       _send_withdraw_tx(...)
#
#   Those should use your existing modules (runner, trade_executor, balances, prices, etc.).
#
#   - We assume Harmony gas is paid in ONE. Adjust _format_gas_cost if needed.

import os, sys, logging, subprocess, shlex
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Optional, List, Dict, Any, Tuple

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

# ---------- Tolerant imports ----------
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

# For route discovery
try:
    from app import route_finder as RF
except Exception:
    import route_finder as RF  # type: ignore

# ---------- Constants ----------
TREASURY_ADDRESS = "0x360c48a44f513b5781854588d2f1A40E90093c60"

SUPPORTED_TOKENS_UI = ["ONE", "1USDC", "1sDAI", "TEC", "1ETH", "WONE"]  # WONE included for clarity
FORCE_VIA_OPTIONS = ["WONE", "1sDAI"]  # Advanced path hints
DEFAULT_SLIP_CHOICES_BPS = [10, 20, 30, 50]  # we will display both bps and %

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

def _coinbase_eth() -> Optional[Decimal]:
    try:
        import coinbase_client
        val = coinbase_client.fetch_eth_usd_price()
        return Decimal(str(val)) if val is not None else None
    except Exception:
        return None

# Money formatting for USD
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

def _resolve_one_value(row: Dict[str, Decimal]) -> Decimal:
    _ONE_KEY_ORDER = [
        "ONE(native)", "ONE (native)", "ONE_NATIVE", "NATIVE_ONE", "NATIVE",
        "ONE", "WONE"
    ]
    lower = {k.lower(): k for k in row.keys()}
    for key in _ONE_KEY_ORDER:
        k = lower.get(key.lower())
        if k is not None:
            try:
                return Decimal(str(row[k]))
            except Exception:
                pass
    return Decimal("0")

def _log_update(update: Update, context: CallbackContext):
    try:
        uid = update.effective_user.id if update.effective_user else "?"
        txt = update.effective_message.text if update.effective_message else "<non-text>"
        log.info(f"UPDATE from {uid}: {txt}")
    except Exception:
        pass

def _log_error(update: object, context: CallbackContext):
    log.exception("Handler error")

# ---------- Gas formatting helper ----------
def _format_gas_cost(gas_units: int,
                     gas_price_wei: int,
                     native_symbol: str = "ONE") -> Tuple[str, str]:
    """
    Returns (gas_line, cost_line)
    - gas_line like '210,843 gas'
    - cost_line like '0.00421 ONE (~4,210 gwei)'
    You MUST adapt gas_price_wei, 1 ONE = 1e18 wei assumption etc.
    TODO: plug correct chain math (Harmony uses 1 ONE = 1e18 "atto"), etc.
    """
    try:
        gas_units_int = int(gas_units)
        gas_price_wei_int = int(gas_price_wei)
        total_wei = gas_units_int * gas_price_wei_int

        # naive conversions:
        # gwei = 1e9 wei
        total_gwei = Decimal(total_wei) / Decimal(10**9)

        # assume 1 ONE = 1e18 wei
        total_one = Decimal(total_wei) / Decimal(10**18)

        gas_line = f"{gas_units_int:,} gas"
        cost_line = f"{total_one:.6f} {native_symbol}  (~{total_gwei:,.0f} gwei)"
        return gas_line, cost_line
    except Exception:
        return (f"{gas_units} gas", f"(gas calc unavailable)")

# ---------- Planner/Dryrun Renders (unchanged) ----------
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

# ---------- Core command handlers (unchanged) ----------
def cmd_start(update: Update, context: CallbackContext):
    update.message.reply_text(
        "TECBot online.\n"
        "Try: /help\n"
        "Core: /trade /withdraw /cooldowns /ping\n"
        "On-chain: /prices [SYMS…] /balances /slippage <IN> [AMOUNT] [OUT]\n"
        "Meta: /version /sanity /assets"
    )

def cmd_help(update: Update, context: CallbackContext):
    update.message.reply_text(
        "Commands:\n"
        "  /ping — health check\n"
        "  /trade — manual trade wizard (one flow w/ execute)\n"
        "  /withdraw — withdraw to treasury wallet\n"
        "  /cooldowns [bot|route] — show cooldowns\n"
        "  /prices [SYMS…] — on-chain quotes in USDC\n"
        "  /balances — per-wallet balances\n"
        "  /slippage <IN> [AMOUNT] [OUT] — impact curve\n"
        "  /assets — configured tokens & wallets\n"
        "  /version — code version\n"
        "  /sanity — config/modules sanity\n"
        # /plan and /dryrun intentionally NOT advertised
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
        for ccc in cols[1:]:
            vals.append(_fmt_amt(ccc, row.get(ccc, 0)))
        lines.append(f"{w_name:<{w_wallet}}  " + "  ".join(f"{v:>{w_amt}}" for v in vals))

    update.message.reply_text(f"<pre>\n{chr(10).join(lines)}\n</pre>", parse_mode=ParseMode.HTML)

# ----- Internal helpers for /prices -----
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

        # reverse tiny-probe: how much ETH we get for tiny USDC, then invert
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

        # forward: sell 1ETH for USDC
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

    # ETH LP vs Coinbase note
    lines += ["", "ETH: Harmony LP vs Coinbase"]
    try:
        eth_lp_display = next(
            (Decimal(lines[i].split("|")[1].strip().replace("$","").replace(",",""))
             for i in range(len(lines))
             if lines[i].startswith("1ETH ")), None)
    except Exception:
        eth_lp_display = None
    lines.append(f"  LP:       {_fmt_money(eth_lp_display)}")
    lines.append(f"  Coinbase: {_fmt_money(cb_eth)}")
    if eth_lp_display is not None and cb_eth not in (None, Decimal(0)):
        try:
            diff = (Decimal(eth_lp_display) - Decimal(cb_eth)) / Decimal(cb_eth) * Decimal(100)
            lines.append(f"  Diff:     {diff:+.2f}%")
        except Exception:
            pass

    update.message.reply_text(f"<pre>\n{chr(10).join(lines)}\n</pre>", parse_mode=ParseMode.HTML)

# ----- Slippage helpers -----
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

# ---------- cooldowns / ping ----------
def cmd_ping(update: Update, context: CallbackContext):
    ip_txt = "unknown"
    try:
        with open("/bot/db/public_ip.txt", "r") as f:
            ip_txt = f.read().strip() or "unknown"
    except Exception:
        pass
    ver = os.getenv("TECBOT_VERSION", getattr(C, "TECBOT_VERSION", "v0.1.0-ops"))
    update.message.reply_text(f"pong · IP: {ip_txt} · {ver}")

def cmd_cooldowns(update: Update, context: CallbackContext):
    defaults = getattr(C, "COOLDOWNS_DEFAULTS", {"price_refresh": 15, "trade_retry": 30, "alert_throttle": 60})
    by_bot = getattr(C, "COOLDOWNS_BY_BOT", {})
    by_route = getattr(C, "COOLDOWNS_BY_ROUTE", {})
    args = context.args or []
    if not args:
        update.message.reply_text(
            "Default cooldowns (seconds):\n  " +
            "\n  ".join(f"{k}: {v}" for k,v in defaults.items())
        )
        return
    key = args[0]
    if key in by_bot:
        d = by_bot[key]; header = f"Cooldowns for {key} (seconds):"
    elif key in by_route:
        d = by_route[key]; header = f"Cooldowns for route {key} (seconds):"
    else:
        update.message.reply_text(
            f"No specific cooldowns for '{key}'. Showing defaults.\n  " +
            "\n  ".join(f"{k}: {v}" for k,v in defaults.items())
        )
        return
    update.message.reply_text(header + "\n  " + "\n  ".join(f"{k}: {v}" for k,v in d.items()))

# ---------- (legacy) plan / dryrun ----------
def cmd_plan(update: Update, context: CallbackContext):
    # still callable manually but not advertised
    if planner is None or not hasattr(planner, "build_plan_snapshot"):
        update.message.reply_text("Plan error: planner module not available."); return
    try:
        snap = planner.build_plan_snapshot()
    except Exception as e:
        log.exception("plan failure")
        update.message.reply_text(f"Plan error: {e}"); return
    update.message.reply_text(render_plan(snap))

def cmd_dryrun(update: Update, context: CallbackContext):
    # still callable manually but not advertised
    if not getattr(C, "DRYRUN_ENABLED", True):
        update.message.reply_text("Dry-run disabled."); return
    if runner is None or not all(hasattr(runner, n) for n in ("build_dryrun","execute_action")):
        update.message.reply_text("Dry-run unavailable."); return
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
    # legacy execute from /dryrun
    q = update.callback_query; data = q.data or ""
    if not data.startswith("exec:"):
        q.answer(); return
    if not is_admin(q.from_user.id):
        q.answer("Not authorized.", show_alert=True); return
    aid = data.split(":",1)[1]
    q.edit_message_text(f"Confirm execution: Action #{aid}\nAre you sure?")
    q.edit_message_reply_markup(InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm", callback_data=f"exec_go:{aid}"),
         InlineKeyboardButton("❌ Abort", callback_data="exec_cancel")]
    ]))
    q.answer()

def on_exec_confirm(update: Update, context: CallbackContext):
    # legacy execute, same warning applies
    q = update.callback_query; data = q.data or ""
    if not data.startswith("exec_go:"):
        q.answer(); return
    if not is_admin(q.from_user.id):
        q.answer("Not authorized.", show_alert=True); return
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
# /trade wizard (single-pass trade builder, quote, execute)
# ============================================================================

# State per chat for /trade
_TRADE: Dict[int, Dict[str, Any]] = {}

def _tw_state(chat_id: int) -> Dict[str, Any]:
    st = _TRADE.get(chat_id)
    if not st:
        st = {
            "wallet": None,
            "from": None,
            "to": None,
            "force_via": None,
            "route_tokens": None,  # e.g. ["1USDC","WONE","1ETH"]
            "route_fees": None,    # e.g. [500,3000]
            "amount": None,
            "slip_bps": None,
            "quote": None,         # filled later by _quote_swap_for_review
        }
        _TRADE[chat_id] = st
    return st

def _get_wallet_balance_for_token(wallet_key: str, sym: str) -> Optional[Decimal]:
    """
    Look up this wallet's balance for this token symbol.
    We already have BL.all_balances() that returns {wallet:{token:amount,...}}.
    We'll try to read from that. This should mirror /balances logic.
    """
    if BL is None:
        return None
    try:
        table = BL.all_balances()
        row = table.get(wallet_key, {})
        if sym.upper() == "ONE":
            return _resolve_one_value(row)
        return Decimal(str(row.get(sym, "0")))
    except Exception as e:
        log.warning("balance lookup failed: %s", e)
        return None

def _fee_bps_to_pct(fee_bps: int) -> str:
    # 500 -> 0.05%, 3000 -> 0.30%, 10000 -> 1.00%
    pct = Decimal(fee_bps) / Decimal(10000) * Decimal(100)  # convert bps-of-1 to %
    # Wait: Uniswap fee tier 500 means 0.05% = 5/10000.
    # 500/1e4 = 0.05, *100 = 5% (WRONG).
    # Let's do direct mapping per Uniswap spec:
    # 100 => 0.01%
    # 500 => 0.05%
    # 3000 => 0.30%
    # 10000 => 1.00%
    # We'll special-case known tiers.
    if fee_bps == 100:
        return "0.01%"
    if fee_bps == 500:
        return "0.05%"
    if fee_bps == 3000:
        return "0.30%"
    if fee_bps == 10000:
        return "1.00%"
    # fallback generic:
    # fee_bps basis points of 1.00%? We'll just show bps/100 with 2 decimals as %
    pct_generic = (Decimal(fee_bps) / Decimal(10000)) * Decimal(100)
    return f"{pct_generic:.2f}%"

def _humanize_route(route_tokens: List[str], route_fees: List[int]) -> Tuple[str, str]:
    """
    route_tokens = ["1USDC","WONE","1ETH"]
    route_fees   = [500,3000]

    Returns:
      readable_path:
        '1USDC → WONE@0.05% → 1ETH@0.30%'
      total_fee_text:
        '~0.35% total pool fees'
    """
    if not route_tokens or not route_fees:
        return ("Best route (auto)", "—")
    hops = []
    total_pct = Decimal("0")
    for i in range(len(route_fees)):
        token_next = route_tokens[i+1]
        fee_raw = route_fees[i]
        fee_pct_txt = _fee_bps_to_pct(fee_raw)
        hops.append(f"{route_tokens[i]} → {token_next}@{fee_pct_txt}")
        # we add fee_pct to total_pct numerically:
        # convert known tiers numerically:
        if fee_raw == 100:
            total_pct += Decimal("0.01")
        elif fee_raw == 500:
            total_pct += Decimal("0.05")
        elif fee_raw == 3000:
            total_pct += Decimal("0.30")
        elif fee_raw == 10000:
            total_pct += Decimal("1.00")
        else:
            # fallback approx:
            total_pct += (Decimal(fee_raw) / Decimal(10000)) * Decimal(100)
    readable_path = " → ".join([route_tokens[0]] + [f"{route_tokens[i+1]}@{_fee_bps_to_pct(route_fees[i])}" for i in range(len(route_fees))])
    total_fee_text = f"~{total_pct:.2f}% total pool fees"
    return readable_path, total_fee_text

def _quote_swap_for_review(st: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build a full preview for the final confirmation screen:
    - quote_out_human
    - impact_bps
    - min_out_human (after slippage)
    - gas_units, gas_price_wei
    - allowance_ok
    - nonce
    - tx_preview_text (router call)
    TODO: wire this into your actual quoting logic.
    """
    # This is where you call your current dryrun path to get:
    #   - expected output
    #   - impact
    #   - minOut using st["slip_bps"]
    #   - gas estimate, gas price
    #   - allowance status
    #   - account nonce
    #
    # Below is placeholder structure you must replace with real calls.
    dummy = {
        "amount_in_human": f"{st.get('amount')} {st.get('from')}",
        "quote_out_human": f"0.4321 {st.get('to')}",
        "impact_bps": Decimal("11.00"),
        "min_out_human": f"0.4308 {st.get('to')}",
        "slip_bps": st.get("slip_bps", 30),
        "gas_units": 210843,
        "gas_price_wei": 20000000000,  # 20 gwei placeholder
        "allowance_ok": True,
        "needs_approval_amount_human": f"{st.get('amount')} {st.get('from')}",
        "nonce": 57,
        "tx_preview_text": (
            "swapExactTokensForTokens(\n"
            "  path=[USDC,WONE@0.05%,1ETH@0.30%],\n"
            "  amountIn=1,500.00,\n"
            "  amountOutMin=0.4308,\n"
            "  deadline=now+120s\n"
            ")"
        ),
    }
    return dummy

def _send_approval_tx(st: Dict[str, Any], amount_human: str) -> Dict[str, Any]:
    """
    Send ERC20 approve() for EXACT trade size, not unlimited.
    Returns tx result dict {hash, gas_used, gas_price_wei,...}
    TODO: hook into trade_executor / web3 signer.
    """
    # placeholder stub
    return {
        "tx_hash": "0xAPPROVEPLACEHOLDER",
        "gas_used": 50000,
        "gas_price_wei": 20000000000,
    }

def _execute_swap_now(st: Dict[str, Any], quote: Dict[str, Any]) -> Dict[str, Any]:
    """
    Actually submit the swap using st + quote info.
    Returns tx result dict {hash, gas_used, gas_price_wei, filled_out_human}
    TODO: hook into your real on-chain send.
    """
    return {
        "tx_hash": "0xSWAPPLACEHOLDER",
        "gas_used": 212004,
        "gas_price_wei": 20000000000,
        "filled_out_human": quote.get("quote_out_human", "?"),
    }

# --- Keyboards for /trade steps ---

def _kb_trade_wallets() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("tecbot_usdc", callback_data="tw|w|tecbot_usdc"),
         InlineKeyboardButton("tecbot_sdai", callback_data="tw|w|tecbot_sdai")],
        [InlineKeyboardButton("tecbot_eth",  callback_data="tw|w|tecbot_eth"),
         InlineKeyboardButton("tecbot_tec",  callback_data="tw|w|tecbot_tec")],
        [InlineKeyboardButton("Cancel", callback_data="tw|x")]
    ]
    return InlineKeyboardMarkup(rows)

def _kb_trade_tokens(kind: str) -> InlineKeyboardMarkup:
    syms = SUPPORTED_TOKENS_UI
    rows, row = [], []
    for s in syms:
        row.append(InlineKeyboardButton(s, callback_data=f"tw|{kind}|{s}"))
        if len(row) == 3:
            rows.append(row); row=[]
    if row: rows.append(row)
    rows.append([InlineKeyboardButton("Back", callback_data="tw|back"),
                 InlineKeyboardButton("Cancel", callback_data="tw|x")])
    return InlineKeyboardMarkup(rows)

def _build_route_candidates(st: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Use route_finder to produce a few candidate paths. Optionally apply force_via.
    We return a list like:
      [{"tokens":["1USDC","WONE","1ETH"],
        "fees":[500,3000],
        "label_multiline":"1USDC → WONE@0.05% → 1ETH@0.30%\nImpact ~11 bps\nFees ~0.35% total\nEstOut ~0.4321 1ETH"}
      , ...]
    NOTE: Impact/EstOut in this menu can be rough/placeholder. We'll show real data in final quote.
    """
    token_in = st.get("from")
    token_out = st.get("to")
    force_via = st.get("force_via")

    cands_raw = RF.candidates(token_in, token_out, force_via=force_via, max_hops=2, max_routes=3)
    out: List[Dict[str, Any]] = []

    # transform each raw path with fees -> tokens[], fees[]
    # raw like ["1USDC","WONE@500","1ETH@3000"]
    for raw in cands_raw:
        tokens_clean: List[str] = []
        fees_clean: List[int] = []
        # parse "WONE@500"
        first_tok = raw[0]
        tokens_clean.append(first_tok)
        for hop in raw[1:]:
            nxt, fee_txt = hop.split("@")
            tokens_clean.append(nxt)
            fees_clean.append(int(fee_txt))
        readable_path, fee_text = _humanize_route(tokens_clean, fees_clean)
        label = (
            f"{readable_path}\n"
            f"{fee_text}\n"
            f"(Preview size not final)"
        )
        out.append({
            "tokens": tokens_clean,
            "fees": fees_clean,
            "label": label,
        })
    return out

def _kb_trade_routes(st: Dict[str, Any]) -> InlineKeyboardMarkup:
    buttons: List[List[InlineKeyboardButton]] = []
    cands = _build_route_candidates(st)

    if cands:
        # show each route as its own button
        for i, ccc in enumerate(cands, start=1):
            buttons.append([
                InlineKeyboardButton(
                    f"Use route {i}",
                    callback_data=f"tw|route|{i}"
                )
            ])
    else:
        buttons.append([
            InlineKeyboardButton("No direct pool — routing via intermediates", callback_data="tw|noop")
        ])

    # advanced path picks
    buttons.append([
        InlineKeyboardButton("Force via WONE", callback_data="tw|force|WONE"),
        InlineKeyboardButton("Force via 1sDAI", callback_data="tw|force|1sDAI"),
    ])
    # auto/best
    buttons.append([
        InlineKeyboardButton("Auto (best)", callback_data="tw|route|AUTO")
    ])

    buttons.append([
        InlineKeyboardButton("Back", callback_data="tw|back"),
        InlineKeyboardButton("Cancel", callback_data="tw|x")
    ])
    return InlineKeyboardMarkup(buttons)

def _kb_trade_amount(st: Dict[str, Any]) -> InlineKeyboardMarkup:
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
    return InlineKeyboardMarkup(rows)

def _kb_trade_slip() -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for bps in DEFAULT_SLIP_CHOICES_BPS:
        pct = Decimal(bps) / Decimal(100)  # 30 bps -> 0.30%
        rows.append([
            InlineKeyboardButton(f"{bps} bps ({pct:.2f}%)", callback_data=f"tw|slip|{bps}")
        ])
    rows.append([
        InlineKeyboardButton("Custom…", callback_data="tw|slip|CUSTOM"),
        InlineKeyboardButton("Back", callback_data="tw|back"),
        InlineKeyboardButton("Cancel", callback_data="tw|x")
    ])
    return InlineKeyboardMarkup(rows)

def _kb_trade_confirm(allowance_ok: bool, price_ok: bool) -> InlineKeyboardMarkup:
    if not price_ok:
        # price already breached slippage cap
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("Change Slippage", callback_data="tw|edit_slip")],
            [InlineKeyboardButton("Cancel", callback_data="tw|x")]
        ])
    if allowance_ok:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Execute Trade", callback_data="tw|exec_go")],
            [InlineKeyboardButton("◀ Back", callback_data="tw|back"),
             InlineKeyboardButton("❌ Cancel", callback_data="tw|x")]
        ])
    else:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("Approve spend first", callback_data="tw|approve")],
            [InlineKeyboardButton("❌ Cancel", callback_data="tw|x")]
        ])

def _kb_trade_after_exec() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Close", callback_data="tw|x")]
    ])

def cmd_trade(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    _tw_state(chat_id)  # init/reset state
    update.message.reply_text("Trade: choose a wallet", reply_markup=_kb_trade_wallets())

def _render_trade_amount_prompt(st: Dict[str, Any]) -> str:
    bal = _get_wallet_balance_for_token(st["wallet"], st["from"])
    bal_line = ""
    if bal is not None:
        bal_line = f"Available in {st['wallet']}: {bal} {st['from']}"
    return (
        f"Amount to spend (in {st['from']})\n"
        f"{bal_line}"
    )

def _slippage_explainer() -> str:
    return (
        "Price protection (max slippage)\n\n"
        "This is the MOST you're willing\n"
        "to lose to price movement between\n"
        "quote and execution.\n\n"
        "Example:\n"
        "30 bps = 0.30%"
    )

def _render_trade_quote(st: Dict[str, Any]) -> Tuple[str, InlineKeyboardMarkup]:
    """
    Build final confirmation message after we have
    wallet/from/to/route/amount/slip_bps.
    """
    quote = _quote_swap_for_review(st)
    st["quote"] = quote

    # path formatting
    if st["route_tokens"] and st["route_fees"]:
        readable_path, total_fee_text = _humanize_route(st["route_tokens"], st["route_fees"])
    else:
        readable_path = "Best route (auto)"
        total_fee_text = "—"

    # gas
    gas_line, cost_line = _format_gas_cost(
        quote.get("gas_units", 0),
        quote.get("gas_price_wei", 0),
        native_symbol="ONE"
    )

    # slippage
    bps = quote.get("slip_bps", st.get("slip_bps"))
    pct_str = f"{Decimal(bps)/Decimal(100):.2f}%" if bps is not None else "?"
    slip_line = f"{pct_str} max → minOut {quote.get('min_out_human','?')}"

    impact_line = f"{quote.get('impact_bps','?')} bps (price move at this size)"

    # allowance / price_ok
    allowance_ok = bool(quote.get("allowance_ok", False))
    # price_ok is check if impact <= slip_bps (max). If we don't have both, assume True.
    try:
        impact_val = Decimal(str(quote.get("impact_bps","0")))
        slip_val = Decimal(str(bps))
        price_ok = (impact_val <= slip_val)
    except Exception:
        price_ok = True

    body_lines = [
        f"<pre>Review Trade — {st.get('wallet','?')}",
        f"Path     : {readable_path}",
        f"AmountIn : {quote.get('amount_in_human','?')}",
        f"QuoteOut : {quote.get('quote_out_human','?')}",
        f"Impact   : {impact_line}",
        f"Fees     : {total_fee_text}",
        f"Slippage : {slip_line}",
        f"Gas Est  : {gas_line}",
        f"Cost     : {cost_line}",
        "Allowance: " + ("OK" if allowance_ok else "NOT APPROVED"),
        f"Nonce    : {quote.get('nonce','?')}",

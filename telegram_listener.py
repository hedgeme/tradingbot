#!/usr/bin/env python3
# TECBot Telegram Listener — formats locked; per-unit LP price & slippage fixed (listener-only)

import os, sys, logging, subprocess, shlex
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Optional, List, Tuple, Dict

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ParseMode, Update
from telegram.ext import (
    Updater, CommandHandler, CallbackQueryHandler, MessageHandler, Filters, CallbackContext
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
log = logging.getLogger("telegram_listener")

# Ensure /bot on path
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
    from app import prices as PR   # we'll still use helpers (_addr, _dec, _find_pool, price_usd)
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

# Optional Coinbase spot (existing file in repo)
CB = None
try:
    import coinbase_client as CB  # exposes fetch_eth_usd_price()
except Exception:
    CB = None

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
    x = Decimal(x)
    if x >= 1000:
        return f"${x:,.2f}"
    if x >= 1:
        return f"${x:,.5f}"  # 5 decimals for sub-$1000 per your request
    return f"${x:,.5f}"

def _coinbase_eth() -> Optional[Decimal]:
    if CB is None:
        return None
    try:
        v = CB.fetch_eth_usd_price()
        return Decimal(str(v)) if v is not None else None
    except Exception:
        return None

def _qctx():
    # lazy import only when needed (for quoting tables)
    from app.chain import get_ctx
    from web3 import Web3
    return get_ctx(C.HARMONY_RPC), Web3

# ------------- QUOTER helpers (listener-only) -------------
# Minimal ABI for QuoterV2.quoteExactInput(bytes path, uint256 amountIn)
_QUOTER_ABI = [{
    "inputs":[
      {"internalType":"bytes","name":"path","type":"bytes"},
      {"internalType":"uint256","name":"amountIn","type":"uint256"}
    ],
    "name":"quoteExactInput",
    "outputs":[
      {"internalType":"uint256","name":"amountOut","type":"uint256"},
      {"internalType":"uint160","name":"sqrtPriceX96After","type":"uint160"},
      {"internalType":"uint32","name":"initializedTicksCrossed","type":"uint32"},
      {"internalType":"uint256","name":"gasEstimate","type":"uint256"}],
    "stateMutability":"nonpayable","type":"function"
}]

def _fee3(fee: int) -> bytes:
    return int(fee).to_bytes(3, "big")

def _build_forward_path(sym_in: str, sym_out: str = "1USDC") -> Optional[List[Tuple[str,int,str]]]:
    """Forward path using only verified pools (sym_in -> ... -> sym_out)."""
    s = sym_in.upper()
    if s == sym_out.upper():
        return []
    # direct
    f = PR._find_pool(s, sym_out)
    if f:
        return [(s, f, sym_out)]
    # via WONE
    f1 = PR._find_pool(s, "WONE"); f2 = PR._find_pool("WONE", sym_out)
    if f1 and f2:
        return [(s, f1, "WONE"), ("WONE", f2, sym_out)]
    # via 1sDAI
    f1 = PR._find_pool(s, "1sDAI"); f2 = PR._find_pool("1sDAI", sym_out)
    if f1 and f2:
        return [(s, f1, "1sDAI"), ("1sDAI", f2, sym_out)]
    return None

def _build_reverse_path(sym_in: str, sym_out: str = "1USDC") -> Optional[List[Tuple[str,int,str]]]:
    """Reverse-buy path (sym_out -> ... -> sym_in); we still encode forward bytes path from sym_out to sym_in."""
    t = sym_in.upper()
    base = sym_out.upper()
    # direct
    f = PR._find_pool(base, t)
    if f:
        return [(base, f, t)]
    # via WONE
    f1 = PR._find_pool(base, "WONE"); f2 = PR._find_pool("WONE", t)
    if f1 and f2:
        return [(base, f1, "WONE"), ("WONE", f2, t)]
    # via 1sDAI
    f1 = PR._find_pool(base, "1sDAI"); f2 = PR._find_pool("1sDAI", t)
    if f1 and f2:
        return [(base, f1, "1sDAI"), ("1sDAI", f2, t)]
    return None

def _path_bytes(hops: List[Tuple[str,int,str]], Web3) -> bytes:
    b = b""
    for i,(a,fee,bn) in enumerate(hops):
        if i==0:
            b += Web3.to_bytes(hexstr=Web3.to_checksum_address(PR._addr(a)))
        b += _fee3(fee) + Web3.to_bytes(hexstr=Web3.to_checksum_address(PR._addr(bn)))
    return b

def _quote_exact_input(hops: List[Tuple[str,int,str]], amount_in_wei: int) -> Optional[int]:
    try:
        ctx, Web3 = _qctx()
        q = ctx.w3.eth.contract(address=Web3.to_checksum_address(C.QUOTER_ADDR), abi=_QUOTER_ABI)
        out = q.functions.quoteExactInput(_path_bytes(hops, Web3), int(amount_in_wei)).call()
        return int(out[0])
    except Exception as e:
        log.debug("quoteExactInput error: %s", e)
        return None

# ------------- rendering helpers -------------
def _balances_block(table: Dict[str, Dict[str, Decimal]]) -> str:
    # Approved style (5 decimals), no duplicate ONE/WONE columns.
    order = ["ONE(native)", "1USDC", "1ETH", "TEC", "1sDAI"]
    def fmt(d: Decimal) -> str:
        try:
            q = Decimal(d).quantize(Decimal("0.00001"), rounding=ROUND_DOWN)
            return f"{q:.5f}"
        except Exception:
            return str(d)
    lines = [f"Balances (@ {now_iso()})"]
    for w_name in sorted(table.keys()):
        row = table[w_name]
        parts = []
        for col in order:
            v = row.get(col, Decimal("0"))
            parts.append(f"{col} {fmt(v)}")
        lines.append(f"{w_name}\n  " + "   ".join(parts) + "\n")
    return "\n".join(lines).rstrip()

def _prices_table() -> str:
    # Option C: Asset | LP Price | Quote Basis | Slippage | Route
    # Per-unit price: price_usd(basis) / basis; Slippage: impact vs tiny probe on the same forward path.
    syms = ["ONE","1USDC","1sDAI","TEC","1ETH"]
    basis_map = {"ONE": Decimal("1"), "1USDC": Decimal("1"), "1sDAI": Decimal("1"),
                 "TEC": Decimal("100"), "1ETH": Decimal("1")}
    rows = []
    # Tiny probe sizes (forward) used to approximate "mid-impact" bps
    tiny_probe = {"ONE": Decimal("0.05"), "1USDC": Decimal("1"),
                  "1sDAI": Decimal("0.05"), "TEC": Decimal("50"), "1ETH": Decimal("0.01")}
    for s in syms:
        try:
            # Special: ONE priced via WONE forward
            route_hops = _build_forward_path("WONE" if s=="ONE" else s, "1USDC")
            route_text = "—" if s=="1USDC" else " → ".join(h[0] for h in route_hops) + f" → {route_hops[-1][2]}" if route_hops else "—"
            if s=="ONE":
                # compute via WONE amount = basis
                basis = basis_map[s]
                price_total = PR.price_usd("WONE", basis)  # total USDC for basis
                lp_per_unit = (price_total / basis) if (price_total and basis>0) else None
                # slippage via tiny probe on WONE
                dec_in = PR._dec("WONE"); dec_usd = PR._dec("1USDC")
                probe = tiny_probe[s]
                wei_in = int(probe * (Decimal(10)**dec_in))
                wei_out = _quote_exact_input(route_hops, wei_in) if route_hops else None
                mid = (Decimal(wei_out) / (Decimal(10)**dec_usd) / probe) if (wei_out and probe>0) else None
                bps = int(((lp_per_unit - mid)/mid*Decimal(10000)).quantize(Decimal("1"))) if (lp_per_unit and mid and mid>0) else 0
                rows.append(("ONE", lp_per_unit, basis, bps, route_text + " (fwd)"))
                continue

            # General case
            basis = basis_map[s]
            total = PR.price_usd(s, basis)  # USDC total for "basis" units
            lp_per_unit = (total / basis) if (total and basis>0) else None

            # Forward tiny-probe slippage
            if s != "1USDC" and route_hops:
                dec_in = PR._dec("WONE" if s=="ONE" else s); dec_usd = PR._dec("1USDC")
                probe = tiny_probe[s]
                wei_in = int(probe * (Decimal(10)**dec_in))
                wei_out = _quote_exact_input(route_hops, wei_in)
                mid = (Decimal(wei_out) / (Decimal(10)**dec_usd) / probe) if (wei_out and probe>0) else None
                bps = int(((lp_per_unit - mid)/mid*Decimal(10000)).quantize(Decimal("1"))) if (lp_per_unit and mid and mid>0) else 0
            else:
                bps = 0

            # ETH: annotate (rev) if reverse is tighter (display stays per forward/best already inside prices.py)
            suffix = ""
            if s == "1ETH":
                # forward & reverse per-unit comparison (small basis 1 ETH and buy-1ETH via reverse)
                # Forward per-unit from lp_per_unit already; compute reverse by buying 1 ETH in USDC and invert
                rev_hops = _build_reverse_path("1ETH","1USDC")
                dec_usd = PR._dec("1USDC"); dec_eth = PR._dec("1ETH")
                # buy 1 ETH: we don't know exact USDC needed; sample 1000 USDC and invert
                if rev_hops:
                    wei_in = int(Decimal("1000") * (Decimal(10)**dec_usd))
                    wei_out = _quote_exact_input(rev_hops, wei_in)
                    eth_out = Decimal(wei_out) / (Decimal(10)**dec_eth) if wei_out else None
                    rev_per_unit = (Decimal("1000")/eth_out) if (eth_out and eth_out>0) else None
                    if lp_per_unit and rev_per_unit:
                        if rev_per_unit > lp_per_unit * Decimal("1.01"):  # >1% tighter
                            suffix = " (rev)"
                            lp_per_unit = rev_per_unit  # display tighter
                route_text += " (rev)" if suffix else " (fwd)"

            rows.append((s, lp_per_unit, basis, bps, route_text))
        except Exception as e:
            rows.append((s, None, basis_map[s], 0, "—"))

    # Format table (fixed-width; Telegram-friendly)
    header = "LP Prices"
    colh = "Asset | LP Price  | Quote Basis | Slippage | Route"
    sep  = "------+-----------+-------------+----------+----------------------------"
    lines = [header, colh, sep]
    for a,px,basis,bps,route in rows:
        pxs = _fmt_money(px)
        bas = f"{basis:.5f}"
        slp = f"{bps:>4} bps" if px not in (None,"—") else "  —   "
        lines.append(f"{a:<5} | {pxs:<9} | {bas:<11} | {slp:<8} | {route}")
    # Coinbase compare (ETH)
    eth_lp = next((x[1] for x in rows if x[0]=="1ETH"), None)
    cb = _coinbase_eth()
    lines.append("")
    lines.append("ETH: Harmony LP vs Coinbase")
    lines.append(f"  LP:       {_fmt_money(eth_lp)}")
    lines.append(f"  Coinbase: {_fmt_money(cb)}")
    if eth_lp is not None and cb not in (None, Decimal(0)):
        diff = (Decimal(eth_lp) - Decimal(cb)) / Decimal(cb) * Decimal(100)
        lines.append(f"  Diff:     {diff:+.2f}%")
    return "\n".join(lines)

def _slippage_table(token_in: str, token_out: str) -> str:
    # Build forward path; if ETH, also compute mid with reverse check but rows are quoted forward on the chosen path.
    token_in = token_in.upper(); token_out = token_out.upper()
    if token_out != "1USDC":
        token_out = "1USDC"  # we only render USDC slippage tables
    # Choose path
    if token_in == "ONE":
        hops = _build_forward_path("WONE", token_out)
        sym_for_dec = "WONE"
    else:
        hops = _build_forward_path(token_in, token_out)
        sym_for_dec = token_in
    # Mid (per-unit) using tiny probe on the same forward path
    dec_in = PR._dec(sym_for_dec); dec_usd = PR._dec("1USDC")
    probe = Decimal("0.01") if token_in != "TEC" else Decimal("10")
    wei_in = int(probe * (Decimal(10)**dec_in))
    wei_out = _quote_exact_input(hops, wei_in) if hops else None
    mid = (Decimal(wei_out) / (Decimal(10)**dec_usd) / probe) if (hops and wei_out and probe>0) else None

    head = [f"Slippage: {token_in} → {token_out}"]
    if mid:
        head.append(f"Baseline (mid): ${mid:,.2f} per 1{token_in}")

    # Targets (USDC) and rows
    targets = [Decimal("10"), Decimal("100"), Decimal("1000"), Decimal("10000")]
    colh = " Size (USDC) | Amount In ({}) | Eff. Price | Slippage vs mid".format(token_in)
    sep  = "------------+-----------------+------------+----------------"
    rows = [colh, sep]
    for usdc in targets:
        if not (hops and mid and mid > 0):
            rows.append(f"{usdc:>12} | {'—':>15} | {'—':>10} | {'—':>14}")
            continue
        est_in = (usdc / mid).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        wei_in = int(est_in * (Decimal(10)**dec_in))
        wei_out = _quote_exact_input(hops, wei_in)
        if not wei_out:
            rows.append(f"{usdc:>12} | {'—':>15} | {'—':>10} | {'—':>14}")
            continue
        eff = (Decimal(wei_out) / (Decimal(10)**dec_usd) / est_in) if est_in > 0 else None
        slip = ((eff - mid)/mid*Decimal(100)) if (eff and mid) else None
        rows.append(f"{usdc:>12} | {est_in:>15.6f} | ${eff:>9.2f} | {slip:+>13.2f}%")

    return "\n".join(head + [""] + rows)

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
        "  /balances — per-wallet balances\n"
        "  /slippage <IN> [AMOUNT] [OUT] — live impact curve (OUT=1USDC)\n"
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
    update.message.reply_text(
        "Sanity:\n  " + "\n  ".join(f"{k}: {v}" for k,v in details.items()) +
        "\n\nModules:\n  " + "\n  ".join(f"{k}: {v}" for k,v in avail.items())
    )

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

def cmd_balances(update: Update, context: CallbackContext):
    if BL is None:
        update.message.reply_text("Balances unavailable (module not loaded)."); return
    try:
        table = BL.all_balances()
    except Exception as e:
        log.exception("balances failure")
        update.message.reply_text(f"Balances error: {e}"); return
    update.message.reply_text(_balances_block(table))

def cmd_prices(update: Update, context: CallbackContext):
    if PR is None:
        update.message.reply_text("Prices unavailable (module not loaded)."); return
    update.message.reply_text(_prices_table())

def cmd_slippage(update: Update, context: CallbackContext):
    args = context.args or []
    if not args:
        update.message.reply_text(
            "Usage: /slippage <TOKEN_IN> [TOKEN_OUT]\n"
            "Examples:\n"
            "  /slippage 1ETH\n"
            "  /slippage TEC 1USDC"
        ); return
    token_in = args[0].upper()
    token_out = args[1].upper() if len(args) >= 2 else "1USDC"
    update.message.reply_text(_slippage_table(token_in, token_out))

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

    log.info("Handlers registered: /start /help /version /sanity /assets /prices /balances /slippage /ping /plan /dryrun /cooldowns")
    up.start_polling(clean=True)  # ok for ptb 13.x
    log.info("Telegram bot started")
    up.idle()

if __name__ == "__main__":
    main()

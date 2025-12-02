# /bot/runner.py
# Minimal dryrun stubs (kept) + real manual quote/execute wired to trade_executor

from dataclasses import dataclass
from typing import List, Optional, Dict, Any, NamedTuple
from decimal import Decimal, ROUND_DOWN

# ---------- Dry-run types (unchanged so /dryrun UI stays stable) ----------
@dataclass
class DryRunResult:
    action_id: str
    bot: str
    path_text: str
    amount_in_text: str
    quote_out_text: str
    impact_bps: float
    slippage_bps: int
    min_out_text: str
    gas_estimate: int
    allowance_ok: bool
    nonce: int
    tx_preview_text: str

@dataclass
class ExecResult:
    tx_hash: str
    filled_text: str
    gas_used: int
    explorer_url: str

# Simple in-memory cache keyed by action_id so execute can find what dryrun showed
_CACHE: Dict[str, DryRunResult] = {}

def build_dryrun() -> List[DryRunResult]:
    # Keep your existing display mock so /dryrun remains usable while strategies mature
    r = DryRunResult(
        action_id="A12",
        bot="tecbot_usdc",
        path_text="1USDC → WONE@500 → 1sDAI@500",
        amount_in_text="1,500.00 USDC",
        quote_out_text="1,501.89 sDAI",
        impact_bps=11.0,
        slippage_bps=30,
        min_out_text="1,497.38 sDAI",
        gas_estimate=210843,
        allowance_ok=True,
        nonce=57,
        tx_preview_text="swapExactTokensForTokens(path=[USDC,WONE@500,sDAI@500], amountIn=1,500,000, amountOutMin=1,497,380, deadline=now+120s)"
    )
    _CACHE[r.action_id] = r
    return [r]

def execute_action(action_id: str) -> ExecResult:
    # Stub remains the same to avoid changing /dryrun behavior
    if action_id not in _CACHE:
        raise RuntimeError("Action not prepared (dry-run cache miss).")
    return ExecResult(
        tx_hash="0x" + "ab"*16,
        filled_text="Filled: 1,500.00 USDC → 1,499.92 sDAI",
        gas_used=212144,
        explorer_url="https://explorer.harmony.one/tx/0x" + "ab"*16,
    )

# ---------------------------------------------------------------------------------
# MANUAL TRADE SUPPORT FOR /trade
# ---------------------------------------------------------------------------------

# Light, safe imports that exist in your repo
try:
    import config as C
except Exception:
    from app import config as C  # if you ever move it under app/

from app import slippage as SLMOD
from app import wallet as W
import trade_executor as TE
from web3 import Web3

# ---------- Helpers: token canon / fee / decimals / formatting ----------

def _canon(sym: str) -> str:
    """Canonical token symbol for routing/addresses (case-insensitive)."""
    s = (sym or "").strip()
    u = s.upper()
    # User 'ONE' routes as 'WONE' on pools; display stays 'ONE'
    if u == "ONE":
        return "WONE"
    # Harmony sDAI symbol is mixed-case in your maps; normalize both inputs
    if u == "1SDAI":
        return "1sDAI"
    return u  # 1USDC, 1ETH, TEC, WONE

def _display_sym(sym: str) -> str:
    """What we show back to the user."""
    u = (sym or "").upper()
    if u == "WONE":
        return "WONE"
    if u == "1SDAI":
        return "1sDAI"
    return u

def _addr(sym: str) -> str:
    """
    Resolve symbol to checksum address via config first, then TE.FALLBACK_TOKENS.
    Case-insensitive matching; supports 1sDAI/1SDAI, ONE/WONE, etc.
    """
    s = _canon(sym)            # e.g. "1sDAI"
    su = s.upper()             # e.g. "1SDAI"

    # Try config.TOKENS first (case-insensitive)
    tok_map = {k.upper(): v for k, v in getattr(C, "TOKENS", {}).items()}
    if su in tok_map:
        return Web3.to_checksum_address(tok_map[su])

    # Fallback to trade_executor.FALLBACK_TOKENS (case-insensitive)
    fe_map = {k.upper(): v for k, v in getattr(TE, "FALLBACK_TOKENS", {}).items()}
    if su in fe_map:
        return Web3.to_checksum_address(fe_map[su])

    raise KeyError(f"Unknown token symbol: {sym}")

def _dec(sym: str) -> int:
    """On-chain decimals for a token symbol."""
    return int(TE.get_decimals(_addr(sym)))

def _fee_for_pair(token_in: str, token_out: str) -> int:
    """
    Read preferred fee tier from C.POOLS_V3 keys like '1USDC/1sDAI@500'.
    Falls back to 500 if none found.
    """
    ins = _canon(token_in)
    outs = _canon(token_out)
    pools = getattr(C, "POOLS_V3", {}) or {}
    # Keys may be like "1USDC/1sDAI@500" or "WONE/1USDC@3000"
    for k in pools.keys():
        try:
            pair, fee = k.split("@", 1)
            a, b = pair.split("/", 1)
            if _canon(a) == ins and _canon(b) == outs:
                return int(fee)
        except Exception:
            continue
    # Default harmony tiers commonly 500 for stable/WONE
    return 500

def _fmt_amt(sym: str, val: Decimal) -> str:
    """Human formatting aligned to your telegram_listener behavior."""
    s = sym.upper()
    if s == "1ETH":
        q = Decimal("0.00000001")
        return f"{val.quantize(q, rounding=ROUND_DOWN):f}"
    q = Decimal("0.01")
    return f"{val.quantize(q, rounding=ROUND_DOWN):.2f}"

def _path_human_with_fee(token_in: str, token_out: str, fee: int) -> str:
    """Display single-hop with fee like '1USDC@500→1sDAI' and ensure ONE/WONE display ok."""
    a = _display_sym(token_in)
    b = _display_sym(token_out)
    return f"{a}@{fee}→{b}"

def _router_addr() -> str:
    return Web3.to_checksum_address(getattr(TE, "ROUTER_ADDR_ETH"))

# ---------- Result container for /trade preview ----------
class ManualQuoteResult(NamedTuple):
    action_id: str
    bot: str
    path_text: str
    amount_in_text: str
    quote_out_text: str
    impact_bps: Optional[float]
    slippage_bps: Optional[int]
    min_out_text: str
    gas_estimate: int
    allowance_ok: bool
    nonce: int
    tx_preview_text: str
    # extras
    slippage_ok: bool
    approval_required_amount_text: Optional[str]

# ---------- Core: build_manual_quote ----------
def build_manual_quote(
    wallet_key: str,
    token_in: str,
    token_out: str,
    amount_in: Decimal,
    slippage_bps: int
) -> ManualQuoteResult:

    t_in_norm  = _canon(token_in)
    t_out_norm = _canon(token_out)

    # Ask the slippage module for a live quote (and path if multi-hop in the future)
    slip = SLMOD.compute_slippage(
        t_in_norm,
        t_out_norm,
        amount_in,
        slippage_bps=slippage_bps
    )
    if not slip:
        # Sentinel failure
        return ManualQuoteResult(
            action_id="manual",
            bot=wallet_key,
            path_text=f"{_display_sym(token_in)} → {_display_sym(token_out)}",
            amount_in_text=f"{_fmt_amt(token_in, amount_in)} {_display_sym(token_in)}",
            quote_out_text=f"~? {_display_sym(token_out)}",
            impact_bps=None,
            slippage_bps=slippage_bps,
            min_out_text=f"? {_display_sym(token_out)}",
            gas_estimate=0,
            allowance_ok=False,
            nonce=0,
            tx_preview_text="(unable to quote)",
            slippage_ok=False,
            approval_required_amount_text=None,
        )

    quoted_out  = slip.get("amount_out")            # Decimal
    min_out     = slip.get("min_out")               # Decimal
    impact_bps  = slip.get("impact_bps")            # float
    # Your slippage.compute_slippage already formats path with @fee; keep single-hop here
    fee = _fee_for_pair(t_in_norm, t_out_norm)
    path_text = _path_human_with_fee(t_in_norm, t_out_norm, fee)

    # Guard: if price impact exceeds user slippage choice, we’ll mark not-ok
    slippage_ok = True
    try:
        if impact_bps is not None and slippage_bps is not None:
            if float(impact_bps) > float(slippage_bps):
                slippage_ok = False
    except Exception:
        pass

    # Pull allowance/gas/nonce/preview via internal prepare (real, no broadcast)
    details = _prepare_manual_trade_for_wallet(
        wallet_key=wallet_key,
        token_in=t_in_norm,
        token_out=t_out_norm,
        amount_in=amount_in,
        slippage_bps=slippage_bps,
        quoted_out=quoted_out,
        min_out=min_out,
    )

    amount_in_text = f"{_fmt_amt(token_in, amount_in)} {_display_sym(token_in)}"
    quote_out_text = f"{_fmt_amt(token_out, quoted_out)} {_display_sym(token_out)}" if quoted_out is not None else f"~? {_display_sym(token_out)}"
    min_out_text   = f"{_fmt_amt(token_out, min_out)} {_display_sym(token_out)}" if min_out is not None else f"? {_display_sym(token_out)}"

    return ManualQuoteResult(
        action_id="manual",
        bot=wallet_key,
        path_text=path_text,
        amount_in_text=amount_in_text,
        quote_out_text=quote_out_text,
        impact_bps=impact_bps,
        slippage_bps=slippage_bps,
        min_out_text=min_out_text,
        gas_estimate=int(details.get("gas_estimate", 0)),
        allowance_ok=bool(details.get("allowance_ok", False)),
        nonce=int(details.get("nonce", 0)),
        tx_preview_text=str(details.get("tx_preview_text", "(tx preview unavailable)")),
        slippage_ok=slippage_ok,
        approval_required_amount_text=details.get("approve_amount_text"),
    )

# ---------- Internal: allowance/gas/nonce/preview (no broadcast) ----------
def _prepare_manual_trade_for_wallet(
    wallet_key: str,
    token_in: str,
    token_out: str,
    amount_in: Decimal,
    slippage_bps: int,
    quoted_out: Optional[Decimal],
    min_out: Optional[Decimal],
) -> Dict[str, Any]:

    owner = W.WALLETS.get(wallet_key) or ""
    if not owner:
        raise RuntimeError(f"Unknown wallet key: {wallet_key}")

    addr_in  = _addr(token_in)
    addr_out = _addr(token_out)

    dec_in  = _dec(token_in)
    dec_out = _dec(token_out)

    amount_in_wei = int((amount_in * (Decimal(10) ** dec_in)).to_integral_value(rounding=ROUND_DOWN))

    # If min_out not provided by slippage, compute from quoted_out + chosen slippage
    if min_out is None and quoted_out is not None:
        min_out = quoted_out * (Decimal(10000 - int(slippage_bps)) / Decimal(10000))

    min_out_wei = 1
    if min_out is not None:
        min_out_wei = int((min_out * (Decimal(10) ** dec_out)).to_integral_value(rounding=ROUND_DOWN))

    router = _router_addr()

    # Allowance check for EXACT amount (no unlimited approvals here)
    allowance = TE.get_allowance(owner, addr_in, router)
    allowance_ok = int(allowance) >= int(amount_in_wei)
    approve_text = None if allowance_ok else f"{_fmt_amt(token_in, amount_in)} {_display_sym(token_in)}"

    # Build tx data for preview + gas estimate (same function signature as send path)
    fee = _fee_for_pair(token_in, token_out)
    path_bytes = TE._v3_path_bytes(addr_in, fee, addr_out)
    try:
        router_c = TE.w3.eth.contract(address=router, abi=TE.ROUTER_EXACT_INPUT_ABI)
        # NOTE: exactInput(params) layout differs from Quoter; here we stick to IV3SwapRouter.ExactInputParams
        params = (path_bytes, Web3.to_checksum_address(owner), int(amount_in_wei), int(min_out_wei))
        fn = router_c.functions.exactInput(params)
        try:
            data = fn._encode_transaction_data()
        except AttributeError:
            data = fn.encode_abi()
        tx = {
            "to": router,
            "value": 0,
            "data": data,
            "chainId": TE.HMY_CHAIN_ID,
            "nonce": TE.w3.eth.get_transaction_count(Web3.to_checksum_address(owner)),
            "gasPrice": TE._current_gas_price_wei_capped() if hasattr(TE, "_current_gas_price_wei_capped") else W.suggest_gas_price_wei(),
        }
        try:
            est = TE.w3.eth.estimate_gas({**tx, "from": Web3.to_checksum_address(owner)})
            gas = max(min(int(est * 1.5), 1_500_000), 300_000)
        except Exception:
            gas = 300_000
    except Exception:
        gas = 300_000
        data = b""

    # Pretty tx preview (matches your dryrun style)
    tx_preview_text = (
        f"exactInput(path=[{_display_sym(token_in)}@{fee}→{_display_sym(token_out)}], "
        f"amountIn={amount_in_wei}, amountOutMin={min_out_wei}, deadline=now+600s)"
    )

    nonce = TE.w3.eth.get_transaction_count(Web3.to_checksum_address(owner))

    return {
        "gas_estimate": gas,
        "allowance_ok": allowance_ok,
        "approve_amount_text": approve_text,
        "nonce": int(nonce),
        "tx_preview_text": tx_preview_text,
    }

# ---------- Execute: approve (if needed) + send (single-hop) ----------
def execute_manual_quote(
    wallet_key: str,
    token_in: str,
    token_out: str,
    amount_in: Decimal,
    slippage_bps: int
) -> Dict[str, Any]:

    owner = W.WALLETS.get(wallet_key) or ""
    if not owner:
        raise RuntimeError(f"Unknown wallet key: {wallet_key}")

    addr_in  = _addr(token_in)
    addr_out = _addr(token_out)
    router   = _router_addr()

    dec_in  = _dec(token_in)
    dec_out = _dec(token_out)

    amount_in_wei = int((amount_in * (Decimal(10) ** dec_in)).to_integral_value(rounding=ROUND_DOWN))

    # Approve exact if needed (no unlimited)
    TE.approve_if_needed(wallet_key, addr_in, router, int(amount_in_wei))

    # Single-hop send (current executor supports one hop cleanly)
    fee = _fee_for_pair(token_in, token_out)
    res = TE.swap_v3_exact_input_once(
        wallet_key=wallet_key,
        token_in=addr_in,
        token_out=addr_out,
        amount_in_wei=int(amount_in_wei),
        fee=int(fee),
        slippage_bps=int(slippage_bps),
        deadline_s=600
    )
    txh = res.get("tx_hash", "")
    min_out_wei = int(res.get("amount_out_min", "0") or 0)
    min_out = (Decimal(min_out_wei) / (Decimal(10) ** dec_out)) if min_out_wei > 0 else Decimal("0")

    gas_used = int(res.get("gas_used", 0) or 0)
    gas_price_wei = int(res.get("gas_price_wei", 0) or 0)
    gas_cost_wei = int(res.get("gas_cost_wei", gas_used * gas_price_wei))
    gas_one = Decimal("0")
    if gas_cost_wei:
        gas_one = Decimal(gas_cost_wei) / (Decimal(10) ** 18)
    gas_one_text = f"{gas_one:.6f}" if gas_cost_wei else "0"

    return {
        "tx_hash": txh,
        "filled_text": f"Sent {_fmt_amt(token_in, amount_in)} {_display_sym(token_in)} → min {_fmt_amt(token_out, min_out)} {_display_sym(token_out)}",
        "gas_used": gas_used,
        "gas_one_text": gas_one_text,
        "explorer_url": f"https://explorer.harmony.one/tx/{txh}" if txh else "",
    }

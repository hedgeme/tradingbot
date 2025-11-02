# /bot/runner.py
# Runner: keeps legacy /dryrun mock; adds real /trade hooks using app.slippage + trade_executor
from dataclasses import dataclass
from typing import List, Optional, Dict, Any, NamedTuple
from decimal import Decimal
import time

# --------------------------
# Legacy /dryrun (unchanged)
# --------------------------
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

_CACHE: Dict[str, DryRunResult] = {}

def build_dryrun() -> List[DryRunResult]:
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
    if action_id not in _CACHE:
        raise RuntimeError("Action not prepared (dry-run cache miss).")
    return ExecResult(
        tx_hash="0x" + "ab"*16,
        filled_text="Filled: 1,500.00 USDC → 1,499.92 sDAI",
        gas_used=212144,
        explorer_url="https://explorer.harmony.one/tx/0x" + "ab"*16,
    )

# -------------------------------------------------
# Imports for manual-quote + execute (real wiring)
# -------------------------------------------------
try:
    from app import slippage as SLMOD
except Exception:
    import app.slippage as SLMOD  # type: ignore

try:
    from app import prices as PR  # _addr(sym), _dec(sym)
except Exception as e:
    raise RuntimeError(f"runner: cannot import app.prices: {e}")

import config as C  # WALLETS, etc.

# trade executor helpers (already in your repo)
import trade_executor as TE  # approve_if_needed, swap_v3_exact_input_once, quote_v3_exact_input, _v3_path_bytes
from web3 import Web3

# ------------------------------------
# Symbol normalization ONE <-> WONE
# ------------------------------------
def _norm_in(sym: str) -> str:
    s = sym.upper()
    return "WONE" if s == "ONE" else s

def _norm_out(sym: str) -> str:
    s = sym.upper()
    return "ONE" if s == "WONE" else s

# ------------------------------------
# Small format helpers (mirror bot UI)
# ------------------------------------
def _fmt_amt(sym: str, val: Decimal) -> str:
    if val is None:
        return "—"
    s = sym.upper()
    if s == "1ETH":
        return f"{val:.8f}".rstrip("0").rstrip(".")
    return f"{val:,.2f}"

def _fee_from_path_text_first_hop(path_text: str, token_in_norm: str) -> Optional[int]:
    """
    Extract the fee for the FIRST hop that starts with token_in_norm.
    Examples:
      "1USDC → WONE@500" -> 500
      "TEC → WONE@10000 → 1USDC@3000" -> 10000
      "1USDC → 1sDAI@500" -> 500
    """
    # Normalize ONE→WONE in the string for comparison only
    pt = path_text.replace(" ONE", " WONE").replace("ONE@", "WONE@")
    parts = [p.strip() for p in pt.split("→")]
    if not parts:
        return None
    for i in range(1, len(parts)):
        prev = parts[i-1].split("@")[0].strip().upper()
        cur  = parts[i]
        if prev == token_in_norm.upper():
            if "@" in cur:
                # cur like "WONE@500" or "1sDAI@500"
                seg = cur.split("@", 1)[1].strip()
                # seg may have trailing symbols; keep digits only at start
                num = []
                for ch in seg:
                    if ch.isdigit():
                        num.append(ch)
                    else:
                        break
                try:
                    return int("".join(num)) if num else None
                except Exception:
                    return None
            return None
    return None

def _owner_eth(wallet_key: str) -> str:
    addr = C.WALLETS.get(wallet_key)
    if not addr:
        raise RuntimeError(f"Unknown wallet key: {wallet_key}")
    return Web3.to_checksum_address(addr)

def _addr(sym: str) -> str:
    return Web3.to_checksum_address(PR._addr(sym))

def _dec(sym: str) -> int:
    return int(PR._dec(sym))

def _router_addr() -> str:
    ra = getattr(TE, "ROUTER_ADDR_ETH", None)
    if not ra:
        raise RuntimeError("trade_executor.ROUTER_ADDR_ETH is missing")
    return Web3.to_checksum_address(ra)

# ------------------------------------
# Manual quote result shape
# ------------------------------------
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
    slippage_ok: bool
    approval_required_amount_text: Optional[str]

# ------------------------------------
# Build manual quote (live preview)
# ------------------------------------
def build_manual_quote(
    wallet_key: str,
    token_in: str,
    token_out: str,
    amount_in: Decimal,
    slippage_bps: int
) -> ManualQuoteResult:

    t_in_norm  = _norm_in(token_in)
    t_out_norm = _norm_in(token_out)

    slip_info = SLMOD.compute_slippage(
        t_in_norm, t_out_norm, amount_in, slippage_bps=slippage_bps
    )
    if not slip_info:
        return ManualQuoteResult(
            action_id="manual",
            bot=wallet_key,
            path_text=f"{token_in} → {token_out}",
            amount_in_text=f"{_fmt_amt(token_in, amount_in)} {token_in}",
            quote_out_text=f"~? {token_out}",
            impact_bps=None,
            slippage_bps=slippage_bps,
            min_out_text=f"? {token_out}",
            gas_estimate=0,
            allowance_ok=False,
            nonce=0,
            tx_preview_text="(unable to quote)",
            slippage_ok=False,
            approval_required_amount_text=None,
        )

    quoted_out      = slip_info.get("amount_out")
    min_out_amt     = slip_info.get("min_out")
    impact_bps_val  = slip_info.get("impact_bps")
    path_text_route = slip_info.get("path_text", f"{t_in_norm} → {t_out_norm}")

    # Display path with ONE, not WONE
    path_text_disp = path_text_route.replace("WONE", "ONE")

    amount_in_text  = f"{_fmt_amt(token_in, amount_in)} {token_in}"
    quote_out_text  = f"{_fmt_amt(token_out, quoted_out)} {token_out}" if quoted_out is not None else f"~? {token_out}"
    min_out_text    = f"{_fmt_amt(token_out, min_out_amt)} {token_out}" if min_out_amt is not None else f"? {token_out}"

    # Pre-check on slippage: if current impact already > chosen slippage cap, warn/block
    slippage_ok_flag = True
    try:
        if impact_bps_val is not None and slippage_bps is not None:
            if float(impact_bps_val) > float(slippage_bps):
                slippage_ok_flag = False
    except Exception:
        pass

    details = _prepare_manual_trade_for_wallet(
        wallet_key=wallet_key,
        token_in=t_in_norm,
        token_out=t_out_norm,
        amount_in=amount_in,
        slippage_bps=slippage_bps,
        quoted_out=quoted_out,
        min_out=min_out_amt,
        path_text=path_text_route
    )

    return ManualQuoteResult(
        action_id="manual",
        bot=wallet_key,
        path_text=path_text_disp,
        amount_in_text=amount_in_text,
        quote_out_text=quote_out_text,
        impact_bps=impact_bps_val,
        slippage_bps=slippage_bps,
        min_out_text=min_out_text,
        gas_estimate=int(details.get("gas_estimate", 0)),
        allowance_ok=bool(details.get("allowance_ok", False)),
        nonce=int(details.get("nonce", 0)),
        tx_preview_text=str(details.get("tx_preview_text", "(tx preview unavailable)")),
        slippage_ok=slippage_ok_flag,
        approval_required_amount_text=details.get("approve_amount_text"),
    )

# ------------------------------------
# Internal: allowance/gas/nonce/preview
# ------------------------------------
def _prepare_manual_trade_for_wallet(
    wallet_key: str,
    token_in: str,
    token_out: str,
    amount_in: Decimal,
    slippage_bps: int,
    quoted_out: Optional[Decimal],
    min_out: Optional[Decimal],
    path_text: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Mirrors your /dryrun internals but for a single manual request.
    - Checks allowance for EXACT amount
    - Builds exactInput calldata (single-hop)
    - Gas estimate
    - Next nonce
    - Pretty tx_preview_text
    """
    owner = _owner_eth(wallet_key)
    router = _router_addr()

    # Convert human -> wei
    dec_in  = _dec(token_in)
    dec_out = _dec(token_out)
    amount_in_wei  = int(amount_in * (Decimal(10) ** dec_in))
    min_out_wei    = int((min_out or Decimal(0)) * (Decimal(10) ** dec_out))

    # Determine single-hop fee for first hop
    fee_first = _fee_from_path_text_first_hop(path_text or f"{token_in} → {token_out}", token_in) or 500

    # Build path bytes (single hop) using resolved addresses
    t_in_addr  = _addr(token_in)
    t_out_addr = _addr(token_out)
    path_bytes = TE._v3_path_bytes(t_in_addr, int(fee_first), t_out_addr)

    # Allowance check
    approve_text = None
    try:
        current_allow = TE.get_allowance(owner, t_in_addr, router)
    except Exception:
        current_allow = 0
    allowance_ok = current_allow >= int(amount_in_wei)
    if not allowance_ok:
        approve_text = f"{_fmt_amt(_norm_out(token_in), amount_in)} {_norm_out(token_in)}"

    # Gas estimate preview (node estimate, no headroom here)
    router_ct = TE.w3.eth.contract(address=Web3.to_checksum_address(router), abi=TE.ROUTER_EXACT_INPUT_ABI)
    deadline  = int(time.time()) + 600
    fn = router_ct.functions.exactInput(path_bytes, int(amount_in_wei), max(1, int(min_out_wei)), owner, int(deadline))
    try:
        data = fn._encode_transaction_data() if hasattr(fn, "_encode_transaction_data") else fn.encode_abi()
    except Exception:
        data = fn.encode_abi()
    try:
        est = TE.w3.eth.estimate_gas({"to": router, "from": owner, "data": data, "value": 0})
        gas_est = int(est)
    except Exception:
        gas_est = 300_000  # conservative fallback

    # Next nonce
    try:
        nonce = TE.w3.eth.get_transaction_count(owner)
    except Exception:
        nonce = 0

    tx_preview = f"exactInput(path=[{_norm_out(token_in)}@{int(fee_first)}→{_norm_out(token_out)}], amountIn={amount_in_wei}, amountOutMin={max(1,int(min_out_wei))}, deadline=now+600s)"

    return {
        "gas_estimate": gas_est,
        "allowance_ok": allowance_ok,
        "approve_amount_text": approve_text,
        "nonce": nonce,
        "tx_preview_text": tx_preview,
    }

# ------------------------------------
# Execute manual quote (send tx)
# ------------------------------------
def execute_manual_quote(
    wallet_key: str,
    token_in: str,
    token_out: str,
    amount_in: Decimal,
    slippage_bps: int
) -> Dict[str, Any]:
    """
    Executes a single-hop V3 swap immediately.
    NOTE: If the current resolved path is multi-hop, we refuse with a clear message.
    """
    t_in_norm  = _norm_in(token_in)
    t_out_norm = _norm_in(token_out)

    # Re-quote to get minOut & path (and to fail fast if pool is stale)
    slip_info = SLMOD.compute_slippage(
        t_in_norm, t_out_norm, amount_in, slippage_bps=slippage_bps
    )
    if not slip_info:
        raise RuntimeError("Unable to quote live route for execution.")

    path_text = slip_info.get("path_text", f"{t_in_norm} → {t_out_norm}")
    # Count hops: tokens in the printed path (WONE vs ONE is cosmetic)
    hop_tokens = [p.strip().split("@")[0] for p in path_text.replace("ONE", "WONE").split("→")]
    if len(hop_tokens) != 2:
        raise RuntimeError("Multi-hop execution is not enabled yet for /trade. Choose a direct pool pair.")

    # Fee for the first hop
    fee_first = _fee_from_path_text_first_hop(path_text, t_in_norm) or 500

    # Convert to wei
    dec_in  = _dec(t_in_norm)
    amount_in_wei = int(amount_in * (Decimal(10) ** dec_in))

    # Ensure allowance for EXACT amount
    TE.approve_if_needed(
        wallet_key=wallet_key,
        token_addr=_addr(t_in_norm),
        spender_eth=_router_addr(),
        amount_wei=int(amount_in_wei),
        gas_limit=120_000
    )

    # Execute single-hop swap
    res = TE.swap_v3_exact_input_once(
        wallet_key=wallet_key,
        token_in=_addr(t_in_norm),
        token_out=_addr(t_out_norm),
        amount_in_wei=int(amount_in_wei),
        fee=int(fee_first),
        slippage_bps=int(slippage_bps),
        deadline_s=600
    )
    txh = res.get("tx_hash", "")
    return {
        "tx_hash": txh,
        "filled_text": f"Executed manual swap {amount_in} {token_in} → {token_out}",
        "gas_used": 0,
        "explorer_url": "",
    }

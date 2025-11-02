# Minimal runner stub for /dryrun + execution callback
from dataclasses import dataclass
from typing import List, Optional, Dict, Any, NamedTuple
from decimal import Decimal
import re

# ------------------------
# Existing dataclasses
# ------------------------
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
_CACHE = {}

def build_dryrun() -> List[DryRunResult]:
    # No real actions yet; return one mock so UI can be tested end-to-end.
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
    # Simulate success; replace with real send logic later
    if action_id not in _CACHE:
        raise RuntimeError("Action not prepared (dry-run cache miss).")
    return ExecResult(
        tx_hash="0x" + "ab"*16,
        filled_text="Filled: 1,500.00 USDC → 1,499.92 sDAI",
        gas_used=212144,
        explorer_url="https://explorer.harmony.one/tx/0x" + "ab"*16,
    )

# ---------------------------------------------------------------------------------
# MANUAL TRADE SUPPORT FOR /trade (TELEGRAM)
# ---------------------------------------------------------------------------------

# Load on-chain helpers from your executor to avoid duplicating config/node/ABIs
from web3 import Web3
import trade_executor as TE

try:
    from app import slippage as SLMOD
except Exception:
    import app.slippage as SLMOD  # fallback

# --- symbol mapping ---
def _canon(sym: str) -> str:
    return (sym or "").strip().upper()

def _norm_in(sym: str) -> str:
    s = _canon(sym)
    if s == "ONE":
        return "WONE"
    return s

def _norm_out(sym: str) -> str:
    s = _canon(sym)
    if s == "WONE":
        return "ONE"
    return s

# --- address/decimals helpers (source of truth = trade_executor)
def _addr(sym: str) -> str:
    s = _canon(sym)
    if s == "ONE":
        s = "WONE"
    if s in TE.FALLBACK_TOKENS:
        return Web3.to_checksum_address(TE.FALLBACK_TOKENS[s])
    raise KeyError(f"Unknown token symbol: {sym}")

def _dec(sym: str) -> int:
    return int(TE.get_decimals(_addr(sym)))

def _router_addr() -> str:
    return Web3.to_checksum_address(TE.ROUTER_ADDR_ETH)

# --- parse fee from a path like "1USDC → 1SDAI@500" or "TEC → WONE@10000"
_FEE_RE = re.compile(r"@(\d{3,5})")

def _fee_from_path_text(path_text: str, default_fee: int = 500) -> int:
    if not path_text:
        return default_fee
    m = _FEE_RE.search(path_text)
    if not m:
        return default_fee
    try:
        return int(m.group(1))
    except Exception:
        return default_fee

# --- path inspection utilities ---
def _is_single_hop_display(path_text: str) -> bool:
    if not path_text:
        return False
    # Count arrows ignoring fee annotation
    hops = [p.strip() for p in path_text.split("→")]
    return len(hops) == 2  # tokenA → tokenB

# --- preview text builder (human-friendly, mirrors /dryrun tone) ---
def _preview_exact_input(path_text_display: str, amount_in_wei: int, min_out_wei: int, deadline_s: int = 600) -> str:
    return (
        f"exactInput(path=[{path_text_display.replace(' ', '')}], "
        f"amountIn={amount_in_wei}, amountOutMin={min_out_wei}, deadline=now+{deadline_s}s)"
    )

class ManualQuoteResult(NamedTuple):
    # This mirrors what telegram_listener.render_dryrun() expects
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

def build_manual_quote(
    wallet_key: str,
    token_in: str,
    token_out: str,
    amount_in: Decimal,
    slippage_bps: int
) -> ManualQuoteResult:
    """
    Dryrun-style preview for a specific manual trade request.
    No broadcast here.
    """
    t_in_norm  = _norm_in(token_in)
    t_out_norm = _norm_in(token_out)

    # 1) Live quote (path + amount_out + impact) via your slippage module
    slip_info = SLMOD.compute_slippage(
        t_in_norm,
        t_out_norm,
        amount_in,
        slippage_bps=slippage_bps
    )
    if not slip_info:
        return ManualQuoteResult(
            action_id="manual",
            bot=wallet_key,
            path_text=f"{token_in} → {token_out}",
            amount_in_text=f"{amount_in:,.2f} {token_in}",
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
    path_text_disp  = path_text_route.replace("WONE", "ONE")  # user-facing ONE

    # Human formatting (consistent with telegram_listener)
    def _fmt_amt(sym: str, val: Decimal) -> str:
        if sym.upper() == "1ETH":
            return f"{val:.8f}".rstrip("0").rstrip(".")
        return f"{val:,.2f}"

    amount_in_text  = f"{_fmt_amt(token_in, amount_in)} {token_in}"
    quote_out_text  = f"{_fmt_amt(token_out, quoted_out)} {token_out}" if quoted_out is not None else f"~? {token_out}"
    min_out_text    = f"{_fmt_amt(token_out, min_out_amt)} {token_out}" if min_out_amt is not None else f"? {token_out}"

    # Simple sanity: if price impact already exceeds selected slippage, flag
    slippage_ok_flag = True
    try:
        if impact_bps_val is not None and slippage_bps is not None:
            if float(impact_bps_val) > float(slippage_bps):
                slippage_ok_flag = False
    except Exception:
        pass

    # 2) Allowance / Gas / Nonce / tx_preview using TE's node+ABI
    details = _prepare_manual_trade_for_wallet(
        wallet_key=wallet_key,
        token_in=t_in_norm,
        token_out=t_out_norm,
        amount_in=amount_in,
        slippage_bps=slippage_bps,
        quoted_out=quoted_out,
        min_out=min_out_amt,
    )

    gas_estimate_val   = int(details.get("gas_estimate", 0))
    allowance_ok_flag  = bool(details.get("allowance_ok", False))
    approval_text      = details.get("approve_amount_text")
    next_nonce         = int(details.get("nonce", 0))
    tx_preview_display = details.get("tx_preview_text", "(tx preview unavailable)")

    return ManualQuoteResult(
        action_id="manual",
        bot=wallet_key,
        path_text=path_text_disp,
        amount_in_text=amount_in_text,
        quote_out_text=quote_out_text,
        impact_bps=impact_bps_val,
        slippage_bps=slippage_bps,
        min_out_text=min_out_text,
        gas_estimate=gas_estimate_val,
        allowance_ok=allowance_ok_flag,
        nonce=next_nonce,
        tx_preview_text=tx_preview_display,
        slippage_ok=slippage_ok_flag,
        approval_required_amount_text=approval_text,
    )

def _prepare_manual_trade_for_wallet(
    wallet_key: str,
    token_in: str,
    token_out: str,
    amount_in: Decimal,
    slippage_bps: int,
    quoted_out: Optional[Decimal],
    min_out: Optional[Decimal],
) -> Dict[str, Any]:
    """
    INTERNAL SUPPORT (no broadcast):
      - check allowance for EXACT amount
      - build router exactInput call and estimate gas
      - get next nonce
      - produce a readable tx preview
    """
    w3 = TE.w3  # reuse same provider
    router = _router_addr()

    # Resolve addresses/decimals
    addr_in  = _addr(token_in)
    addr_out = _addr(token_out)
    dec_in   = _dec(token_in)
    dec_out  = _dec(token_out)

    amount_in_wei = int(Decimal(amount_in) * (Decimal(10) ** dec_in))

    # If caller didn't pass min_out, compute with selected slippage against a fresh quote
    if min_out is None:
        # Build path bytes and quote via QuoterV1
        fee = 500  # default; UI normally yields a fee in the path_text, but we may not have it here
        path_bytes = TE._v3_path_bytes(addr_in, fee, addr_out)
        quoted = TE.quote_v3_exact_input(path_bytes, int(amount_in_wei))
        min_out_wei = max(1, (int(quoted) * (10_000 - int(slippage_bps))) // 10_000)
    else:
        min_out_wei = int(Decimal(min_out) * (Decimal(10) ** dec_out))

    # Allowance check against router for EXACT amount
    owner_eth = None
    try:
        # In your env, wallet private keys are loaded inside TE._get_account(wallet_key)
        # We can't call _get_account here (it's private), but allowance doesn't require it.
        # Use WALLETS from root wallet module through TE.FALLBACK_TOKENS? Not needed:
        # We only need the owner address for nonce; TE.swap* will sign later.
        from wallet import WALLETS  # root wallet.py
        owner_eth = Web3.to_checksum_address(WALLETS[wallet_key])
    except Exception:
        owner_eth = None

    allowance_ok = False
    approve_text = None
    try:
        current = TE.get_allowance(owner_eth, addr_in, router) if owner_eth else 0
        allowance_ok = (int(current) >= int(amount_in_wei))
        if not allowance_ok:
            # show the EXACT amount needing approval (human units)
            approve_text = f"{amount_in:,.2f} {token_in}"
    except Exception:
        allowance_ok = False

    # Build router exactInput and estimate gas (with headroom like trade_executor does)
    from web3.contract import Contract
    router_c: Contract = w3.eth.contract(address=router, abi=TE.ROUTER_EXACT_INPUT_ABI)
    path_bytes = TE._v3_path_bytes(addr_in, 500, addr_out)  # preview uses fee=500 here; execution will parse actual fee
    fn = router_c.functions.exactInput(path_bytes, int(amount_in_wei), int(min_out_wei), owner_eth or router, int(w3.eth.get_block("latest")["timestamp"] + 600))
    try:
        data = fn._encode_transaction_data()
    except AttributeError:
        data = fn.encode_abi()

    gas_est = 300_000
    try:
        tx = {
            "to": router,
            "value": 0,
            "data": data,
            "chainId": TE.HMY_CHAIN_ID,
            "nonce": w3.eth.get_transaction_count(owner_eth) if owner_eth else 0,
            "gasPrice": w3.eth.gas_price,
        }
        est = w3.eth.estimate_gas({**tx, "from": owner_eth}) if owner_eth else 300_000
        gas_est = max(min(int(est * 1.5), 1_500_000), 300_000)
    except Exception:
        pass

    # Nonce
    next_nonce = 0
    try:
        if owner_eth:
            next_nonce = int(w3.eth.get_transaction_count(owner_eth))
    except Exception:
        next_nonce = 0

    # Human preview string
    path_text_display = f"{_norm_out(token_in)}@500→{_norm_out(token_out)}"
    tx_preview = _preview_exact_input(path_text_display, int(amount_in_wei), int(min_out_wei))

    return {
        "gas_estimate": int(gas_est),
        "allowance_ok": bool(allowance_ok),
        "approve_amount_text": approve_text,
        "nonce": int(next_nonce),
        "tx_preview_text": tx_preview,
    }

def execute_manual_quote(
    wallet_key: str,
    token_in: str,
    token_out: str,
    amount_in: Decimal,
    slippage_bps: int
) -> Dict[str, Any]:
    """
    Broadcast the trade prepared in build_manual_quote().
    Steps:
      1) parse single-hop fee from the path
      2) approve_if_needed(EXACT amount)
      3) swap_v3_exact_input_once(...)
    """
    # Build a fresh quote to (a) get path fee and (b) block if multi-hop
    q = build_manual_quote(wallet_key, token_in, token_out, amount_in, slippage_bps)

    if not _is_single_hop_display(q.path_text):
        # Safety: we only enable single-hop sends through TE.swap_v3_exact_input_once
        raise RuntimeError("Multi-hop send not enabled (path: %s)" % q.path_text)

    fee = _fee_from_path_text(q.path_text, default_fee=500)

    # Resolve addresses/decimals + compute amount_in_wei
    sym_in_norm  = _norm_in(token_in)
    sym_out_norm = _norm_in(token_out)
    addr_in  = _addr(sym_in_norm)
    addr_out = _addr(sym_out_norm)
    dec_in   = _dec(sym_in_norm)
    amount_in_wei = int(Decimal(amount_in) * (Decimal(10) ** dec_in))

    # 1) Approval (EXACT amount, no unlimited)
    TE.approve_if_needed(wallet_key, addr_in, _router_addr(), int(amount_in_wei))

    # 2) Execute (sign+send) via executor’s exactInput wrapper
    send_res = TE.swap_v3_exact_input_once(
        wallet_key=wallet_key,
        token_in=addr_in,
        token_out=addr_out,
        amount_in_wei=int(amount_in_wei),
        fee=int(fee),
        slippage_bps=int(slippage_bps),
        deadline_s=600
    )
    txh = send_res.get("tx_hash", "")
    filled = f"Executed manual swap {amount_in:,.2f} {token_in} → {token_out}"
    return {
        "tx_hash": txh,
        "filled_text": filled,
        "gas_used": 0,            # not returned by the executor; explorer shows final gas
        "explorer_url": "",       # alert already includes clickable explorer link
    }

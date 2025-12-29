# /bot/runner.py
# Minimal dryrun stubs (kept) + real manual quote/execute wired to trade_executor
# Extended with ONE<->WONE wrap/unwrap support and gas tracking.

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
        tx_hash="0x" + "ab" * 16,
        filled_text="Filled: 1,500.00 USDC → 1,499.92 sDAI",
        gas_used=212144,
        explorer_url="https://explorer.harmony.one/tx/0x" + "ab" * 16,
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


# ERC-20 Transfer event topic (keccak256)
_TRANSFER_TOPIC = Web3.keccak(text="Transfer(address,address,uint256)").hex()

def _extract_actual_out_wei(tx_hash: str, token_out_addr: str, recipient: str) -> int:
    """Sum ERC-20 Transfer logs for token_out where `to` == recipient."""
    if not tx_hash:
        return 0
    try:
        receipt = TE.w3.eth.get_transaction_receipt(tx_hash)
    except Exception:
        return 0

    tok = Web3.to_checksum_address(token_out_addr)
    to_addr = Web3.to_checksum_address(recipient)

    total = 0
    for lg in getattr(receipt, "logs", []) or []:
        try:
            if Web3.to_checksum_address(lg["address"]) != tok:
                continue
            topics = lg.get("topics", [])
            if not topics or topics[0].hex().lower() != _TRANSFER_TOPIC.lower():
                continue
            # topics[2] is indexed 'to' (32 bytes)
            if len(topics) < 3:
                continue
            to_topic = "0x" + topics[2].hex()[-40:]
            if Web3.to_checksum_address(to_topic) != to_addr:
                continue
            # data is value (uint256)
            val = int(lg.get("data", "0x0"), 16)
            total += val
        except Exception:
            continue
    return int(total)


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
        return "WONE"  # keep separate from ONE in UI
    if u == "1SDAI":
        return "1sDAI"
    return u

def _addr(sym: str) -> str:
    """
    Resolve symbol to checksum address via config first, then TE.FALLBACK_TOKENS.
    Case-insensitive matching; supports 1sDAI/1SDAI, ONE/WONE, etc.
    """
    s = _canon(sym)            # e.g. "1sDAI" or "WONE"
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

# ---------- WONE wrap/unwrap helpers ----------
# Simple WONE ABI subset (deposit/withdraw) — Harmony’s WONE is WETH-style.
_WONE_ABI = [
    {
        "name": "deposit",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [],
        "outputs": []
    },
    {
        "name": "withdraw",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "wad", "type": "uint256"}],
        "outputs": []
    },
]

def _wone_address() -> str:
    return _addr("WONE")

def _wone_contract():
    return TE.w3.eth.contract(
        address=Web3.to_checksum_address(_wone_address()),
        abi=_WONE_ABI
    )

def _gas_cost_summary(gas_used: int, gas_price_wei: Optional[int]) -> Dict[str, Any]:
    """
    Compute gas cost in ONE given gasUsed and gasPrice (wei).
    Returns dict with 'gas_used', 'gas_cost_wei', 'gas_cost_one' (Decimal).
    """
    if not gas_used or not gas_price_wei or gas_used <= 0 or gas_price_wei <= 0:
        return {"gas_used": int(gas_used or 0), "gas_cost_wei": 0, "gas_cost_one": Decimal("0")}
    cost_wei = int(gas_used) * int(gas_price_wei)
    cost_one = Decimal(cost_wei) / (Decimal(10) ** 18)
    return {"gas_used": int(gas_used), "gas_cost_wei": cost_wei, "gas_cost_one": cost_one}

def _wrap_one(wallet_key: str, amount: Decimal) -> Dict[str, Any]:
    """
    Wrap native ONE into WONE by calling deposit() on the WONE contract.
    This sends ONE from the strategy wallet and mints equal WONE.
    """
    if amount <= 0:
        return {"tx_hash": "", "gas_used": 0, "gas_cost_wei": 0, "gas_cost_one": Decimal("0")}

    acct = TE._get_account(wallet_key)
    from_addr = Web3.to_checksum_address(acct.address)
    wone = _wone_contract()

    dec = 18  # native ONE decimals
    amount_wei = int((amount * (Decimal(10) ** dec)).to_integral_value(rounding=ROUND_DOWN))

    fn = wone.functions.deposit()
    try:
        data = fn._encode_transaction_data()
    except AttributeError:
        data = fn.encode_abi()

    gas_price = TE._current_gas_price_wei_capped() if hasattr(TE, "_current_gas_price_wei_capped") else W.suggest_gas_price_wei()

    tx = {
        "to": Web3.to_checksum_address(_wone_address()),
        "value": amount_wei,
        "data": data,
        "chainId": TE.HMY_CHAIN_ID,
        "nonce": TE.w3.eth.get_transaction_count(from_addr),
        "gasPrice": int(gas_price),
    }

    try:
        est = TE.w3.eth.estimate_gas({**tx, "from": from_addr})
        tx["gas"] = max(int(est * 1.2), 80_000)
    except Exception:
        tx["gas"] = 120_000

    signed = acct.sign_transaction(tx)
    txh = TE.w3.eth.send_raw_transaction(signed.raw_transaction).hex()

    gas_used = 0
    gp_effective = int(gas_price)
    try:
        r = TE.w3.eth.wait_for_transaction_receipt(txh, timeout=180)
        gas_used = int(getattr(r, "gasUsed", 0))
        # effectiveGasPrice may exist on some nodes
        gp_effective = int(getattr(r, "effectiveGasPrice", getattr(r, "gasPrice", gas_price)))
    except Exception:
        pass

    gas_info = _gas_cost_summary(gas_used, gp_effective)
    gas_info["tx_hash"] = txh
    return gas_info

def _unwrap_one(wallet_key: str, amount: Decimal) -> Dict[str, Any]:
    """
    Unwrap WONE into native ONE by calling withdraw(wad) on the WONE contract.
    NOTE: Uses the provided Decimal amount (commonly minOut). Any extra WONE
    from the swap (if actualOut > minOut) remains as WONE, which is safe.
    """
    if amount <= 0:
        return {"tx_hash": "", "gas_used": 0, "gas_cost_wei": 0, "gas_cost_one": Decimal("0")}

    acct = TE._get_account(wallet_key)
    from_addr = Web3.to_checksum_address(acct.address)
    wone = _wone_contract()

    dec = 18
    amount_wei = int((amount * (Decimal(10) ** dec)).to_integral_value(rounding=ROUND_DOWN))

    fn = wone.functions.withdraw(int(amount_wei))
    try:
        data = fn._encode_transaction_data()
    except AttributeError:
        data = fn.encode_abi()

    gas_price = TE._current_gas_price_wei_capped() if hasattr(TE, "_current_gas_price_wei_capped") else W.suggest_gas_price_wei()

    tx = {
        "to": Web3.to_checksum_address(_wone_address()),
        "value": 0,
        "data": data,
        "chainId": TE.HMY_CHAIN_ID,
        "nonce": TE.w3.eth.get_transaction_count(from_addr),
        "gasPrice": int(gas_price),
    }

    try:
        est = TE.w3.eth.estimate_gas({**tx, "from": from_addr})
        tx["gas"] = max(int(est * 1.2), 80_000)
    except Exception:
        tx["gas"] = 120_000

    signed = acct.sign_transaction(tx)
    txh = TE.w3.eth.send_raw_transaction(signed.raw_transaction).hex()

    gas_used = 0
    gp_effective = int(gas_price)
    try:
        r = TE.w3.eth.wait_for_transaction_receipt(txh, timeout=180)
        gas_used = int(getattr(r, "gasUsed", 0))
        gp_effective = int(getattr(r, "effectiveGasPrice", getattr(r, "gasPrice", gas_price)))
    except Exception:
        pass

    gas_info = _gas_cost_summary(gas_used, gp_effective)
    gas_info["tx_hash"] = txh
    return gas_info

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
    quote_out_text = (
        f"{_fmt_amt(token_out, quoted_out)} {_display_sym(token_out)}"
        if quoted_out is not None else f"~? {_display_sym(token_out)}"
    )
    min_out_text   = (
        f"{_fmt_amt(token_out, min_out)} {_display_sym(token_out)}"
        if min_out is not None else f"? {_display_sym(token_out)}"
    )

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
        # We use Quoter for the preview quote (already done in slippage), now encode swap
        router_c = TE.w3.eth.contract(address=router, abi=TE.ROUTER_EXACT_INPUT_ABI)
        fn = router_c.functions.exactInput(
            path_bytes,
            int(amount_in_wei),
            int(min_out_wei),
            Web3.to_checksum_address(owner),
            int(TE.time.time()) + 600
        )
        try:
            data = fn._encode_transaction_data()
        except AttributeError:
            data = fn.encode_abi()
        # Estimate gas with headroom inline (like TE.swap_v3_exact_input_once)
        tx = {
            "to": router,
            "value": 0,
            "data": data,
            "chainId": TE.HMY_CHAIN_ID,
            "nonce": TE.w3.eth.get_transaction_count(Web3.to_checksum_address(owner)),
            "gasPrice": (
                TE._current_gas_price_wei_capped()
                if hasattr(TE, "_current_gas_price_wei_capped")
                else W.suggest_gas_price_wei()
            ),
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
    """
    Execute a single-hop v3 swap with optional ONE<->WONE wrapping:
      - If FROM = ONE: wrap ONE -> WONE, then trade WONE -> token_out
      - If TO   = ONE: trade token_in -> WONE, then unwrap WONE -> ONE (minOut)
    Returns tx hash, filled text, total gas used, gas cost in ONE, explorer URL.
    """

    owner = W.WALLETS.get(wallet_key) or ""
    if not owner:
        raise RuntimeError(f"Unknown wallet key: {wallet_key}")

    base_in  = token_in.upper()
    base_out = token_out.upper()

    addr_in  = _addr(token_in)
    addr_out = _addr(token_out)
    router   = _router_addr()

    dec_in  = _dec(token_in)
    dec_out = _dec(token_out)

    amount_in_wei = int((amount_in * (Decimal(10) ** dec_in)).to_integral_value(rounding=ROUND_DOWN))

    total_gas_used = 0
    total_cost_wei = 0

    # 1) If FROM = ONE (native), wrap into WONE first.
    if base_in == "ONE":
        wrap_info = _wrap_one(wallet_key, amount_in)
        total_gas_used += int(wrap_info.get("gas_used", 0))
        total_cost_wei += int(wrap_info.get("gas_cost_wei", 0))
        # After wrap, on-chain we now spend WONE for the swap.
        addr_in = _addr("WONE")
        dec_in = 18
        amount_in_wei = int((amount_in * (Decimal(10) ** dec_in)).to_integral_value(rounding=ROUND_DOWN))

    # Approve exact if needed (no unlimited)
    # NOTE: Manual trades do NOT auto-approve. Telegram UI must explicitly set allowance.

    # Single-hop send (current executor supports one hop cleanly)
    fee = _fee_for_pair(token_in, token_out)
    res = TE.swap_v3_exact_input_once(
        wallet_key=wallet_key,
        token_in=addr_in,
        token_out=_addr("WONE") if base_out == "ONE" else addr_out,
        amount_in_wei=int(amount_in_wei),
        fee=int(fee),
        slippage_bps=int(slippage_bps),
        deadline_s=600
    )
    , auto_approve=False
)
    txh = res.get("tx_hash", "")
    min_out_wei = int(res.get("amount_out_min", "0") or 0)
    min_out = (Decimal(min_out_wei) / (Decimal(10) ** dec_out)) if min_out_wei > 0 else Decimal("0")

    # Read gas for the swap itself
    gas_swap = 0
    gp_swap = None
    if txh:
        try:
            r = TE.w3.eth.wait_for_transaction_receipt(txh, timeout=180)
            gas_swap = int(getattr(r, "gasUsed", 0))
            gp_swap = int(getattr(r, "effectiveGasPrice", getattr(r, "gasPrice", 0)))
        except Exception:
            gas_swap = 0
    gas_swap_info = _gas_cost_summary(gas_swap, gp_swap or 0)
    total_gas_used += gas_swap_info["gas_used"]
    total_cost_wei += gas_swap_info["gas_cost_wei"]

    # 2) If TO = ONE, unwrap WONE -> ONE using minOut as safe lower bound
    unwrap_info = {"gas_used": 0, "gas_cost_wei": 0, "gas_cost_one": Decimal("0"), "tx_hash": ""}
    if base_out == "ONE" and min_out > 0:
        unwrap_info = _unwrap_one(wallet_key, min_out)
        total_gas_used += int(unwrap_info.get("gas_used", 0))
        total_cost_wei += int(unwrap_info.get("gas_cost_wei", 0))

    # Convert total gas cost to ONE
    gas_cost_one = Decimal("0")
    if total_cost_wei > 0:
        gas_cost_one = Decimal(total_cost_wei) / (Decimal(10) ** 18)

    # Build output message
    display_in  = _display_sym(token_in)
    display_out = _display_sym(token_out)

    filled = (
        f"Sent {_fmt_amt(token_in, amount_in)} {display_in} → "
        f"{_fmt_amt(token_out, (Decimal(actual_out_wei) / (Decimal(10) ** dec_out)) if actual_out_wei else min_out)} {display_out}"
        + (f" (min {_fmt_amt(token_out, min_out)} {display_out})" if actual_out_wei else "")
    )

    return {
        "tx_hash": txh,
        "filled_text": filled,
        "gas_used": int(total_gas_used),
        "gas_cost_one": f"{gas_cost_one:.6f}",
        "explorer_url": f"https://explorer.harmony.one/tx/{txh}" if txh else "",
    }
    \"trade_tx_hash\": txh,
    \"approval_tx_hash\": \"\",  # manual trades should not auto-approve
    \"actual_out_wei\": str(int(actual_out_wei or 0)),
    \"min_out_wei\": str(int(min_out_wei or 0)),
    \"token_out_decimals\": int(dec_out),
    \"token_out_addr\": (_addr(\"WONE\") if base_out == \"ONE\" else addr_out),
}

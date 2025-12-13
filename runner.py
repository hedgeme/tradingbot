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
    txh = "0x" + "ab" * 16
    return ExecResult(
        tx_hash=txh,
        filled_text="Filled: 1,500.00 USDC → 1,499.92 sDAI",
        gas_used=212144,
        explorer_url=f"https://explorer.harmony.one/tx/{txh}?shard=0",
    )

# ---------------------------------------------------------------------------------
# MANUAL TRADE SUPPORT FOR /trade
# ---------------------------------------------------------------------------------
try:
    import config as C
except Exception:
    from app import config as C

from app import slippage as SLMOD
from app import wallet as W
import trade_executor as TE
from web3 import Web3

def _canon(sym: str) -> str:
    s = (sym or "").strip()
    u = s.upper()
    if u == "ONE":
        return "WONE"
    if u == "1SDAI":
        return "1sDAI"
    return u

def _display_sym(sym: str) -> str:
    u = (sym or "").upper()
    if u == "1SDAI":
        return "1sDAI"
    return u

def _addr(sym: str) -> str:
    s = _canon(sym)
    su = s.upper()

    tok_map = {k.upper(): v for k, v in getattr(C, "TOKENS", {}).items()}
    if su in tok_map:
        return Web3.to_checksum_address(tok_map[su])

    fe_map = {k.upper(): v for k, v in getattr(TE, "FALLBACK_TOKENS", {}).items()}
    if su in fe_map:
        return Web3.to_checksum_address(fe_map[su])

    raise KeyError(f"Unknown token symbol: {sym}")

def _dec(sym: str) -> int:
    return int(TE.get_decimals(_addr(sym)))

def _fee_for_pair(token_in: str, token_out: str) -> int:
    ins = _canon(token_in)
    outs = _canon(token_out)
    pools = getattr(C, "POOLS_V3", {}) or {}
    for k in pools.keys():
        try:
            pair, fee = k.split("@", 1)
            a, b = pair.split("/", 1)
            if _canon(a) == ins and _canon(b) == outs:
                return int(fee)
        except Exception:
            continue
    return 500

def _fmt_amt(sym: str, val: Decimal) -> str:
    s = sym.upper()
    if s == "1ETH":
        q = Decimal("0.00000001")
        return f"{val.quantize(q, rounding=ROUND_DOWN):f}"
    q = Decimal("0.01")
    return f"{val.quantize(q, rounding=ROUND_DOWN):.2f}"

def _router_addr() -> str:
    return Web3.to_checksum_address(getattr(TE, "ROUTER_ADDR_ETH"))

_WONE_ABI = [
    {"name": "deposit", "type": "function", "stateMutability": "payable", "inputs": [], "outputs": []},
    {"name": "withdraw", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "wad", "type": "uint256"}], "outputs": []},
]

def _wone_contract():
    return TE.w3.eth.contract(address=Web3.to_checksum_address(_addr("WONE")), abi=_WONE_ABI)

def _gas_cost_summary(gas_used: int, gas_price_wei: int) -> Dict[str, Any]:
    if not gas_used or not gas_price_wei or gas_used <= 0 or gas_price_wei <= 0:
        return {"gas_used": int(gas_used or 0), "gas_cost_wei": 0, "gas_cost_one": Decimal("0")}
    cost_wei = int(gas_used) * int(gas_price_wei)
    cost_one = Decimal(cost_wei) / (Decimal(10) ** 18)
    return {"gas_used": int(gas_used), "gas_cost_wei": cost_wei, "gas_cost_one": cost_one}

def _wrap_one(wallet_key: str, amount: Decimal) -> Dict[str, Any]:
    if amount <= 0:
        return {"tx_hash": "", "gas_used": 0, "gas_cost_wei": 0, "gas_cost_one": Decimal("0")}

    acct = TE._get_account(wallet_key)
    from_addr = Web3.to_checksum_address(acct.address)
    wone = _wone_contract()

    amount_wei = int((amount * (Decimal(10) ** 18)).to_integral_value(rounding=ROUND_DOWN))
    fn = wone.functions.deposit()
    try:
        data = fn._encode_transaction_data()
    except AttributeError:
        data = fn.encode_abi()

    gas_price = TE._current_gas_price_wei_capped()
    tx = {
        "to": Web3.to_checksum_address(_addr("WONE")),
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
        gp_effective = int(getattr(r, "effectiveGasPrice", getattr(r, "gasPrice", gas_price)))
    except Exception:
        pass

    out = _gas_cost_summary(gas_used, gp_effective)
    out["tx_hash"] = txh
    return out

def _unwrap_one(wallet_key: str, amount: Decimal) -> Dict[str, Any]:
    if amount <= 0:
        return {"tx_hash": "", "gas_used": 0, "gas_cost_wei": 0, "gas_cost_one": Decimal("0")}

    acct = TE._get_account(wallet_key)
    from_addr = Web3.to_checksum_address(acct.address)
    wone = _wone_contract()

    amount_wei = int((amount * (Decimal(10) ** 18)).to_integral_value(rounding=ROUND_DOWN))
    fn = wone.functions.withdraw(int(amount_wei))
    try:
        data = fn._encode_transaction_data()
    except AttributeError:
        data = fn.encode_abi()

    gas_price = TE._current_gas_price_wei_capped()
    tx = {
        "to": Web3.to_checksum_address(_addr("WONE")),
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

    out = _gas_cost_summary(gas_used, gp_effective)
    out["tx_hash"] = txh
    return out

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

def build_manual_quote(
    wallet_key: str,
    token_in: str,
    token_out: str,
    amount_in: Decimal,
    slippage_bps: int
) -> ManualQuoteResult:

    t_in_norm  = _canon(token_in)
    t_out_norm = _canon(token_out)

    slip = SLMOD.compute_slippage(
        t_in_norm,
        t_out_norm,
        amount_in,
        slippage_bps=slippage_bps
    )

    if not slip:
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

    quoted_out  = slip.get("amount_out")   # Decimal
    min_out     = slip.get("min_out")      # Decimal
    impact_bps  = slip.get("impact_bps")   # float

    fee = _fee_for_pair(t_in_norm, t_out_norm)
    path_text = f"{_display_sym(token_in)}@{fee}→{_display_sym(token_out)}"

    slippage_ok = True
    try:
        if impact_bps is not None and slippage_bps is not None:
            if float(impact_bps) > float(slippage_bps):
                slippage_ok = False
    except Exception:
        pass

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

    if min_out is None and quoted_out is not None:
        min_out = quoted_out * (Decimal(10000 - int(slippage_bps)) / Decimal(10000))

    min_out_wei = 1
    if min_out is not None:
        min_out_wei = int((min_out * (Decimal(10) ** dec_out)).to_integral_value(rounding=ROUND_DOWN))

    router = _router_addr()

    allowance = TE.get_allowance(owner, addr_in, router)
    allowance_ok = int(allowance) >= int(amount_in_wei)
    approve_text = None if allowance_ok else f"{_fmt_amt(token_in, amount_in)} {_display_sym(token_in)}"

    fee = _fee_for_pair(token_in, token_out)
    path_bytes = TE._v3_path_bytes(addr_in, fee, addr_out)

    gas_price = TE._current_gas_price_wei_capped()
    nonce = TE.w3.eth.get_transaction_count(Web3.to_checksum_address(owner))

    # Build exactInput(params) call correctly (single tuple param, no deadline)
    router_c = TE.w3.eth.contract(address=router, abi=TE.ROUTER_EXACT_INPUT_ABI)
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
        "nonce": int(nonce),
        "gasPrice": int(gas_price),
    }

    try:
        est = TE.w3.eth.estimate_gas({**tx, "from": Web3.to_checksum_address(owner)})
        gas_estimate = max(min(int(est * 1.5), 1_500_000), 300_000)
    except Exception:
        gas_estimate = 300_000

    tx_preview_text = (
        f"exactInput(path=[{_display_sym(token_in)}@{fee}→{_display_sym(token_out)}], "
        f"amountIn={amount_in_wei}, amountOutMin={min_out_wei})"
    )

    return {
        "gas_estimate": int(gas_estimate),
        "allowance_ok": allowance_ok,
        "approve_amount_text": approve_text,
        "nonce": int(nonce),
        "tx_preview_text": tx_preview_text,
    }

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

    if base_in == "ONE":
        wrap_info = _wrap_one(wallet_key, amount_in)
        total_gas_used += int(wrap_info.get("gas_used", 0))
        total_cost_wei += int(wrap_info.get("gas_cost_wei", 0))
        addr_in = _addr("WONE")
        dec_in = 18
        amount_in_wei = int((amount_in * (Decimal(10) ** dec_in)).to_integral_value(rounding=ROUND_DOWN))

    TE.approve_if_needed(wallet_key, addr_in, router, int(amount_in_wei), send_alerts=False)

    fee = _fee_for_pair(token_in, token_out)

    # If user wants native ONE out, we swap to WONE then unwrap minOut
    swap_out_addr = _addr("WONE") if base_out == "ONE" else addr_out
    res = TE.swap_v3_exact_input_once(
        wallet_key=wallet_key,
        token_in=addr_in,
        token_out=swap_out_addr,
        amount_in_wei=int(amount_in_wei),
        fee=int(fee),
        slippage_bps=int(slippage_bps),
        deadline_s=600,
        send_alerts=False,
    )

    txh = res.get("tx_hash", "")
    min_out_wei = int(res.get("amount_out_min", "0") or 0)

    # For ONE-out case, min_out_wei is WONE(18). For normal case, use token_out decimals.
    out_decimals = 18 if base_out == "ONE" else dec_out
    min_out = (Decimal(min_out_wei) / (Decimal(10) ** out_decimals)) if min_out_wei > 0 else Decimal("0")

    gas_swap = 0
    gp_swap = 0
    if txh:
        try:
            r = TE.w3.eth.wait_for_transaction_receipt(txh, timeout=180)
            gas_swap = int(getattr(r, "gasUsed", 0))
            gp_swap = int(getattr(r, "effectiveGasPrice", getattr(r, "gasPrice", 0)))
        except Exception:
            gas_swap = 0
            gp_swap = 0

    gas_swap_info = _gas_cost_summary(gas_swap, gp_swap)
    total_gas_used += gas_swap_info["gas_used"]
    total_cost_wei += gas_swap_info["gas_cost_wei"]

    if base_out == "ONE" and min_out > 0:
        unwrap_info = _unwrap_one(wallet_key, min_out)
        total_gas_used += int(unwrap_info.get("gas_used", 0))
        total_cost_wei += int(unwrap_info.get("gas_cost_wei", 0))

    gas_cost_one = Decimal("0")
    if total_cost_wei > 0:
        gas_cost_one = Decimal(total_cost_wei) / (Decimal(10) ** 18)

    filled = (
        f"Sent {_fmt_amt(token_in, amount_in)} {_display_sym(token_in)} → "
        f"min {_fmt_amt(token_out, min_out)} {_display_sym(token_out)}"
    )

    explorer = f"https://explorer.harmony.one/tx/{txh}?shard=0" if txh else ""

    return {
        "tx_hash": txh,
        "filled_text": filled,
        "gas_used": int(total_gas_used),
        "gas_cost_one": f"{gas_cost_one:.6f}",
        "explorer_url": explorer,
    }

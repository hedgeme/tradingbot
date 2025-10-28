# Minimal runner stub for /dryrun + execution callback
from dataclasses import dataclass
from typing import List

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
#
# These helpers let Telegram request:
#   1) a one-off "dry run"-style quote for an arbitrary wallet/amount/route/slippage
#   2) execution of that exact quote
#
# This is intentionally modeled after the objects returned by build_dryrun()
# and after execute_action(), because /dryrun is already known-good.
#
# IMPORTANT:
# - We DO NOT broadcast in build_manual_quote().
# - We DO broadcast in execute_manual_quote(), but only after Telegram confirms
#   and only if the caller is admin.
#
# You will need to fill in the HOOK HERE sections to connect to your existing
# builder logic for gas/allowance and tx execution, using the same code paths
# build_dryrun() and execute_action() already use.
# ---------------------------------------------------------------------------------

from decimal import Decimal
from typing import Optional, Dict, Any, NamedTuple

try:
    from app import slippage as SLMOD
except Exception:
    import app.slippage as SLMOD  # if direct import fails

# symbol normalization for routing: user says "ONE", pools use "WONE"
def _norm_in(sym: str) -> str:
    s = sym.upper()
    if s == "ONE":
        return "WONE"
    return s

def _norm_out(sym: str) -> str:
    # for user display we prefer "ONE" instead of "WONE"
    s = sym.upper()
    if s == "WONE":
        return "ONE"
    return s


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

    # extras we also want to surface
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
    Produce a dryrun-style preview for a specific manual trade request.

    Inputs:
      wallet_key    "tecbot_tec", "tecbot_usdc", etc.
      token_in      e.g. "TEC", "1USDC", "ONE"
      token_out     e.g. "1USDC", "1ETH", "1sDAI"
      amount_in     Decimal("100")  # human units
      slippage_bps  e.g. 50 for 0.50%

    Output:
      ManualQuoteResult which matches what /trade wants to show.

    This should:
      - get a quoted amount_out + path_text + impact_bps from slippage.compute_slippage(...)
      - compute min_out with the provided slippage_bps
      - check allowance for wallet_key vs router for EXACT amount_in
      - estimate gas (swap tx) + next nonce
      - generate a tx_preview_text string similar to build_dryrun()

    NOTE:
      We DO NOT broadcast here.
    """

    # normalize ONE -> WONE for routing/quoting. caller still sees "ONE".
    t_in_norm  = _norm_in(token_in)
    t_out_norm = _norm_in(token_out)

    # 1) ask slippage.compute_slippage for live quote + path
    # compute_slippage returns:
    #   {
    #     "amount_out": Decimal(...),
    #     "min_out": Decimal(...),
    #     "slippage_bps": int,
    #     "impact_bps": float,
    #     "path_text": "1USDC → WONE@500 → 1sDAI@500",
    #     ...
    #   }
    slip_info = SLMOD.compute_slippage(
        t_in_norm,
        t_out_norm,
        amount_in,
        slippage_bps=slippage_bps
    )
    if not slip_info:
        # We failed to quote. Return a sentinel ManualQuoteResult with obviously-bad fields.
        return ManualQuoteResult(
            action_id="manual",
            bot=wallet_key,
            path_text=f"{token_in} → {token_out}",
            amount_in_text=f"{amount_in} {token_in}",
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

    # Turn WONE back into ONE in the displayed path_text
    # e.g. "WONE@500" -> "ONE@500", "WONE" -> "ONE"
    def _path_display_fix(p: str) -> str:
        # very cheap replace that keeps fee annotations
        return p.replace("WONE", "ONE")

    path_text_disp = _path_display_fix(path_text_route)

    # Human text versions:
    # We mimic /dryrun style, not scientific formatting
    def _fmt_amt(sym: str, val: Decimal) -> str:
        # we align with telegram_listener._fmt_amt rules but we don't import telegram_listener here
        # quick heuristic:
        if sym.upper() == "1ETH":
            return f"{val:.8f}".rstrip("0").rstrip(".")
        # show 2 decimals for most
        return f"{val:,.2f}"

    amount_in_text  = f"{_fmt_amt(token_in, amount_in)} {token_in}"
    quote_out_text  = f"{_fmt_amt(token_out, quoted_out)} {token_out}" if quoted_out is not None else f"~? {token_out}"
    min_out_text    = f"{_fmt_amt(token_out, min_out_amt)} {token_out}" if min_out_amt is not None else f"? {token_out}"

    # Slippage sanity: if impact already worse than our chosen slippage_bps, block execution
    # We infer: "impact_bps" is positive bps of price impact.
    # If impact_bps > slippage_bps, warn/block.
    slippage_ok_flag = True
    try:
        if impact_bps_val is not None and slippage_bps is not None:
            if float(impact_bps_val) > float(slippage_bps):
                slippage_ok_flag = False
    except Exception:
        pass

    # 2) Allowance / gas / nonce / tx_preview
    # We now call into internal/private helpers that build_dryrun() already uses.
    # You MUST hook these calls up to real code from your runner.
    # Pseudocode:
    #   details = _internal_prepare_trade(wallet_key, t_in_norm, t_out_norm, amount_in, slippage_bps)
    #
    # details should include:
    #   gas_estimate: int
    #   allowance_ok: bool
    #   approve_amount_text: "100 TEC" if not ok else None
    #   nonce: int
    #   tx_preview_text: string describing the calldata (like build_dryrun())
    #
    # For now we leave a skeleton and expect you to fill it.

    details = _prepare_manual_trade_for_wallet(
        wallet_key=wallet_key,
        token_in=t_in_norm,
        token_out=t_out_norm,
        amount_in=amount_in,
        slippage_bps=slippage_bps,
        quoted_out=quoted_out,
        min_out=min_out_amt,
    )

    gas_estimate_val   = details.get("gas_estimate", 0)
    allowance_ok_flag  = details.get("allowance_ok", False)
    approval_text      = details.get("approve_amount_text")
    next_nonce         = details.get("nonce", 0)
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
    INTERNAL SUPPORT:
    This function should mirror whatever build_dryrun() already does to:
      - check allowance for 'token_in' in 'wallet_key' vs the router
      - generate calldata to perform the swap with 'amount_in' and 'min_out'
      - estimate gas for that calldata
      - get next nonce for the wallet
      - render tx_preview_text similar to tx_preview_text in dryrun

    IMPORTANT:
      You MUST implement the internals by reusing your existing code.
      Below is a skeleton with the shape we expect back.
    """

    # TODO: Replace all these placeholders by calling into your existing logic.
    # The point is to NOT guess the contract interaction in telegram_listener.py.
    #
    # 1. Check allowance for wallet_key -> router for 'token_in' amount_in
    # 2. Build swap tx dict/txdata exactly like build_dryrun() does internally
    # 3. Gas estimate (call w3.eth.estimate_gas or your helper)
    # 4. Next nonce for that wallet (w3.eth.get_transaction_count)
    # 5. tx_preview_text that matches build_dryrun()['tx_preview_text']

    fake_allowance_ok = True
    fake_gas_estimate = 210000
    fake_nonce        = 0
    fake_preview      = "swapExactTokensForTokens(...)"  # Replace with real preview
    fake_approve_amt  = None  # e.g. "100 TEC" when allowance is short

    return {
        "gas_estimate": fake_gas_estimate,
        "allowance_ok": fake_allowance_ok,
        "approve_amount_text": fake_approve_amt,
        "nonce": fake_nonce,
        "tx_preview_text": fake_preview,
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

    This is analogous to execute_action(action_id),
    but instead of referencing a planner action_id,
    we execute the manually-specified swap immediately.

    Return dict should look like what on_exec_confirm() expects:
      {
        "tx_hash": "0x...",
        "filled_text": "Sold 100 TEC → 432.10 1USDC",
        "gas_used": 212345,
        "explorer_url": "https://...."
      }

    You MUST hook this into the same signing/broadcast path execute_action() uses.
    """
    # TODO: call into your existing trade execution logic (likely trade_executor.py),
    # passing wallet_key, path, amount_in, slippage_bps. Use minOut from slippage math.
    #
    # For now we return a stub dict so telegram_listener can compile. Replace this.

    return {
        "tx_hash": "0xTODO",
        "filled_text": f"Executed manual swap {amount_in} {token_in} → {token_out}",
        "gas_used": 0,
        "explorer_url": "",
    }


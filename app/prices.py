# app/prices.py — on-chain quoting using QuoterV2.quoteExactInput(path, amountIn)
from decimal import Decimal, ROUND_DOWN
from typing import List, Tuple, Optional
from web3 import Web3

# tolerant imports (repo supports both /bot and /bot/app)
try:
    from app import config as C
    from app.chain import get_ctx
except Exception:
    import config as C
    from app.chain import get_ctx  # chain.py is under app/ now

# -------- utilities --------

def _addr(sym: str) -> str:
    a = C.TOKENS.get(sym)
    if not a:
        raise KeyError(f"unknown token: {sym}")
    return Web3.to_checksum_address(a)

def _dec(sym: str) -> int:
    d = C.DECIMALS.get(sym)
    if d is None:
        raise KeyError(f"missing decimals for {sym}")
    return int(d)

def _find_pool(sym_in: str, sym_out: str) -> Optional[int]:
    """Return fee (uint24) if we have a direct pool for (sym_in,sym_out) in either order."""
    for key, meta in C.POOLS_V3.items():
        # safety guard: only consider entries that look like "A/B@fee"
        if ("/" not in key) or ("@" not in key):
            continue
        try:
            pair, _fee_label = key.split("@", 1)
            a, b = pair.split("/", 1)
            if {a.upper(), b.upper()} == {sym_in.upper(), sym_out.upper()}:
                return int(meta["fee"])
        except Exception:
            continue
    return None

def _w3_and_quoter():
    ctx = get_ctx(C.HARMONY_RPC)
    q = ctx.w3.eth.contract(
        address=Web3.to_checksum_address(C.QUOTER_ADDR),
        abi=[{
          "inputs":[{"internalType":"bytes","name":"path","type":"bytes"},
                    {"internalType":"uint256","name":"amountIn","type":"uint256"}],
          "name":"quoteExactInput",
          "outputs":[
            {"internalType":"uint256","name":"amountOut","type":"uint256"},
            {"internalType":"uint160","name":"sqrtPriceX96After","type":"uint160"},
            {"internalType":"uint32","name":"initializedTicksCrossed","type":"uint32"},
            {"internalType":"uint256","name":"gasEstimate","type":"uint256"}],
          "stateMutability":"nonpayable","type":"function"
        }]
    )
    return ctx.w3, q

def _fee3(fee: int) -> bytes:
    return int(fee).to_bytes(3, "big")

def _build_path(hops: List[Tuple[str,int,str]]) -> bytes:
    """
    hops: list of (tokenInSym, fee, tokenOutSym), e.g.
          [("1ETH",3000,"WONE"), ("WONE",3000,"1USDC")]
    Encoded as: tokenIn (20) + fee(3) + tokenOut (20) [+ fee + token ...]
    """
    path = b""
    for i, (a, fee, b) in enumerate(hops):
        if i == 0:
            path += Web3.to_bytes(hexstr=_addr(a))
        path += _fee3(fee) + Web3.to_bytes(hexstr=_addr(b))
    return path

def _quote_path(hops: List[Tuple[str,int,str]], amount_in_wei: int) -> Optional[int]:
    if not hops:
        return None
    _, quoter = _w3_and_quoter()
    path = _build_path(hops)
    try:
        amt_out, *_ = quoter.functions.quoteExactInput(path, int(amount_in_wei)).call()
        return int(amt_out)
    except Exception:
        return None

# -------- route policy (based on verified pools) --------

def _route_to_usdc(sym: str) -> Optional[List[Tuple[str,int,str]]]:
    """
    Return the hop list to 1USDC for a given sym, using verified pools:

      1ETH/WONE@3000
      1USDC/WONE@3000
      TEC/WONE@10000
      1USDC/1sDAI@500
      TEC/1sDAI@10000
    """
    s = sym.upper()
    if s == "1USDC":
        return []  # identity

    # direct (if ever added)
    fee = _find_pool(s, "1USDC")
    if fee:
        return [(s, fee, "1USDC")]

    # Known working paths (per diagnostics):
    if s == "WONE":
        return [("WONE", 3000, "1USDC")]
    if s == "1ETH":
        return [("1ETH", 3000, "WONE"), ("WONE", 3000, "1USDC")]
    if s == "1SDAI":
        return [("1sDAI", 500, "1USDC")]
    if s == "TEC":
        return [("TEC", 10000, "1sDAI"), ("1sDAI", 500, "1USDC")]

    return None

# -------- public API --------

def price_usd(sym: str, amount: Decimal) -> Optional[Decimal]:
    """
    Quote `amount` of `sym` into 1USDC using QuoterV2 path quoting.
    Returns Decimal USD (token is 1USDC with 6 decimals) or None.
    """
    route = _route_to_usdc(sym)
    if route is None:
        return None
    if route == []:  # sym == 1USDC
        return amount.quantize(Decimal("0.000001"))

    dec_in = _dec(route[0][0])
    amt_wei = int((amount * (Decimal(10) ** dec_in)).to_integral_value(rounding=ROUND_DOWN))
    out_wei = _quote_path(route, amt_wei)
    if out_wei is None:
        return None

    usd = Decimal(out_wei) / (Decimal(10) ** _dec("1USDC"))  # 6 decimals
    return usd

# re-export helpers used elsewhere / by diagnostics
__all__ = [
    "price_usd",
    "_addr", "_dec", "_find_pool",
    "_build_path", "_quote_path", "_route_to_usdc"
]

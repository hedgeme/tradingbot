# app/balances.py
from __future__ import annotations
from decimal import Decimal
from typing import Dict
from web3 import Web3

# tolerant import: app.config or root config
try:
    from app import config as C
except Exception:
    import config as C

from app.chain import get_ctx

def native_one_balance(wallet: str) -> Decimal:
    ctx = get_ctx(C.HARMONY_RPC)
    wei = ctx.w3.eth.get_balance(Web3.to_checksum_address(wallet))
    return (Decimal(wei) / Decimal(10**18)).quantize(Decimal("0.00000001"))

def erc20_balance(sym: str, wallet: str) -> Decimal:
    sym = sym.upper()
    addr = C.TOKENS[sym]
    dec = int(C.DECIMALS.get(sym, 18))
    ctx = get_ctx(C.HARMONY_RPC)
    erc = ctx.erc20(addr)
    raw = erc.functions.balanceOf(Web3.to_checksum_address(wallet)).call()
    return (Decimal(raw) / Decimal(10**dec)).quantize(Decimal("0.00000001"))

def all_balances() -> Dict[str, Dict[str, Decimal]]:
    out: Dict[str, Dict[str, Decimal]] = {}
    for w_name, w_addr in C.WALLETS.items():
        row: Dict[str, Decimal] = {}
        # native ONE
        try:
            row["ONE(native)"] = native_one_balance(w_addr)
        except Exception:
            row["ONE(native)"] = Decimal(0)
        # ERC-20 tokens
        for sym in C.TOKENS.keys():
            try:
                row[sym] = erc20_balance(sym, w_addr)
            except Exception:
                row[sym] = Decimal(0)
        out[w_name] = row
    return out

#!/usr/bin/env python3
# app/balances.py — unified balance fetcher for ONE(native) + ERC20
#
# Updated for full WONE support

from __future__ import annotations
import os
from decimal import Decimal
from typing import Dict, Any

from web3 import Web3
from app.wallet import w3, WALLETS, get_erc20_decimals, get_erc20_balance_wei, get_native_balance_wei


# ---------------------------------------------------------------------
# Contract addresses for ERC-20 tokens we track
# ---------------------------------------------------------------------
TOKENS: Dict[str, str] = {
    # Wrapped ONE (ERC-20)
    "WONE": Web3.to_checksum_address("0xcF664087a5bB0237a0BAd6742852ec6c8d69A27a"),

    # Harmony tokens
    "1USDC": Web3.to_checksum_address("0xBC594CABd205bD993e7FfA6F3e9ceA75c1110da5"),
    "1ETH":  Web3.to_checksum_address("0x4cC435d7b9557d54d6EF02d69Bbf72634905Bf11"),
    "TEC":   Web3.to_checksum_address("0x0DEB9A1998aAE32dAAcF6de21161c3E942aCe074"),
    "1sDAI": Web3.to_checksum_address("0xeDEb95D51dBc4116039435379Bd58472A2c09b1f"),
}


# ---------------------------------------------------------------------
# Helper: fetch ERC20 balance
# ---------------------------------------------------------------------
def _erc20_human(symbol: str, wallet: str) -> Decimal:
    addr = TOKENS[symbol]
    raw = get_erc20_balance_wei(addr, wallet)
    dec = get_erc20_decimals(addr)
    return Decimal(raw) / (Decimal(10) ** dec)


# ---------------------------------------------------------------------
# Main: fetch balances for all wallets
# Returns:
#   { wallet_name: { "ONE": dec, "WONE": dec, "1USDC": dec, ... } }
# ---------------------------------------------------------------------
def all_balances() -> Dict[str, Dict[str, Decimal]]:
    out: Dict[str, Dict[str, Decimal]] = {}

    for w_name, w_addr in WALLETS.items():
        row: Dict[str, Decimal] = {}

        # Native ONE (always exists)
        try:
            native = get_native_balance_wei(w_addr)
            row["ONE"] = Decimal(native) / Decimal(10**18)
        except Exception:
            row["ONE"] = Decimal(0)

        # ERC20 tokens
        for sym in TOKENS.keys():
            try:
                row[sym] = _erc20_human(sym, w_addr)
            except Exception:
                row[sym] = Decimal(0)

        out[w_name] = row

    return out


__all__ = ["all_balances", "TOKENS"]

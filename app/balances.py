#!/usr/bin/env python3
# app/balances.py — unified balance fetcher for ONE(native) + ERC20
#
# Updated for:
#   - Full WONE support
#   - Only wallets listed in config.WALLETS are shown in /balances

from __future__ import annotations
import os
from decimal import Decimal
from typing import Dict

from web3 import Web3

# Core wallet helpers (RPC + low-level balance functions + default wallet map)
from app.wallet import (
    w3,
    WALLETS as WALLET_DEFAULTS,
    get_erc20_decimals,
    get_erc20_balance_wei,
    get_native_balance_wei,
)

# Try to use config.WALLETS as the authoritative list shown in /balances
try:
    from app import config as C
except Exception:
    import config as C  # type: ignore


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
# Helper: fetch ERC20 balance as Decimal
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
#
# Wallets shown:
#   - Prefer config.WALLETS (what your Telegram bot uses)
#   - Fallback to app.wallet.WALLETS if config has no WALLETS
# ---------------------------------------------------------------------
def all_balances() -> Dict[str, Dict[str, Decimal]]:
    out: Dict[str, Dict[str, Decimal]] = {}

    # Prefer the curated wallet list from config (tecbot_eth, tecbot_usdc, etc.)
    cfg_wallets = getattr(C, "WALLETS", None) or {}
    if cfg_wallets:
        wallets = cfg_wallets
    else:
        wallets = WALLET_DEFAULTS

    for w_name, w_addr in wallets.items():
        if not w_addr:
            # Skip empty / unconfigured wallet entries
            continue

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

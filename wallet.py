# /bot/app/wallet.py
import json
import os
import logging
from pathlib import Path
from typing import Dict, Optional

from dotenv import load_dotenv
from web3 import Web3

# --- Load env early ---
ENV_PATH = "/home/tecviva/.env"
load_dotenv(ENV_PATH)

# --- Basic RPC config ---
HMY_NODE = os.getenv("HMY_NODE", "https://api.s0.t.hmny.io")
HMY_CHAIN_ID = int(os.getenv("HMY_CHAIN_ID", "1666600000"))
GAS_CAP_GWEI = int(os.getenv("GAS_CAP_GWEI", "150"))

# Paths (not strictly needed for balances, kept for compatibility)
BOT_DB_DIR  = Path(os.getenv("BOT_DB_DIR", "/bot/db"))
BOT_LOG_DIR = Path(os.getenv("BOT_LOG_DIR", "/bot/logs"))

# --- Web3 singleton ---
_w3: Optional[Web3] = None
def get_w3() -> Web3:
    global _w3
    if _w3 is None:
        _w3 = Web3(Web3.HTTPProvider(HMY_NODE, request_kwargs={"timeout": 20}))
    return _w3

# --- Address loading helpers ---
def _read_any(*keys: str) -> str:
    """Return the first non-empty environment value among keys; empty string if none."""
    for k in keys:
        v = os.getenv(k, "").strip()
        if v:
            return v
    return ""

def _to_eth_addr(maybe_addr: str) -> str:
    """
    Accept a 0x address (return checksum). If a 'one1...' was provided, raise with guidance
    because we no longer use `hmy` for conversion.
    """
    if not maybe_addr:
        return ""
    if maybe_addr.lower().startswith("one1"):
        raise ValueError(
            f"Got a bech32 ONE address '{maybe_addr}'. "
            f"Please convert it to 0x and put the 0x address in .env "
            f"(e.g. WALLET_*_ADDR=0x...). We no longer use 'hmy' for conversion."
        )
    return Web3.to_checksum_address(maybe_addr)

# Accept old/new variable names; prefer *_ADDR (0x)
_eth_addr   = _read_any("WALLET_ETH_ADDR",   "WALLET_ETH_ONE_ADDR")
_usdc_addr  = _read_any("WALLET_USDC_ADDR",  "WALLET_USDC_ONE_ADDR")
_sdai_addr  = _read_any("WALLET_SDAI_ADDR",  "WALLET_SDAI_ONE_ADDR")
_tec_addr   = _read_any("WALLET_TEC_ADDR",   "WALLET_TEC_ONE_ADDR")

def _safe_checksum(label: str, a: str) -> str:
    if not a:
        return ""
    try:
        return _to_eth_addr(a)
    except Exception as e:
        # Make it obvious in logs *which* var is wrong
        logging.error("Address for %s invalid: %s", label, e)
        raise

WALLETS: Dict[str, Dict[str, str]] = {
    "tecbot_eth":  {"eth": _safe_checksum("WALLET_ETH_ADDR",  _eth_addr)},
    "tecbot_usdc": {"eth": _safe_checksum("WALLET_USDC_ADDR", _usdc_addr)},
    "tecbot_sdai": {"eth": _safe_checksum("WALLET_SDAI_ADDR", _sdai_addr)},
    "tecbot_tec":  {"eth": _safe_checksum("WALLET_TEC_ADDR",  _tec_addr)},
}

# --- Public utils used by other modules/bot ---
def print_wallet_map() -> None:
    """Pretty-print the wallet name -> 0x address mapping (what Telegram/balances will use)."""
    out = {k: v.get("eth", "") for k, v in WALLETS.items()}
    print(json.dumps(out, indent=2))

def get_native_balance_wei(owner_eth: str) -> int:
    """Return ONE balance (wei) for a 0x address. Empty -> 0."""
    if not owner_eth:
        return 0
    w3 = get_w3()
    return int(w3.eth.get_balance(Web3.to_checksum_address(owner_eth)))

def get_erc20_balance_wei(token_addr: str, owner_eth: str) -> int:
    """Read ERC-20 balanceOf owner (wei). Empty owner -> 0."""
    if not owner_eth:
        return 0
    w3 = get_w3()
    token = w3.eth.contract(
        address=Web3.to_checksum_address(token_addr),
        abi=[{
            "constant": True,
            "inputs": [{"name":"owner","type":"address"}],
            "name": "balanceOf",
            "outputs": [{"name":"","type":"uint256"}],
            "type": "function",
        }]
    )
    return int(token.functions.balanceOf(Web3.to_checksum_address(owner_eth)).call())


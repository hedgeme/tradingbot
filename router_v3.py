# /bot/app/router_v3.py — Uniswap V3 router helpers (fork-aware)
# Adapts to routers that include a 'deadline' in their structs by inspecting the ABI.

import json
import time
from typing import Any, Dict, List, Tuple

from web3 import Web3, HTTPProvider
from .config import ROUTER_ADDR, HARMONY_RPC

# ---------------------------------------------------------------------
# Web3 + Router
# ---------------------------------------------------------------------
_W3 = Web3(HTTPProvider(HARMONY_RPC, request_kwargs={"timeout": 8}))

with open("/bot/app/SwapRouter02_minimal.json") as f:
    _ABI = json.load(f)

_ROUTER = _W3.eth.contract(
    address=Web3.to_checksum_address(ROUTER_ADDR),
    abi=_ABI
)

def w3() -> Web3:
    return _W3

def router():
    return _ROUTER

# ---------------------------------------------------------------------
# ABI helpers to detect tuple shapes (7-field vs 8-field with deadline)
# ---------------------------------------------------------------------
def _get_fn_abi(fn_name: str) -> Dict[str, Any]:
    cands = [a for a in _ABI if a.get("type") == "function" and a.get("name") == fn_name]
    if not cands:
        raise ValueError(f"Function {fn_name} not found in ABI")
    for abi in cands:
        ins = abi.get("inputs") or []
        if ins and (ins[0].get("type", "").startswith("(") or ins[0].get("components")):
            return abi
    return cands[0]

def _expects_deadline_in_single(fn_name: str) -> bool:
    abi = _get_fn_abi(fn_name)
    ins = abi.get("inputs") or []
    if not ins:
        return False
    comp = ins[0].get("components") or []
    return len(comp) >= 8  # (address,address,uint24,address,uint256,uint256,uint256,uint160)

def _expects_deadline_in_path(fn_name: str) -> bool:
    abi = _get_fn_abi(fn_name)
    ins = abi.get("inputs") or []
    if not ins:
        return False
    comp = ins[0].get("components") or []
    return len(comp) >= 5  # many forks: add deadline at the end

def _deadline(seconds: int = 1800) -> int:
    return int(time.time()) + int(seconds)

# ---------------------------------------------------------------------
# Path building (standard)
# ---------------------------------------------------------------------
def build_path_bytes(legs: List[Tuple[str, int, str]]) -> bytes:
    """
    legs = [(tokenA, feeAB, tokenB), (tokenB, feeBC, tokenC), ...]
    Returns canonical path bytes: tokenA (20) + fee (3) + tokenB (20) + ...
    """
    out = b""
    for i, (a, fee, b) in enumerate(legs):
        a = Web3.to_checksum_address(a)
        b = Web3.to_checksum_address(b)
        if i == 0:
            out += bytes.fromhex(a[2:])
        out += int(fee).to_bytes(3, "big")
        out += bytes.fromhex(b[2:])
    return out

# ---------------------------------------------------------------------
# Single-hop calldata (fork-aware)
# ---------------------------------------------------------------------
def data_exact_input_single(token_in: str, token_out: str, fee: int, recipient: str,
                            amount_in: int, amount_out_min: int,
                            sqrt_price_limit_x96: int = 0, deadline: int | None = None) -> bytes:
    """
    exactInputSingle:
      - Standard Uniswap V3: (address,address,uint24,address,uint256,uint256,uint160)
      - Some forks (your Harmony router): (address,address,uint24,address,uint256,uint256,uint256,uint160)  # + deadline
    """
    token_in  = Web3.to_checksum_address(token_in)
    token_out = Web3.to_checksum_address(token_out)
    recipient = Web3.to_checksum_address(recipient)
    fee       = int(fee)
    amount_in = int(amount_in)
    amount_out_min = int(amount_out_min)
    sqrt_price_limit_x96 = int(sqrt_price_limit_x96)
    dl = _deadline() if deadline is None else int(deadline)

    if _expects_deadline_in_single("exactInputSingle"):
        tup = (token_in, token_out, fee, recipient, amount_in, amount_out_min, dl, sqrt_price_limit_x96)
    else:
        tup = (token_in, token_out, fee, recipient, amount_in, amount_out_min, sqrt_price_limit_x96)

    fn = _ROUTER.functions.exactInputSingle(tup)
    return fn._encode_transaction_data()

def data_exact_output_single(token_in: str, token_out: str, fee: int, recipient: str,
                             amount_out: int, amount_in_max: int,
                             sqrt_price_limit_x96: int = 0, deadline: int | None = None) -> bytes:
    token_in  = Web3.to_checksum_address(token_in)
    token_out = Web3.to_checksum_address(token_out)
    recipient = Web3.to_checksum_address(recipient)
    fee          = int(fee)
    amount_out   = int(amount_out)
    amount_in_max = int(amount_in_max)
    sqrt_price_limit_x96 = int(sqrt_price_limit_x96)
    dl = _deadline() if deadline is None else int(deadline)

    if _expects_deadline_in_single("exactOutputSingle"):
        tup = (token_in, token_out, fee, recipient, amount_out, amount_in_max, dl, sqrt_price_limit_x96)
    else:
        tup = (token_in, token_out, fee, recipient, amount_out, amount_in_max, sqrt_price_limit_x96)

    fn = _ROUTER.functions.exactOutputSingle(tup)
    return fn._encode_transaction_data()

# ---------------------------------------------------------------------
# Multi-hop calldata (fork-aware)
# ---------------------------------------------------------------------
def data_exact_input(path_bytes: bytes, recipient: str, amount_in: int, amount_out_min: int,
                     deadline: int | None = None) -> bytes:
    recipient = Web3.to_checksum_address(recipient)
    amount_in = int(amount_in)
    amount_out_min = int(amount_out_min)
    dl = _deadline() if deadline is None else int(deadline)

    if _expects_deadline_in_path("exactInput"):
        tup = (bytes(path_bytes), recipient, amount_in, amount_out_min, dl)
    else:
        tup = (bytes(path_bytes), recipient, amount_in, amount_out_min)

    fn = _ROUTER.functions.exactInput(tup)
    return fn._encode_transaction_data()

def data_exact_output(path_bytes: bytes, recipient: str, amount_out: int, amount_in_max: int,
                      deadline: int | None = None) -> bytes:
    recipient    = Web3.to_checksum_address(recipient)
    amount_out   = int(amount_out)
    amount_in_max = int(amount_in_max)
    dl = _deadline() if deadline is None else int(deadline)

    if _expects_deadline_in_path("exactOutput"):
        tup = (bytes(path_bytes), recipient, amount_out, amount_in_max, dl)
    else:
        tup = (bytes(path_bytes), recipient, amount_out, amount_in_max)

    fn = _ROUTER.functions.exactOutput(tup)
    return fn._encode_transaction_data()

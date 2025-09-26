# /bot/app/router_v3.py
# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import List, Tuple, Dict, Any, Optional
import json, os, time, struct

from web3 import Web3, HTTPProvider

def _imp(m):
    try: return __import__(m, fromlist=['*'])
    except Exception: return __import__(f"app.{m}", fromlist=['*'])

config = _imp("config")

APP_DIR = "/bot/app"
ROUTER_ADDR = Web3.to_checksum_address("0x85495f44768ccbb584d9380Cc29149fDAA445F69")  # SwapRouter02 on Harmony (your verified)
ROUTER_ABI_PATH = os.path.join(APP_DIR, "SwapRouter02_minimal.json")

# Load the ABI from your repo file so it stays in sync
with open(ROUTER_ABI_PATH, "r", encoding="utf-8") as f:
    ROUTER_ABI = json.load(f)

def w3() -> Web3:
    rpc = getattr(config, "RPC_URL", "https://api.harmony.one")
    return Web3(HTTPProvider(rpc, request_kwargs={"timeout": 12}))

def router_contract():
    return w3().eth.contract(address=ROUTER_ADDR, abi=ROUTER_ABI)

# ---- V3 path helpers ----
def _addr(a: str) -> bytes:
    return bytes.fromhex(Web3.to_checksum_address(a)[2:])

def _fee_bytes(fee: int) -> bytes:
    # uint24 big-endian
    return struct.pack(">I", int(fee))[1:]

def build_path_bytes(hops: List[Tuple[str, int, str]]) -> bytes:
    """
    hops: [(tokenIn, fee, tokenOut), ...]
    Returns encoded V3 path bytes for exactInput/exactOutput.
    """
    if not hops:
        raise ValueError("empty hops")
    buf = b""
    # first segment includes tokenIn + fee + tokenOut
    t_in, fee, t_out = hops[0]
    buf += _addr(t_in) + _fee_bytes(fee) + _addr(t_out)
    # subsequent segments append fee + tokenOut
    for (t_in, fee, t_out) in hops[1:]:
        buf += _fee_bytes(fee) + _addr(t_out)
    return buf

# ---- Transactions (data only; you sign/send elsewhere) ----
def data_exact_input_single(token_in: str, token_out: str, fee: int,
                            recipient: str, amount_in_wei: int,
                            min_out_wei: int, deadline: Optional[int] = None,
                            sqrtPriceLimitX96: int = 0) -> bytes:
    """
    Build call data for exactInputSingle(params). No signing/sending here.
    """
    deadline = deadline or (int(time.time()) + 600)
    r = router_contract()
    params = {
        "tokenIn": Web3.to_checksum_address(token_in),
        "tokenOut": Web3.to_checksum_address(token_out),
        "fee": int(fee),
        "recipient": Web3.to_checksum_address(recipient),
        "deadline": int(deadline),
        "amountIn": int(amount_in_wei),
        "amountOutMinimum": int(min_out_wei),
        "sqrtPriceLimitX96": int(sqrtPriceLimitX96),
    }
    fn = r.functions.exactInputSingle(params)
    try:
        return fn._encode_transaction_data()
    except AttributeError:
        return fn.encode_abi()

def data_exact_input(path_bytes: bytes, recipient: str,
                     amount_in_wei: int, min_out_wei: int,
                     deadline: Optional[int] = None) -> bytes:
    """
    Build call data for exactInput({path, recipient, deadline, amountIn, amountOutMinimum}).
    """
    deadline = deadline or (int(time.time()) + 600)
    r = router_contract()
    params = {
        "path": path_bytes,
        "recipient": Web3.to_checksum_address(recipient),
        "deadline": int(deadline),
        "amountIn": int(amount_in_wei),
        "amountOutMinimum": int(min_out_wei),
    }
    fn = r.functions.exactInput(params)
    try:
        return fn._encode_transaction_data()
    except AttributeError:
        return fn.encode_abi()

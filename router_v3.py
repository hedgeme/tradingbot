# /bot/app/router_v3.py — minimal Uniswap V3 router helpers for TECBot

import json
from web3 import Web3, HTTPProvider
from .config import ROUTER_ADDR, HARMONY_RPC

# Keep a module-level Web3 with a real HTTPProvider
_W3 = Web3(HTTPProvider(HARMONY_RPC, request_kwargs={"timeout": 8}))
_ROUTER = _W3.eth.contract(
    address=Web3.to_checksum_address(ROUTER_ADDR),
    abi=json.load(open("/bot/app/SwapRouter02_minimal.json"))
)

def w3() -> Web3:
    """Return a connected Web3 instance."""
    return _W3

def router():
    """Return the router contract."""
    return _ROUTER

def build_path_bytes(legs):
    """
    legs = [(tokenA, feeAB, tokenB), (tokenB, feeBC, tokenC), ...]
    Returns the canonical bytes: tokenA (20) + fee (3) + tokenB (20) + ...
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

def data_exact_input_single(token_in, token_out, fee, recipient, amount_in, amount_out_min):
    """
    exactInputSingle((address tokenIn, address tokenOut, uint24 fee, address recipient, uint256 amountIn, uint256 amountOutMinimum))
    NOTE: pass a TUPLE in the exact order required by the ABI.
    """
    tup = (
        Web3.to_checksum_address(token_in),
        Web3.to_checksum_address(token_out),
        int(fee),
        Web3.to_checksum_address(recipient),
        int(amount_in),
        int(amount_out_min),
    )
    fn = _ROUTER.functions.exactInputSingle(tup)
    return fn._encode_transaction_data()

def data_exact_output_single(token_in, token_out, fee, recipient, amount_out, amount_in_max):
    tup = (
        Web3.to_checksum_address(token_in),
        Web3.to_checksum_address(token_out),
        int(fee),
        Web3.to_checksum_address(recipient),
        int(amount_out),
        int(amount_in_max),
    )
    fn = _ROUTER.functions.exactOutputSingle(tup)
    return fn._encode_transaction_data()

def data_exact_input(path_bytes, recipient, amount_in, amount_out_min):
    tup = (
        bytes(path_bytes),
        Web3.to_checksum_address(recipient),
        int(amount_in),
        int(amount_out_min),
    )
    fn = _ROUTER.functions.exactInput(tup)
    return fn._encode_transaction_data()

def data_exact_output(path_bytes, recipient, amount_out, amount_in_max):
    tup = (
        bytes(path_bytes),
        Web3.to_checksum_address(recipient),
        int(amount_out),
        int(amount_in_max),
    )
    fn = _ROUTER.functions.exactOutput(tup)
    return fn._encode_transaction_data()

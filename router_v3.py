# router_v3.py — minimal Uniswap V3 router helpers for TECBot

import json
from web3 import Web3, HTTPProvider
from config import ROUTER_ADDR, HARMONY_RPC

# Load ABI
with open("/bot/app/SwapRouter02_minimal.json") as f:
    ABI = json.load(f)

w3 = Web3(HTTPProvider(HARMONY_RPC, request_kwargs={"timeout": 8}))
ROUTER = w3.eth.contract(address=Web3.to_checksum_address(ROUTER_ADDR), abi=ABI)


def data_exact_input_single(token_in, token_out, fee, recipient, amount_in, amount_out_min):
    """
    Build calldata for exactInputSingle
    (address tokenIn, address tokenOut, uint24 fee,
     address recipient, uint256 amountIn, uint256 amountOutMinimum)
    """
    fn = ROUTER.functions.exactInputSingle((
        Web3.to_checksum_address(token_in),
        Web3.to_checksum_address(token_out),
        fee,
        Web3.to_checksum_address(recipient),
        amount_in,
        amount_out_min,
    ))
    return fn._encode_transaction_data()


def data_exact_output_single(token_in, token_out, fee, recipient, amount_out, amount_in_max):
    """
    Build calldata for exactOutputSingle
    (address tokenIn, address tokenOut, uint24 fee,
     address recipient, uint256 amountOut, uint256 amountInMaximum)
    """
    fn = ROUTER.functions.exactOutputSingle((
        Web3.to_checksum_address(token_in),
        Web3.to_checksum_address(token_out),
        fee,
        Web3.to_checksum_address(recipient),
        amount_out,
        amount_in_max,
    ))
    return fn._encode_transaction_data()


def data_exact_input(path, recipient, amount_in, amount_out_min):
    """
    calldata for exactInput (multi-hop)
    """
    fn = ROUTER.functions.exactInput((
        path,
        Web3.to_checksum_address(recipient),
        amount_in,
        amount_out_min,
    ))
    return fn._encode_transaction_data()


def data_exact_output(path, recipient, amount_out, amount_in_max):
    """
    calldata for exactOutput (multi-hop)
    """
    fn = ROUTER.functions.exactOutput((
        path,
        Web3.to_checksum_address(recipient),
        amount_out,
        amount_in_max,
    ))
    return fn._encode_transaction_data()

#!/usr/bin/env python3
from web3 import Web3
import struct, sys

RPC = "https://api.s0.t.hmny.io"
w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 20}))
QUOTER = Web3.to_checksum_address("0x314456E8F5efaa3dD1F036eD5900508da8A3B382")

QUOTER_ABI = [
    {  # V1 path
        "inputs": [
            {"internalType": "bytes", "name": "path", "type": "bytes"},
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
        ],
        "name": "quoteExactInput",
        "outputs": [{"internalType": "uint256", "name": "amountOut", "type": "uint256"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {  # flat single fallback
        "inputs": [
            {"internalType": "address", "name": "tokenIn", "type": "address"},
            {"internalType": "address", "name": "tokenOut", "type": "address"},
            {"internalType": "uint24", "name": "fee", "type": "uint24"},
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "uint160", "name": "sqrtPriceLimitX96", "type": "uint160"},
        ],
        "name": "quoteExactInputSingle",
        "outputs": [{"internalType": "uint256", "name": "amountOut", "type": "uint256"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]
quoter = w3.eth.contract(address=QUOTER, abi=QUOTER_ABI)

ERC20_ABI = [{
    "constant": True, "inputs": [], "name": "decimals",
    "outputs": [{"name": "", "type": "uint8"}],
    "payable": False, "stateMutability": "view", "type": "function",
}]

TOKENS = {
    "WONE":  Web3.to_checksum_address("0xcF664087a5bB0237a0BAd6742852ec6c8d69A27a"),
    "1ETH":  Web3.to_checksum_address("0x4cc435d7b9557d54d6ef02d69bbf72634905bf11"),
    "1USDC": Web3.to_checksum_address("0xbc594cabd205bd993e7ffa6f3e9cea75c1110da5"),
    "TEC":   Web3.to_checksum_address("0x0deb9a1998aae32daacf6de21161c3e942ace074"),
    "1sDAI": Web3.to_checksum_address("0xedeb95d51dbc4116039435379bd58472a2c09b1f"),
}

# Canonical matrix (single-hop that we actually use)
SINGLE = [
    ("1ETH",  "WONE",  3000),
    ("1USDC", "1sDAI",  500),
    ("1USDC", "WONE",   500),
    ("TEC",   "WONE", 10000),
]
# Multihop we care about (example: 1ETH -> 1USDC via WONE)
MULTIHOP = [
    (("1ETH", 3000, "WONE", 500, "1USDC"),),
    # add reverse if you want:
    # (("1USDC", 500, "WONE", 3000, "1ETH"),),
]

def decimals(addr: str) -> int:
    c = w3.eth.contract(address=addr, abi=ERC20_ABI)
    try:
        return int(c.functions.decimals().call())
    except Exception:
        return 18

def default_amount_in(addr: str) -> int:
    dec = decimals(addr)
    if dec >= 18: return 10**15    # 0.001
    if dec >= 6:  return 10**6     # 1.0
    return 100

def v3_path_bytes(*legs) -> bytes:
    # legs like: (tokenA, feeAB, tokenB, feeBC, tokenC, ...)
    b = b""
    for i, item in enumerate(legs):
        if i % 2 == 0:
            # token address
            a = Web3.to_checksum_address(item)
            b += bytes.fromhex(a[2:])
        else:
            # fee (uint24 big-endian)
            fee = int(item)
            b += (fee.to_bytes(4, "big"))[1:]
    return b

def quote_single(token_in, token_out, fee, amt) -> tuple[str, int]:
    # try V1 path first
    path = v3_path_bytes(token_in, fee, token_out)
    try:
        out = quoter.functions.quoteExactInput(path, amt).call()
        return ("OK-path", out)
    except Exception:
        # fallback to flat
        try:
            out = quoter.functions.quoteExactInputSingle(token_in, token_out, fee, amt, 0).call()
            return ("OK-single", out)
        except Exception as e:
            return ("REVERT", 0)

def quote_multihop(legs, amt) -> tuple[str, int]:
    # legs: tokenA, feeAB, tokenB, feeBC, tokenC, ...
    path = v3_path_bytes(*legs)
    try:
        out = quoter.functions.quoteExactInput(path, amt).call()
        return ("OK-path", out)
    except Exception:
        return ("REVERT", 0)

def main():
    print(f"RPC: {RPC}")
    print(f"Quoter: {QUOTER}\n")

    all_ok = True

    # Single hops
    print("== Single-hop ==")
    for sym_in, sym_out, fee in SINGLE:
        a_in, a_out = TOKENS[sym_in], TOKENS[sym_out]
        amt = default_amount_in(a_in)
        mode, out = quote_single(a_in, a_out, fee, amt)
        ok = (mode != "REVERT") and (out > 0)
        all_ok = all_ok and ok
        badge = "✅" if ok else ("⚠️" if mode != "REVERT" else "❌")
        print(f"{badge} {sym_in}->{sym_out} fee {fee} | in={amt} out={out} ({mode})")

    # Multihop
    print("\n== Multihop ==")
    for (legs,) in MULTIHOP:
        sym_in = legs[0]; a_in = TOKENS[sym_in]
        amt = default_amount_in(a_in)
        mode, out = quote_multihop([TOKENS[s] if i%2==0 else legs[i] for i,s in enumerate(legs)], amt)
        ok = (mode != "REVERT") and (out > 0)
        all_ok = all_ok and ok
        path_str = " | ".join(str(x) for x in legs)
        badge = "✅" if ok else ("⚠️" if mode != "REVERT" else "❌")
        print(f"{badge} {path_str} | in={amt} out={out} ({mode})")

    if not all_ok:
        print("\nOVERALL: ❌ FAIL (some paths reverted or returned zero)")
        sys.exit(1)
    print("\nOVERALL: ✅ PASS")
    sys.exit(0)

if __name__ == "__main__":
    main()

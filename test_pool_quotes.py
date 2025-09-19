#!/usr/bin/env python3
# test_pool_quotes.py — sanity probe for TECBot routes (read-only)

from web3 import Web3
import sys

# -------- RPC / Contracts --------
RPC = "https://api.s0.t.hmny.io"
w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 20}))

# Harmony Quoter (Uniswap V3 style) — verified on-chain
QUOTER = Web3.to_checksum_address("0x314456E8F5efaa3dD1F036eD5900508da8A3B382")

# Minimal ABI: V1-style 'quoteExactInput(path, amountIn)' + flat 'quoteExactInputSingle(...)'
QUOTER_ABI = [
    {   # V1 path (works against Harmony QuoterV2 on-chain)
        "inputs": [
            {"internalType": "bytes", "name": "path", "type": "bytes"},
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
        ],
        "name": "quoteExactInput",
        "outputs": [{"internalType": "uint256", "name": "amountOut", "type": "uint256"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {   # Flat single fallback
        "inputs": [
            {"internalType": "address", "name": "tokenIn", "type": "address"},
            {"internalType": "address", "name": "tokenOut", "type": "address"},
            {"internalType": "uint24",  "name": "fee", "type": "uint24"},
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

# -------- Tokens (ETH-format, checksummed) --------
TOKENS = {
    "WONE":  Web3.to_checksum_address("0xcF664087a5bB0237a0BAd6742852ec6c8d69A27a"),
    "1ETH":  Web3.to_checksum_address("0x4cC435d7b9557d54d6EF02d69Bbf72634905Bf11"),
    "1USDC": Web3.to_checksum_address("0xBC594CABd205bD993e7FfA6F3e9ceA75c1110da5"),
    "TEC":   Web3.to_checksum_address("0x0DEB9A1998aAE32dAAcF6de21161c3E942aCe074"),
    "1sDAI": Web3.to_checksum_address("0xeDEb95D51dBc4116039435379Bd58472A2c09b1f"),
}

# -------- Pairs we actually use (single-hop) --------
# Fees are the *known-good* ones from your on-chain tests.
SINGLE = [
    ("1ETH",  "WONE",   3000),   # Bot #1: 1ETH <-> WONE
    ("1USDC", "1sDAI",   500),   # Bot #2/#3: stables
    ("1USDC", "WONE",    500),   # Bot #2: USDC <-> WONE (primary)
    ("TEC",   "WONE",  10000),   # Bot #4: TEC <-> WONE
    # (Optionally add ("1USDC","WONE",3000/10000) as fallbacks)
]

# -------- Multihop routes we care about --------
# 1ETH -> WONE (3000) -> 1USDC (500). (No direct 1ETH<->1USDC pool.)
MULTIHOP = [
    ("1ETH", 3000, "WONE", 500, "1USDC"),
    # Add reverse if desired:
    # ("1USDC", 500, "WONE", 3000, "1ETH"),
]

# -------- Helpers --------
ERC20_DECIMALS_ABI = [{
    "constant": True, "inputs": [], "name": "decimals",
    "outputs": [{"name": "", "type": "uint8"}],
    "payable": False, "stateMutability": "view", "type": "function",
}]

def decimals(addr: str) -> int:
    c = w3.eth.contract(address=addr, abi=ERC20_DECIMALS_ABI)
    try:
        return int(c.functions.decimals().call())
    except Exception:
        return 18  # safe default

def default_amount_in(addr: str) -> int:
    dec = decimals(addr)
    # Use small, decimals-aware canary amounts (read-only anyway)
    if dec >= 18: return 10**15       # 0.001 units
    if dec >= 6:  return 10**6        # 1.0 units
    return 100

def encode_v3_path(*legs) -> bytes:
    """legs: token, fee, token, fee, token ...  (token=0x..., fee=uint24)"""
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

def quote_single(token_in: str, token_out: str, fee: int, amount_in: int) -> tuple[str, int, str]:
    """Try path-based quoteExactInput first, then flat quoteExactInputSingle as fallback."""
    path = encode_v3_path(token_in, fee, token_out)
    # 1) V1-style path method (works on Harmony's QuoterV2)
    try:
        out = quoter.functions.quoteExactInput(path, amount_in).call()
        return ("OK", out, "path")
    except Exception as e1:
        # 2) Flat
        try:
            out = quoter.functions.quoteExactInputSingle(token_in, token_out, int(fee), int(amount_in), 0).call()
            return ("OK", out, "single")
        except Exception as e2:
            return ("REVERT", 0, f"v1:{str(e1)}; single:{str(e2)}")

def quote_multihop(legs: tuple, amount_in: int) -> tuple[str, int, str]:
    """legs: ('1ETH',3000,'WONE',500,'1USDC') -> encode and call path quoter"""
    # convert token symbols to addresses in place
    enc_legs = []
    for i, item in enumerate(legs):
        if i % 2 == 0:
            enc_legs.append(TOKENS[item])
        else:
            enc_legs.append(int(item))
    path = encode_v3_path(*enc_legs)
    try:
        out = quoter.functions.quoteExactInput(path, amount_in).call()
        return ("OK", out, "path")
    except Exception as e:
        return ("REVERT", 0, str(e))

# -------- Main --------
def main() -> int:
    print(f"RPC: {RPC}")
    print(f"Quoter: {QUOTER}\n")

    all_ok = True

    # Single-hop matrix
    print("== Single-hop ==")
    for sym_in, sym_out, fee in SINGLE:
        a_in  = TOKENS[sym_in]
        a_out = TOKENS[sym_out]
        amt   = default_amount_in(a_in)

        status, out, mode = quote_single(a_in, a_out, fee, amt)
        ok = (status == "OK" and out > 0)
        all_ok &= ok
        badge = "✅" if ok else ("⚠️" if status == "OK" else "❌")
        print(f"{badge} {sym_in}->{sym_out} fee {fee} | in={amt} out={out} ({mode})")

    # Multihop paths
    print("\n== Multihop ==")
    for legs in MULTIHOP:
        sym_in = legs[0]
        amt    = default_amount_in(TOKENS[sym_in])

        status, out, mode = quote_multihop(legs, amt)
        ok = (status == "OK" and out > 0)
        all_ok &= ok
        path_str = " | ".join(str(x) for x in legs)
        badge = "✅" if ok else ("⚠️" if status == "OK" else "❌")
        print(f"{badge} {path_str} | in={amt} out={out} ({mode})")

    if not all_ok:
        print("\nOVERALL: ❌ FAIL (some paths reverted or returned zero)")
        return 1

    print("\nOVERALL: ✅ PASS")
    return 0

if __name__ == "__main__":
    sys.exit(main())

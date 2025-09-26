# config.py — TECBot configuration

# Harmony chain ID
CHAIN_ID = 1666600000

# Token addresses (Harmony Mainnet)
TOKENS = {
    "ONE":   "0xcf664087a5bb0237a0bad6742852ec6c8d69a27a",  # WONE
    "1USDC": "0xBC594CABd205bD993e7FfA6F3e9ceA75c1110da5",  # ✅ FIXED verified address
    "1sDAI": "0x44fA8E6f47987339850636F88629646662444217",  # Harmony sDAI
    "1ETH":  "0x75c7f9e0d37e8a93d01d9af709c07c83d99d5c35",  # 1ETH
    "TEC":   "0x0000000000000000000000000000000000000000",  # <-- replace with your TEC token address
}

# Uniswap V3 Router
ROUTER_ADDR = "0x85495f44768ccbb584d9380Cc29149fDAA445F69"

# Quoter contract (Uniswap V3 style)
QUOTER_ADDR = "0x314456E8F5efaa3dD1F036eD5900508da8A3B382"

# Default RPC
HARMONY_RPC = "https://api.s0.t.hmny.io"

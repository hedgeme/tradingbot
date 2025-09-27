# config.py — TECBot configuration (Harmony Mainnet)

CHAIN_ID = 1666600000

TOKENS = {
    # Keep BOTH keys; modules may use ONE or WONE
    "ONE":   "0xcf664087a5bb0237a0bad6742852ec6c8d69a27a",
    "WONE":  "0xcf664087a5bb0237a0bad6742852ec6c8d69a27a",

    # Verified tokens
    "1USDC": "0xBC594CABd205bD993e7FfA6F3e9ceA75c1110da5",  # 6 decimals
    "1sDAI": "0xeDEb95D51dBc4116039435379Bd58472A2c09b1f",
    "1ETH":  "0x4cC435d7b9557d54d6EF02d69Bbf72634905Bf11",
    "TEC":   "0x0DEB9A1998aAE32dAAcF6de21161c3E942aCe074",
}

# Uniswap V3 Router + Quoter (Harmony)
ROUTER_ADDR = "0x85495f44768ccbb584d9380Cc29149fDAA445F69"
QUOTER_ADDR = "0x314456E8F5efaa3dD1F036eD5900508da8A3B382"

# Default RPC
HARMONY_RPC = "https://api.s0.t.hmny.io"

# Explicit decimals (helps keep displays consistent)
DECIMALS = {
    "ONE":   18,
    "WONE":  18,
    "1USDC": 6,
    "1sDAI": 18,
    "1ETH":  18,
    "TEC":   18,
}

# Wallet groups used by telegram_listener:/balances
# Replace the sample addresses with your actual EOA(s).
WALLETS = {
    # “ETH” strategy wallet (example: the RECIP you used in tests)
    "tecbot_eth":  "0x44D60e08bf66CBBBc7B96A0De6c01085Ec2e8D07",
    # If you have distinct wallets, set them here; otherwise you can
    # temporarily point them to the same address:
    "tecbot_usdc": "0x44D60e08bf66CBBBc7B96A0De6c01085Ec2e8D07",
    "tecbot_sdai": "0x44D60e08bf66CBBBc7B96A0De6c01085Ec2e8D07",
    "tecbot_tec":  "0x44D60e08bf66CBBBc7B96A0De6c01085Ec2e8D07",
}

# Optional cool-downs (printed by /cooldowns if present)
COOLDOWNS_DEFAULTS = {
    "price_refresh": 15,
    "trade_retry": 30,
    "alert_throttle": 60,
}

# /bot/app/config.py

# --- Gas & Slippage ---
GAS_CAP_GWEI = 150
DEFAULT_SLIPPAGE_BPS = 100  # 1%

# --- Network / RPC ---
RPC_URL  = "https://api.harmony.one"   # change if you use a different endpoint
CHAIN_ID = 1666600000                  # Harmony Mainnet (Shard 0)

# --- Telegram Admins ---
# Used to authorize tap-to-execute and admin setters in the Telegram UI
ADMIN_CHAT_IDS = {1539031664}  # add others if needed

# --- Wallets per strategy (public 0x EVM addresses) ---
# These match what /balances uses
WALLETS = {
    "tecbot_usdc": "0x85e0366f210a83ffa25031ba0026b640d88e883c",
    "tecbot_sdai": "0x15168e8e30b35db658E1e25417b8B3EB2Bc5f525",
    "tecbot_eth":  "0x44D60e08bf66CBBBc7B96A0De6c01085Ec2e8D07",
    "tecbot_tec":  "0x9e9E195A24562AE1ff1915D70310D255c3c26F89",
}

# --- Verified token contracts on Harmony (from verified_info.md) ---
# NOTE: 1USDC updated to the widely used canonical address on Harmony.
TOKENS = {
    "WONE":  "0xcF664087a5bB0237a0BAd6742852ec6c8d69A27a",  # Wrapped ONE
    "1ETH":  "0x4cc435d7b9557d54d6ef02d69bbf72634905bf11",
    "1USDC": "0x985458e523db3d53125813ed68c274899e9dfab4",  # <-- canonical USDC on Harmony
    "TEC":   "0x0deb9a1998aae32daacf6de21161c3e942ace074",
    "1sDAI": "0xedeb95d51dbc4116039435379bd58472a2c09b1f",
}

# --- (Optional) Token decimals for precise formatting ---
# WONE/1ETH/TEC/1sDAI typically 18; 1USDC is 6
DECIMALS = {
    "WONE":  18,
    "1ETH":  18,
    "1USDC": 6,
    "TEC":   18,
    "1sDAI": 18,
}

# --- (Optional) Default cooldowns (seconds) per strategy ---
COOLDOWNS_DEFAULTS = {
    "sdai-arb": 300,
    "eth-arb":  120,
    "tec-rebal": 0,
    "usdc-hedge": 0,
}

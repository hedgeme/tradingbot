# config.py — TECBot configuration (Harmony Mainnet)

from __future__ import annotations
import os

# -----------------------
# Chain / RPC / Contracts
# -----------------------

CHAIN_ID = 1666600000
HARMONY_RPC = "https://api.s0.t.hmny.io"

# Swap router + Quoter (V3-style)
ROUTER_ADDR = "0x85495f44768ccbb584d9380Cc29149fDAA445F69"
QUOTER_ADDR = "0x314456E8F5efaa3dD1F036eD5900508da8A3B382"

# Tokens (symbol -> address)
TOKENS = {
    "ONE":   "0xcf664087a5bb0237a0bad6742852ec6c8d69a27a",
    "WONE":  "0xcf664087a5bb0237a0bad6742852ec6c8d69a27a",
    "1USDC": "0xBC594CABd205bD993e7FfA6F3e9ceA75c1110da5",
    "1sDAI": "0xeDEb95D51dBc4116039435379Bd58472A2c09b1f",
    "1ETH":  "0x4cC435d7b9557d54d6EF02d69Bbf72634905Bf11",
    "TEC":   "0x0DEB9A1998aAE32dAAcF6de21161c3E942aCe074",
}

# Decimals (symbol -> decimals)
DECIMALS = {
    "ONE":   18,
    "WONE":  18,
    "1USDC": 6,
    "1sDAI": 18,
    "1ETH":  18,
    "TEC":   18,
}

# Verified Uniswap V3-style pools (label -> {address, fee})
POOLS_V3 = {
    "1ETH/WONE@3000":  {"address": "0xe0566c122bdbb29beb5ff2148a6a547df814a246", "fee": 3000},
    "1USDC/WONE@3000": {"address": "0x6e543b707693492a2d14d729ac10a9d03b4c9383", "fee": 3000},
    "TEC/WONE@10000":  {"address": "0xfac981a64ecedf1be8722125fe776bde2f746ff2", "fee": 10000},
    "1USDC/1sDAI@500": {"address": "0xc28f4b97aa9a983da81326f7fb4b9cf84a9703a2", "fee": 500},
    "TEC/1sDAI@10000": {"address": "0x90bfca0ee66ca53cddfc0f6ee5217b6f2acde4ee", "fee": 10000},
}

# -----------------------
# Wallets
# -----------------------

# Strategy wallets (EVM 0x addresses; bech32 kept as comments)
WALLETS = {
    # tecbot_usdc:
    # one: one1shsrvmepp2pllgjsxxaqqf4kgrvgazpu6lg9ww
    "tecbot_usdc": "0x85E0366f210A83fFA25031bA0026b640D88E883C",

    # tecbot_sdai:
    # one: one1z5tgar3skdwmvk8puf2p0w9nav4utaf94jfhjs
    "tecbot_sdai": "0x15168e8e30b35Db658E1E25417B8b3EB2bC5f525",

    # tecbot_eth:
    # one: one1gntquz9lvm9mh3aedgx7dsqsshkzarg83mjxph
    "tecbot_eth":  "0x44D60e08bf66CBBBc7B96A0De6c01085Ec2e8D07",

    # tecbot_tec:
    # one: one1n60pjk3y2c4wrlcezhtsxyxj2hpuymufgqjnd3
    "tecbot_tec":  "0x9e9E195A24562AE1ff1915D70310D255c3c26F89",
}

# -----------------------
# Cooldowns & Ops
# -----------------------

def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default

# Global default cooldowns (seconds)
COOLDOWNS_DEFAULTS = {
    "price_refresh": _env_int("TECBOT_CD_PRICE_REFRESH", 15),
    "trade_retry": _env_int("TECBOT_CD_TRADE_RETRY", 30),
    "alert_throttle": _env_int("TECBOT_CD_ALERT_THROTTLE", 60),
}

# Optional per-bot overrides, e.g.:
# COOLDOWNS_BY_BOT = {"tecbot_eth": {"trade_retry": 120}}
COOLDOWNS_BY_BOT = {}

# Optional per-route overrides, keys like "1ETH/WONE@3000"
# COOLDOWNS_BY_ROUTE = {"1ETH/WONE@3000": {"trade_retry": 180}}
COOLDOWNS_BY_ROUTE = {}

# -----------------------
# Feature Flags / Access
# -----------------------

# Enable /dryrun handler (default True; can be overridden by env)
DRYRUN_ENABLED = _env_bool("TECBOT_DRYRUN_ENABLED", True)

# Telegram admin user IDs (can be set via env "TECBOT_ADMIN_USER_IDS=123,456")
def _parse_admin_ids(raw: str) -> list[int]:
    out = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except Exception:
            pass
    return out

# Default to your known chat ID if env not provided
ADMIN_USER_IDS = _parse_admin_ids(os.getenv("TECBOT_ADMIN_USER_IDS", "")) or [1539031664]

# Optional: provide token/version via env (the service usually sets TELEGRAM_BOT_TOKEN in env)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", None)
TECBOT_VERSION = os.getenv("TECBOT_VERSION", "v0.1.0-ops")

# -----------------------
# (Optional) Slippage / Execution policy defaults
# -----------------------
# These are not enforced here; strategies/executor may import them.
SLIPPAGE_DEFAULT_BPS = _env_int("TECBOT_SLIPPAGE_DEFAULT_BPS", 30)  # 0.30% default
TX_DEADLINE_SECONDS = _env_int("TECBOT_TX_DEADLINE_SECONDS", 120)   # +2 minutes
GAS_LIMIT_CAP = _env_int("TECBOT_GAS_LIMIT_CAP", 500_000)          # safety cap

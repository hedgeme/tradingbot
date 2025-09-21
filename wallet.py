# /bot/app/wallet.py
import os
from typing import Dict, Optional
from web3 import Web3

# ------------------------------------------------------------------------------
# Environment loading
# ------------------------------------------------------------------------------
# Under systemd, the service injects env via EnvironmentFile (e.g. /etc/tecbot/tecbot.env)
# so os.environ already has what we need.
# For manual runs, if HMY_NODE/RPC_URL are missing, try BOTH files independently:
#   1) /etc/tecbot/tecbot.env  (may be unreadable for user; ignore errors)
#   2) ~/.env                   (user-owned copy)
# ------------------------------------------------------------------------------
if not os.getenv("HMY_NODE") and not os.getenv("RPC_URL"):
    try:
        from dotenv import load_dotenv  # type: ignore
        for p in ("/etc/tecbot/tecbot.env", os.path.expanduser("~/.env")):
            try:
                if os.path.isfile(p):
                    load_dotenv(p, override=False)
            except Exception as e:
                print(f"[wallet] WARNING: dotenv load skipped for {p}: {e}")
    except Exception as e:
        print(f"[wallet] WARNING: dotenv unavailable: {e}")

# ------------------------------------------------------------------------------
# Chain / RPC (Harmony mainnet chainId 1666600000)
# ------------------------------------------------------------------------------
RPC_URL = (os.getenv("HMY_NODE") or os.getenv("RPC_URL") or "").strip()
CHAIN_ID = int(os.getenv("HMY_CHAIN_ID") or 1666600000)
GAS_CAP_GWEI = int(os.getenv("GAS_CAP_GWEI") or 150)

def _mk_web3(url: str) -> Web3:
    provider = Web3.HTTPProvider(url, request_kwargs={"timeout": 20})
    return Web3(provider)

if not RPC_URL:
    print("[wallet] WARNING: HMY_NODE / RPC_URL not set. Web3 uninitialized.")
    w3 = Web3()  # type: ignore[attr-defined]
else:
    w3 = _mk_web3(RPC_URL)
    try:
        if not w3.is_connected():
            print(f"[wallet] WARNING: cannot connect to RPC: {RPC_URL}")
        else:
            try:
                rpc_chain_id = w3.eth.chain_id
                if rpc_chain_id != CHAIN_ID:
                    print(f"[wallet] WARNING: chainId mismatch: env={CHAIN_ID}, rpc={rpc_chain_id}")
            except Exception as e:
                print(f"[wallet] WARNING: chainId check failed: {e}")
    except Exception as e:
        print(f"[wallet] WARNING: Web3 connection check raised: {e}")

def get_w3() -> Web3:
    """Compatibility helper for modules importing get_w3()."""
    return w3

# ------------------------------------------------------------------------------
# Wallet addresses (0x checksum on Harmony)
# ------------------------------------------------------------------------------
ETH_ADDR  = os.getenv("WALLET_ETH_ONE_ADDR",  "")
USDC_ADDR = os.getenv("WALLET_USDC_ONE_ADDR", "")
SDAI_ADDR = os.getenv("WALLET_SDAI_ONE_ADDR", "")
TEC_ADDR  = os.getenv("WALLET_TEC_ONE_ADDR",  "")

def _norm(addr: str) -> str:
    if not addr:
        return ""
    try:
        return Web3.to_checksum_address(addr)
    except Exception:
        print(f"[wallet] WARNING: invalid or non-checksum addr: {addr}")
        return addr

WALLET_ETH  = _norm(ETH_ADDR)
WALLET_USDC = _norm(USDC_ADDR)
WALLET_SDAI = _norm(SDAI_ADDR)
WALLET_TEC  = _norm(TEC_ADDR)

# Map used by telegram_listener.py / others
WALLETS: Dict[str, str] = {
    "eth":  WALLET_ETH,
    "usdc": WALLET_USDC,
    "sdai": WALLET_SDAI,
    "tec":  WALLET_TEC,
}

# ------------------------------------------------------------------------------
# Balance helpers
# ------------------------------------------------------------------------------
def get_native_balance_wei(address: str) -> int:
    """Return native ONE balance in wei."""
    return w3.eth.get_balance(Web3.to_checksum_address(address))

# Minimal ERC-20 ABI for balanceOf
_ERC20_ABI = [{
    "constant": True,
    "inputs": [{"name": "owner", "type": "address"}],
    "name": "balanceOf",
    "outputs": [{"name": "", "type": "uint256"}],
    "payable": False,
    "stateMutability": "view",
    "type": "function",
}]

def get_erc20_balance_wei(token_addr: str, wallet_addr: str) -> int:
    """Return ERC-20 token balance (wei) for wallet."""
    token = w3.eth.contract(address=Web3.to_checksum_address(token_addr), abi=_ERC20_ABI)
    return token.functions.balanceOf(Web3.to_checksum_address(wallet_addr)).call()

# ------------------------------------------------------------------------------
# Gas helpers (Harmony commonly uses legacy gasPrice; cap it)
# ------------------------------------------------------------------------------
def gwei_to_wei(g: int) -> int:
    return int(g) * 10**9

def suggest_gas_price_wei(cap_gwei: Optional[int] = None) -> int:
    """
    Returns a legacy gasPrice in wei, capped by GAS_CAP_GWEI.
    Falls back to cap (or 50 gwei) if RPC query fails.
    """
    cap = gwei_to_wei(cap_gwei if cap_gwei is not None else GAS_CAP_GWEI)
    try:
        network = w3.eth.gas_price
        return min(network, cap) if cap > 0 else network
    except Exception:
        return cap if cap > 0 else gwei_to_wei(50)

def legacy_tx_defaults(from_addr: str) -> Dict:
    """
    Minimal legacy tx defaults for Harmony:
      - chainId (EIP-155)
      - gasPrice (legacy; capped)
      - from
    """
    return {
        "chainId": CHAIN_ID,
        "from": Web3.to_checksum_address(from_addr),
        "gasPrice": suggest_gas_price_wei(),
    }

# ------------------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------------------
__all__ = [
    "RPC_URL", "CHAIN_ID", "GAS_CAP_GWEI",
    "w3", "get_w3",
    "WALLET_ETH", "WALLET_USDC", "WALLET_SDAI", "WALLET_TEC", "WALLETS",
    "get_native_balance_wei", "get_erc20_balance_wei",
    "gwei_to_wei", "suggest_gas_price_wei", "legacy_tx_defaults",
]

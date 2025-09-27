# /bot/app/router_v3_test.py
from __future__ import annotations
import os
from typing import Dict, Optional, Tuple, List
from web3 import Web3

def _imp(m):
    try:
        return __import__(m, fromlist=['*'])
    except Exception:
        return __import__(f"app.{m}", fromlist=['*'])

config = _imp("config")
wallet = _imp("wallet")           # fallback for WALLETS and web3 if needed
router = _imp("router_v3")
from router_v3 import build_path_bytes, data_exact_input_single, data_exact_input

# --------------------------------------------------------------------
# Web3 / RPC
# --------------------------------------------------------------------
# Prefer router's web3 accessor if available; otherwise use wallet.get_w3()
w3 = getattr(router, "w3", None)
w3 = w3() if callable(w3) else w3
if w3 is None:
    w3 = wallet.get_w3()

print("RPC:", getattr(getattr(w3, "provider", None), "endpoint_uri", "(unknown)"))

# --------------------------------------------------------------------
# Token map (with WONE alias handling)
# --------------------------------------------------------------------
TOK: Dict[str, str] = {k: Web3.to_checksum_address(v) for k, v in config.TOKENS.items()}

# Ensure BOTH keys exist; many modules use either "ONE" or "WONE"
if "ONE" in TOK and "WONE" not in TOK:
    TOK["WONE"] = TOK["ONE"]
elif "WONE" in TOK and "ONE" not in TOK:
    TOK["ONE"] = TOK["WONE"]

# Resolve commonly used symbols
WONE = TOK.get("WONE") or TOK["ONE"]
ETH  = TOK["1ETH"]
USDC = TOK["1USDC"]
TEC  = TOK["TEC"]
SDAI = TOK["1sDAI"]

# --------------------------------------------------------------------
# Recipient address
# --------------------------------------------------------------------
# Try config.WALLETS (if present), then wallet.WALLETS, else any non-empty in either.
def _pick_recipient() -> str:
    candidates: List[str] = []
    for source in (getattr(config, "WALLETS", {}), getattr(wallet, "WALLETS", {})):
        if isinstance(source, dict):
            # prefer tecbot_eth then any
            if "tecbot_eth" in source and source["tecbot_eth"]:
                candidates.append(source["tecbot_eth"])
            candidates += [v for v in source.values() if v]
    for addr in candidates:
        try:
            return Web3.to_checksum_address(addr)
        except Exception:
            # maybe a ONE bech32 address
            try:
                return Web3.to_checksum_address(wallet.one_to_eth(addr))
            except Exception:
                continue
    # last resort: use any env wallet
    env_addr = os.getenv("WALLET_ETH_ONE_ADDR") or os.getenv("WALLET_TEC_ONE_ADDR") or ""
    if env_addr:
        try:
            return Web3.to_checksum_address(env_addr)
        except Exception:
            try:
                return Web3.to_checksum_address(wallet.one_to_eth(env_addr))
            except Exception:
                pass
    raise RuntimeError("No recipient wallet address found in config.WALLETS or wallet.WALLETS")

RECIP = _pick_recipient()

# Router address to estimate against
ROUTER_ADDR = getattr(router, "ROUTER_ADDR", None) or getattr(config, "ROUTER_ADDR", None)
if not ROUTER_ADDR:
    raise RuntimeError("ROUTER_ADDR not exposed by router_v3 or config")
ROUTER_ADDR = Web3.to_checksum_address(ROUTER_ADDR)

# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------
def try_estimate(data_hex: bytes, to_addr: str, sender: str):
    tx = {
        "to": Web3.to_checksum_address(to_addr),
        "from": Web3.to_checksum_address(sender),
        "data": data_hex,
        "value": 0,
    }
    try:
        gas = w3.eth.estimate_gas(tx)
        print("  estimate_gas:", gas)
    except Exception as e:
        print("  estimate_gas FAILED:", e)

# --------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------
def test_single_hop():
    print("\nSingle-hop: 1ETH -> WONE @ 3000")
    amount_in = 10**15  # 0.001 1ETH for estimation
    min_out   = 1       # placeholder; router revert protects bad quotes
    data = data_exact_input_single(ETH, WONE, 3000, RECIP, amount_in, min_out)
    print("  data len:", len(data))
    try_estimate(data, ROUTER_ADDR, RECIP)

def test_two_hop():
    print("\nTwo-hop: TEC -> WONE (10000) -> 1USDC (3000) via exactInput(path)")
    amount_in = 10**16  # 0.01 TEC (18 decimals)
    min_out   = 1
    path = build_path_bytes([(TEC, 10000, WONE), (WONE, 3000, USDC)])
    data = data_exact_input(path, RECIP, amount_in, min_out)
    print("  path bytes:", path.hex()[:40], "... len", len(path))
    try_estimate(data, ROUTER_ADDR, RECIP)

if __name__ == "__main__":
    print("Using RECIP:", RECIP)
    test_single_hop()
    test_two_hop()
    print("\nNOTE: If estimate_gas reverts, double-check fees, token addresses, and pool existence.")

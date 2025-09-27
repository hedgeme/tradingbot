# /bot/app/router_v3_test.py
from __future__ import annotations
import os
from typing import Dict, List
from web3 import Web3

import app.config as config
import app.wallet as wallet
import app.router_v3 as router
from app.router_v3 import build_path_bytes, data_exact_input_single, data_exact_input

# Web3 from router (guaranteed real provider)
w3 = router.w3()
print("RPC:", getattr(getattr(w3, "provider", None), "endpoint_uri", "(unknown)"))

# Token map (ensure ONE/WONE alias both exist) and checksummed
TOK: Dict[str, str] = {k: Web3.to_checksum_address(v) for k, v in config.TOKENS.items()}
if "ONE" in TOK and "WONE" not in TOK: TOK["WONE"] = TOK["ONE"]
if "WONE" in TOK and "ONE" not in TOK: TOK["ONE"] = TOK["WONE"]

WONE = TOK["WONE"]
ETH  = TOK["1ETH"]
USDC = TOK["1USDC"]
TEC  = TOK["TEC"]
SDAI = TOK["1sDAI"]

def _pick_recipient() -> str:
    cands: List[str] = []
    for source in (getattr(config, "WALLETS", {}), getattr(wallet, "WALLETS", {})):
        if isinstance(source, dict):
            if source.get("tecbot_eth"): cands.append(source["tecbot_eth"])
            cands += [v for v in source.values() if v]
    for addr in cands:
        try:
            return Web3.to_checksum_address(addr)
        except Exception:
            try:
                return Web3.to_checksum_address(wallet.one_to_eth(addr))
            except Exception:
                pass
    env_addr = os.getenv("WALLET_ETH_ONE_ADDR") or os.getenv("WALLET_TEC_ONE_ADDR") or ""
    if env_addr:
        try:
            return Web3.to_checksum_address(env_addr)
        except Exception:
            return Web3.to_checksum_address(wallet.one_to_eth(env_addr))
    raise RuntimeError("No recipient wallet address found")

RECIP = _pick_recipient()
ROUTER_ADDR = Web3.to_checksum_address(getattr(config, "ROUTER_ADDR"))

def try_estimate(data_hex: bytes, to_addr: str, sender: str):
    tx = {"to": Web3.to_checksum_address(to_addr), "from": Web3.to_checksum_address(sender), "data": data_hex, "value": 0}
    try:
        gas = w3.eth.estimate_gas(tx)
        print("  estimate_gas:", gas)
    except Exception as e:
        print("  estimate_gas FAILED:", e)

def test_single_hop():
    print("\nSingle-hop: 1ETH -> WONE @ 3000")
    amount_in = 10**15  # 0.001 1ETH
    min_out   = 1
    data = data_exact_input_single(ETH, WONE, 3000, RECIP, amount_in, min_out)
    print("  data len:", len(data))
    try_estimate(data, ROUTER_ADDR, RECIP)

def test_two_hop():
    print("\nTwo-hop: TEC -> WONE (10000) -> 1USDC (3000) via exactInput(path)")
    amount_in = 10**16  # 0.01 TEC
    min_out   = 1
    path = build_path_bytes([(TEC, 10000, WONE), (WONE, 3000, USDC)])
    data = data_exact_input(path, RECIP, amount_in, min_out)
    print("  path bytes:", path.hex()[:40], "... len", len(path))
    try_estimate(data, ROUTER_ADDR, RECIP)

if __name__ == "__main__":
    print("Using RECIP:", RECIP)
    test_single_hop()
    test_two_hop()

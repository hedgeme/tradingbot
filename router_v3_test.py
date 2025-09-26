# /bot/app/router_v3_test.py
from __future__ import annotations
import os, time
from web3 import Web3
def _imp(m):
    try: return __import__(m, fromlist=['*'])
    except Exception: return __import__(f"app.{m}", fromlist=['*'])

config = _imp("config")
wallet  = _imp("wallet")          # for WALLETS / get_w3 if you prefer
router  = _imp("router_v3")
from router_v3 import build_path_bytes, data_exact_input_single, data_exact_input

w3 = router.w3()
print("RPC:", w3.provider.endpoint_uri)

TOK = {k: Web3.to_checksum_address(v) for k,v in config.TOKENS.items()}
# ONE=WONE for pools, user-facing still "ONE"
WONE = TOK["WONE"]; ETH = TOK["1ETH"]; USDC = TOK["1USDC"]; TEC = TOK["TEC"]; sDAI = TOK["1sDAI"]

# Pick a recipient (your public EOA that will receive outputs)
RECIP = Web3.to_checksum_address(config.WALLETS.get("tecbot_eth") or list(config.WALLETS.values())[0])

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

def test_single_hop():
    print("\nSingle-hop: 1ETH -> WONE @ 3000")
    amount_in = 10**15  # 0.001 ETH for estimation
    min_out   = 1       # placeholder; using router’s revert protection
    data = data_exact_input_single(ETH, WONE, 3000, RECIP, amount_in, min_out)
    print("  data len:", len(data))
    try_estimate(data, router.ROUTER_ADDR, RECIP)

def test_two_hop():
    print("\nTwo-hop: TEC -> WONE (10000) -> 1USDC (3000) via exactInput(path)")
    amount_in = 10**16  # 0.01 TEC (18 decimals)
    min_out   = 1
    path = build_path_bytes([(TEC, 10000, WONE), (WONE, 3000, USDC)])
    data = data_exact_input(path, RECIP, amount_in, min_out)
    print("  path bytes:", path.hex()[:40], "... len", len(path))
    try_estimate(data, router.ROUTER_ADDR, RECIP)

if __name__ == "__main__":
    print("Using RECIP:", RECIP)
    test_single_hop()
    test_two_hop()
    print("\nNOTE: If estimate_gas reverts, double-check fees & token addresses match your VERIFIED_INFO.md.")

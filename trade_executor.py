# /bot/app/trade_executor.py
import os
import re
import json
import time
import struct
from pathlib import Path
from typing import Dict, Any, List, Tuple

from dotenv import load_dotenv
from web3 import Web3
from eth_abi import encode as abi_encode  # installed with web3 v7

from app.alert import (
    alert_trade_success,
    alert_trade_failure,
    alert_preflight_fail,  # used for readyness issues
)
from app.wallet import (
    WALLETS,
    get_w3,
    get_native_balance_wei,
    get_erc20_balance_wei,
)

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
load_dotenv("/home/tecviva/.env")

HMY_NODE      = os.getenv("HMY_NODE", "https://api.s0.t.hmny.io")
HMY_CHAIN_ID  = int(os.getenv("HMY_CHAIN_ID", "1666600000"))
GAS_CAP_GWEI  = int(os.getenv("GAS_CAP_GWEI", "150"))
APP_DIR       = Path("/bot/app")

# Known verified router (Uniswap V3 SwapRouter02 on Harmony)
ROUTER_ADDR_ETH = Web3.to_checksum_address("0x85495f44768ccbb584d9380Cc29149fDAA445F69")

# Harmony Quoter (V1 style: quoteExactInput(bytes,uint256))
QUOTER_V1_ADDR = Web3.to_checksum_address("0x314456E8F5efaa3dD1F036eD5900508da8A3B382")

# Minimal ABIs
ERC20_ABI = [
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "","type": "uint8"}], "stateMutability": "view","type": "function"},
    {"constant": True, "inputs": [{"name":"owner","type":"address"},{"name":"spender","type":"address"}], "name":"allowance", "outputs":[{"name":"","type":"uint256"}], "stateMutability":"view", "type":"function"},
    {"constant": False, "inputs": [{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}], "name":"approve", "outputs":[{"name":"","type":"bool"}], "stateMutability":"nonpayable", "type":"function"},
    {"constant": True, "inputs": [{"name":"account","type":"address"}], "name":"balanceOf", "outputs":[{"name":"","type":"uint256"}], "stateMutability":"view", "type":"function"},
]

ROUTER_EXACT_INPUT_ABI = [{
    "inputs":[
        {"internalType":"bytes","name":"path","type":"bytes"},
        {"internalType":"uint256","name":"amountIn","type":"uint256"},
        {"internalType":"uint256","name":"amountOutMinimum","type":"uint256"},
        {"internalType":"address","name":"recipient","type":"address"},
        {"internalType":"uint256","name":"deadline","type":"uint256"}],
    "name":"exactInput",
    "outputs":[{"internalType":"uint256","name":"amountOut","type":"uint256"}],
    "stateMutability":"payable","type":"function"
}]

QUOTER_V1_ABI = [{
    "inputs":[
        {"internalType":"bytes","name":"path","type":"bytes"},
        {"internalType":"uint256","name":"amountIn","type":"uint256"}],
    "name":"quoteExactInput",
    "outputs":[{"internalType":"uint256","name":"amountOut","type":"uint256"}],
    "stateMutability":"nonpayable","type":"function"
}]

# Web3 provider
w3 = Web3(Web3.HTTPProvider(HMY_NODE, request_kwargs={"timeout": 25}))

# -----------------------------------------------------------------------------
# Token address discovery (from verified_info.md with fallbacks)
# -----------------------------------------------------------------------------
VERIFIED_INFO = APP_DIR / "verified_info.md"

FALLBACK_TOKENS: Dict[str, str] = {
    # Verified ETH-format addresses (case-insensitive; we checksum on return)
    "WONE":  "0xcF664087a5bB0237a0BAd6742852ec6c8d69A27a",
    "1ETH":  "0x4cc435d7b9557d54d6ef02d69bbf72634905bf11",
    "1USDC": "0xbc594cabd205bd993e7ffa6f3e9cea75c1110da5",
    "TEC":   "0x0deb9a1998aae32daacf6de21161c3e942ace074",
    "1sDAI": "0xedeb95d51dbc4116039435379bd58472a2c09b1f",
    # Extras we saw in verified_info.md:
    "FactoryV3": "0x12d21f5d0Ab768c312E19653Bf3f89917866B8e8",
    "TickLens":  "0x2D7B3ae07fE5E1d9da7c2C79F953339D0450a017",
    "NFPM":      "0xE4E259BE9c84260FDC7C9a3629A0410b1Fb3C114",
}

def _parse_verified_addresses() -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    if not VERIFIED_INFO.exists():
        return mapping
    text = VERIFIED_INFO.read_text()

    # Parse markdown table rows like: | Asset | SYMBOL | 0x... |
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|") or "0x" not in s:
            continue
        cells = [c.strip(" `") for c in s.split("|")]
        try:
            ox_idx = next(i for i,c in enumerate(cells) if re.fullmatch(r"0x[a-fA-F0-9]{40}", c))
            sym = cells[ox_idx - 1]
            addr = Web3.to_checksum_address(cells[ox_idx])
            if re.fullmatch(r"[A-Za-z0-9]+", sym):
                mapping[sym] = addr
        except StopIteration:
            pass

    # Also capture loose lines "SYMBOL ... 0x.."
    for line in text.splitlines():
        if "0x" not in line:
            continue
        m = re.search(r"(?:^|\s)([A-Za-z0-9]{2,12})\s+(0x[a-fA-F0-9]{40})(?:\s|$)", line)
        if m:
            sym = m.group(1)
            addr = Web3.to_checksum_address(m.group(2))
            mapping.setdefault(sym, addr)

    return mapping

def get_token_address(symbol: str) -> str:
    parsed = _parse_verified_addresses()
    merged = {**FALLBACK_TOKENS, **parsed}  # file wins
    key = symbol.strip()
    if key in merged:
        return Web3.to_checksum_address(merged[key])
    raise ValueError(f"Token symbol not found: {symbol} (have {sorted(merged.keys())})")

def _find_router_address() -> str:
    # If you add routing by reading from verified_info.md later, put it here.
    return ROUTER_ADDR_ETH

# -----------------------------------------------------------------------------
# Read-only ERC-20 helpers
# -----------------------------------------------------------------------------
def _erc20(token_addr: str):
    return w3.eth.contract(address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI)

def get_decimals(token_addr: str) -> int:
    c = _erc20(token_addr)
    return int(c.functions.decimals().call())

def get_allowance(owner_eth: str, token_addr: str, spender_eth: str) -> int:
    c = _erc20(token_addr)
    return int(c.functions.allowance(
        Web3.to_checksum_address(owner_eth),
        Web3.to_checksum_address(spender_eth)
    ).call())

def get_balance(owner_eth: str, token_addr: str) -> int:
    c = _erc20(token_addr)
    return int(c.functions.balanceOf(Web3.to_checksum_address(owner_eth)).call())

# -----------------------------------------------------------------------------
# Approve-if-needed (ERC-20)
# -----------------------------------------------------------------------------
def approve_if_needed(wallet_key: str,
                      token_addr: str,
                      spender_eth: str,
                      amount_wei: int,
                      gas_limit: int = 120_000) -> Dict[str, Any]:
    """
    If allowance < amount_wei, send approve(spender, amount_wei).
    Returns info dict; sends Telegram alerts on success/failure.
    """
    acct = _get_account(wallet_key)
    owner_eth = Web3.to_checksum_address(acct.address)
    token = _erc20(token_addr)

    current = get_allowance(owner_eth, token_addr, spender_eth)
    if current >= int(amount_wei):
        return {"skipped": True, "current_allowance": str(current)}

    fn = token.functions.approve(Web3.to_checksum_address(spender_eth), int(amount_wei))
    try:
        data = fn._encode_transaction_data()
    except AttributeError:
        data = fn.encode_abi()

    tx = {
        "to": Web3.to_checksum_address(token_addr),
        "value": 0,
        "data": data,
        "chainId": HMY_CHAIN_ID,
        "nonce": w3.eth.get_transaction_count(owner_eth),
        "gasPrice": _current_gas_price_wei_capped(),
    }
    # estimate + headroom
    try:
        est = w3.eth.estimate_gas({**tx, "from": owner_eth})
        tx["gas"] = max(min(int(est * 1.5), 600_000), gas_limit)
    except Exception:
        tx["gas"] = max(gas_limit, 120_000)

    try:
        signed = acct.sign_transaction(tx)
        txh = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
    except Exception as e:
        alert_trade_failure("approve", "erc20.approve", str(e))
        raise

    # No wait needed here; router read will see allowance after inclusion
    return {"tx_hash": txh, "allowance_before": str(current)}

# -----------------------------------------------------------------------------
# V3 quoting & swapping (exactInput path-bytes)
# -----------------------------------------------------------------------------
def _v3_path_bytes(token_in: str, fee: int, token_out: str) -> bytes:
    """tokenIn(20) + fee(uint24 BE) + tokenOut(20)"""
    def _addr(a: str) -> bytes: return bytes.fromhex(Web3.to_checksum_address(a)[2:])
    def _fee(f: int) -> bytes:  return struct.pack(">I", int(f))[1:]
    return _addr(token_in) + _fee(fee) + _addr(token_out)

def quote_v3_exact_input(path_bytes: bytes, amount_in_wei: int) -> int:
    quoter = w3.eth.contract(address=QUOTER_V1_ADDR, abi=QUOTER_V1_ABI)
    return int(quoter.functions.quoteExactInput(path_bytes, int(amount_in_wei)).call())

def swap_exact_tokens_for_tokens(wallet_key: str,
                                 amount_in_wei: int,
                                 amount_out_min_wei: int,
                                 path_eth: List[str],
                                 deadline_ts: int | None = None,
                                 gas_limit: int = 900_000,
                                 v3_fee: int = 500) -> Dict[str, Any]:
    """
    Backward-compatible signature, but internally we do V3 exactInput(path bytes).
    - path_eth should be [token_in, token_out] for single-hop.
    - If deadline_ts is None, uses now + 600s.
    """
    if len(path_eth) != 2:
        raise ValueError("This MVP only supports single-hop path [token_in, token_out]")

    acct = _get_account(wallet_key)
    owner_eth = Web3.to_checksum_address(acct.address)
    token_in  = Web3.to_checksum_address(path_eth[0])
    token_out = Web3.to_checksum_address(path_eth[1])
    router    = w3.eth.contract(address=_find_router_address(), abi=ROUTER_EXACT_INPUT_ABI)

    # Build path bytes and (re)quote to set minOut if caller passed a tiny sentinel
    path_bytes = _v3_path_bytes(token_in, v3_fee, token_out)

    if amount_out_min_wei <= 1:
        try:
            quoted = quote_v3_exact_input(path_bytes, int(amount_in_wei))
        except Exception as e:
            alert_trade_failure(f"{token_in}->{token_out}", "quote", f"Quoter revert: {e}")
            raise
        if quoted <= 0:
            alert_trade_failure(f"{token_in}->{token_out}", "quote", "Quote returned 0")
            raise RuntimeError("Quote returned 0")
        # default 0.5% slippage
        amount_out_min_wei = max(1, (quoted * 995) // 1000)

    deadline = int(deadline_ts) if deadline_ts else int(time.time()) + 600

    fn = router.functions.exactInput(
        path_bytes,
        int(amount_in_wei),
        int(amount_out_min_wei),
        owner_eth,
        int(deadline),
    )
    try:
        data = fn._encode_transaction_data()
    except AttributeError:
        data = fn.encode_abi()

    tx = {
        "to": Web3.to_checksum_address(_find_router_address()),
        "value": 0,
        "data": data,
        "chainId": HMY_CHAIN_ID,
        "nonce": w3.eth.get_transaction_count(owner_eth),
        "gasPrice": _current_gas_price_wei_capped(),
    }

    # Estimate with headroom (Harmony nodes can under-estimate V3)
    try:
        est = w3.eth.estimate_gas({**tx, "from": owner_eth})
        gas = max(min(int(est * 1.5), 1_500_000), 300_000)
    except Exception:
        gas = gas_limit
    tx["gas"] = gas

    # Sign & send
    try:
        signed = acct.sign_transaction(tx)
        txh = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
    except Exception as e:
        alert_trade_failure(f"{path_eth[0]}->{path_eth[1]}", "swap", f"sign/send error: {e}")
        raise

    # Success alert (with explorer link inside alert)
    alert_trade_success(
        f"{path_eth[0]}->{path_eth[1]} (v3 exactInput {v3_fee})",
        "swap",
        str(amount_in_wei),
        str(amount_out_min_wei),
        txh
    )
    return {"tx_hash": txh, "path": path_eth, "amount_out_min": str(amount_out_min_wei)}

# -----------------------------------------------------------------------------
# Convenience single-hop V3 with internal quoting
# -----------------------------------------------------------------------------
def swap_v3_exact_input_once(wallet_key: str,
                             token_in: str,
                             token_out: str,
                             amount_in_wei: int,
                             fee: int = 500,
                             slippage_bps: int = 50,
                             deadline_s: int = 600) -> Dict[str, Any]:
    """
    Simpler API:
      - Quotes via QuoterV1
      - Sets minOut = quote * (1 - slippage_bps/10_000)
      - Calls exactInput and alerts
    """
    acct = _get_account(wallet_key)
    owner_eth = Web3.to_checksum_address(acct.address)

    path_bytes = _v3_path_bytes(token_in, fee, token_out)
    try:
        quoted = quote_v3_exact_input(path_bytes, int(amount_in_wei))
    except Exception as e:
        alert_trade_failure(f"{token_in}->{token_out}", "quote", f"Quoter revert: {e}")
        raise
    if quoted <= 0:
        alert_trade_failure(f"{token_in}->{token_out}", "quote", "Quote returned 0")
        raise RuntimeError("Quote returned 0")

    min_out = max(1, (quoted * (10_000 - int(slippage_bps))) // 10_000)
    deadline = int(time.time()) + int(deadline_s)

    router = w3.eth.contract(address=_find_router_address(), abi=ROUTER_EXACT_INPUT_ABI)
    fn = router.functions.exactInput(path_bytes, int(amount_in_wei), int(min_out), owner_eth, int(deadline))
    try:
        data = fn._encode_transaction_data()
    except AttributeError:
        data = fn.encode_abi()

    tx = {
        "to": Web3.to_checksum_address(_find_router_address()),
        "value": 0,
        "data": data,
        "chainId": HMY_CHAIN_ID,
        "nonce": w3.eth.get_transaction_count(owner_eth),
        "gasPrice": _current_gas_price_wei_capped(),
    }
    # gas estimate + headroom
    try:
        est = w3.eth.estimate_gas({**tx, "from": owner_eth})
        tx["gas"] = max(min(int(est * 1.5), 1_500_000), 300_000)
    except Exception:
        tx["gas"] = 900_000

    try:
        signed = acct.sign_transaction(tx)
        txh = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
    except Exception as e:
        alert_trade_failure(f"{token_in}->{token_out}", "swap", f"sign/send error: {e}")
        raise

    alert_trade_success(
        f"{token_in}->{token_out} (v3 exactInput {fee})",
        "swap",
        str(amount_in_wei),
        str(min_out),
        txh
    )
    return {"tx_hash": txh, "amount_out_min": str(min_out), "path": [token_in, token_out]}

# -----------------------------------------------------------------------------
# Simple self-test (no on-chain send)
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    try:
        # Smoke: ensure router reachable and token parsing works
        print("NODE:", HMY_NODE)
        print("Router:", _find_router_address())
        print("Parsed tokens:", json.dumps(_parse_verified_addresses(), indent=2))
    except Exception as e:
        alert_preflight_fail(f"trade_executor self-test failed: {e}")
        raise

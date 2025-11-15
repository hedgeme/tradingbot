# /bot/trade_executor.py
import os
import re
import json
import time
import struct
import subprocess
from pathlib import Path
from typing import Dict, Any, List

from dotenv import load_dotenv
from web3 import Web3

from app.alert import (
    alert_trade_success,
    alert_trade_failure,
    alert_preflight_fail,
)
from app.wallet import (
    WALLETS,
)

# -----------------------------------------------------------------------------
# Config / RPC
# -----------------------------------------------------------------------------
load_dotenv("/home/tecviva/.env")

HMY_NODE      = os.getenv("HMY_NODE", "https://api.s0.t.hmny.io")
HMY_CHAIN_ID  = int(os.getenv("HMY_CHAIN_ID", "1666600000"))
GAS_CAP_GWEI  = int(os.getenv("GAS_CAP_GWEI", "150"))
APP_DIR       = Path("/bot/app")

# Web3 provider
w3 = Web3(Web3.HTTPProvider(HMY_NODE, request_kwargs={"timeout": 25}))

def _current_gas_price_wei_capped() -> int:
    """Harmony mostly uses legacy gasPrice. Cap by GAS_CAP_GWEI."""
    cap = int(GAS_CAP_GWEI) * 10**9 if int(GAS_CAP_GWEI) > 0 else None
    try:
        gp = int(w3.eth.gas_price)
        return min(gp, cap) if cap else gp
    except Exception:
        # Fallback 50 gwei if RPC hiccups
        return (50 * 10**9) if cap is None else cap

# -----------------------------------------------------------------------------
# SwapRouter02 / Quoter (Uniswap V3 on Harmony)
# -----------------------------------------------------------------------------
ROUTER_ADDR_ETH = Web3.to_checksum_address("0x85495f44768ccbb584d9380Cc29149fDAA445F69")
QUOTER_V1_ADDR  = Web3.to_checksum_address("0x314456E8F5efaa3dD1F036eD5900508da8A3B382")

# SwapRouter02.exactInput(ExactInputParams)
# struct ExactInputParams {
#   bytes   path;
#   address recipient;
#   uint256 deadline;
#   uint256 amountIn;
#   uint256 amountOutMinimum;
# }
ROUTER_EXACT_INPUT_ABI = [{
    "inputs": [{
        "components": [
            {"internalType": "bytes",   "name": "path",             "type": "bytes"},
            {"internalType": "address", "name": "recipient",        "type": "address"},
            {"internalType": "uint256", "name": "deadline",         "type": "uint256"},
            {"internalType": "uint256", "name": "amountIn",         "type": "uint256"},
            {"internalType": "uint256", "name": "amountOutMinimum", "type": "uint256"},
        ],
        "internalType": "struct ISwapRouter.ExactInputParams",
        "name": "params",
        "type": "tuple",
    }],
    "name": "exactInput",
    "outputs": [
        {"internalType": "uint256", "name": "amountOut", "type": "uint256"},
    ],
    "stateMutability": "payable",
    "type": "function",
}]

# Minimal Quoter ABI (quoteExactInput)
QUOTER_V1_ABI = [{
    "inputs":[
        {"internalType":"bytes","name":"path","type":"bytes"},
        {"internalType":"uint256","name":"amountIn","type":"uint256"}],
    "name":"quoteExactInput",
    "outputs":[{"internalType":"uint256","name":"amountOut","type":"uint256"}],
    "stateMutability":"nonpayable","type":"function"
}]

# -----------------------------------------------------------------------------
# Tokens (from verified_info.md with fallbacks)
# -----------------------------------------------------------------------------
VERIFIED_INFO = APP_DIR / "verified_info.md"

FALLBACK_TOKENS: Dict[str, str] = {
    "WONE":  "0xcF664087a5bB0237a0BAd6742852ec6c8d69A27a",
    "1ETH":  "0x4cc435d7b9557d54d6ef02d69bbf72634905bf11",
    "1USDC": "0xbc594cabd205bd993e7ffa6f3e9cea75c1110da5",
    "TEC":   "0x0deb9a1998aae32daacf6de21161c3e942ace074",
    "1sDAI": "0xedeb95d51dbc4116039435379bd58472a2c09b1f",
    # useful infra addrs captured in your doc:
    "FactoryV3": "0x12d21f5d0Ab768c312E19653Bf3f89917866B8e8",
    "TickLens":  "0x2D7B3ae07fE5E1d9da7c2C79F953339D0450a017",
    "NFPM":      "0xE4E259BE9c84260FDC7C9a3629A0410b1Fb3C114",
}

def _parse_verified_addresses() -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    if not VERIFIED_INFO.exists():
        return mapping
    text = VERIFIED_INFO.read_text()

    # markdown rows like: | ... | SYMBOL | 0x... |
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|") or "0x" not in s:
            continue
        cells = [c.strip(" `") for c in s.split("|")]
        try:
            ox_idx = next(i for i, c in enumerate(cells)
                          if re.fullmatch(r"0x[a-fA-F0-9]{40}", c))
            sym = cells[ox_idx - 1]
            addr = Web3.to_checksum_address(cells[ox_idx])
            if re.fullmatch(r"[A-Za-z0-9]+", sym):
                mapping[sym] = addr
        except StopIteration:
            pass

    # loose lines "SYMBOL ... 0x.."
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
    """Resolve token symbol → checksum address using verified_info + fallbacks."""
    parsed = _parse_verified_addresses()
    merged = {**FALLBACK_TOKENS, **parsed}
    key = symbol.strip()
    if key in merged:
        return Web3.to_checksum_address(merged[key])
    raise ValueError(f"Token symbol not found: {symbol} (have {sorted(merged.keys())})")

def _normalize_token(t: str) -> str:
    """Allow either '1USDC' / 'TEC' or a raw 0x address."""
    t = t.strip()
    if t.startswith("0x") and len(t) == 42:
        return Web3.to_checksum_address(t)
    return get_token_address(t)

def _find_router_address() -> str:
    return ROUTER_ADDR_ETH

# -----------------------------------------------------------------------------
# ERC-20 minimal
# -----------------------------------------------------------------------------
ERC20_ABI = [
    {"constant": True,  "inputs": [], "name": "decimals",
     "outputs": [{"name":"","type":"uint8"}], "stateMutability":"view", "type":"function"},
    {"constant": True,  "inputs": [{"name":"owner","type":"address"},{"name":"spender","type":"address"}],
     "name":"allowance", "outputs":[{"name":"","type":"uint256"}], "stateMutability":"view", "type":"function"},
    {"constant": False, "inputs": [{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],
     "name":"approve",   "outputs":[{"name":"","type":"bool"}],     "stateMutability":"nonpayable", "type":"function"},
    {"constant": True,  "inputs": [{"name":"account","type":"address"}],
     "name":"balanceOf", "outputs":[{"name":"","type":"uint256"}], "stateMutability":"view", "type":"function"},
]

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
# GPG-based key management
# -----------------------------------------------------------------------------
_DEFAULT_SECRET_DIR = Path(os.getenv("SECRETS_DIR", "/home/tecviva/.secrets"))

_SECRET_FILE_BY_WALLET: Dict[str, Path] = {
    "tecbot_usdc": _DEFAULT_SECRET_DIR / "tecbot_usdc.pass.gpg",
    "tecbot_sdai": _DEFAULT_SECRET_DIR / "tecbot_sdai.pass.gpg",
    "tecbot_eth":  _DEFAULT_SECRET_DIR / "tecbot_eth.pass.gpg",
    "tecbot_tec":  _DEFAULT_SECRET_DIR / "tecbot_tec.pass.gpg",
}

def _secret_path_for(wallet_key: str) -> Path:
    env_name = f"SECRET_PATH_{wallet_key.upper()}"
    override = os.getenv(env_name)
    if override:
        return Path(override)
    return _SECRET_FILE_BY_WALLET.get(wallet_key, _DEFAULT_SECRET_DIR / f"{wallet_key}.pass.gpg")

def _gpg_decrypt_file(path: Path) -> str:
    """
    Decrypts a GPG file and returns the plaintext string.
    Requires gpg-agent to be unlocked (your gpgcheck helper handles this).
    """
    if not path.exists():
        raise FileNotFoundError(f"Secret file not found: {path}")
    try:
        out = subprocess.check_output(
            ["gpg", "--quiet", "--batch", "--decrypt", str(path)],
            stderr=subprocess.STDOUT,
            timeout=10,
        )
        return out.decode().strip()
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"GPG decrypt failed for {path}: {e.output.decode().strip()}") from e

def _get_account(wallet_key: str):
    """
    Returns a signer account for wallet_key using GPG-decrypted secret.
    Falls back to env PKs if present (optional).
    """
    pk = None
    # 1) Try GPG file
    try:
        pk = _gpg_decrypt_file(_secret_path_for(wallet_key))
    except Exception as e:
        # 2) Optional env PK fallback
        for k in (
            f"PRIVKEY_{wallet_key.upper()}",
            f"PK_{wallet_key.upper()}",
            f"{wallet_key.upper()}_PRIVKEY",
        ):
            v = os.getenv(k)
            if v and v.strip():
                pk = v.strip()
                break
        if pk is None:
            raise RuntimeError(f"Private key unavailable for {wallet_key}: {e}")

    if not pk:
        raise RuntimeError(f"Private key empty for {wallet_key}")

    if not pk.startswith("0x"):
        pk = "0x" + pk

    acct = w3.eth.account.from_key(pk)

    # Optional sanity vs configured address
    try:
        configured = (WALLETS.get(wallet_key) or "").strip()
        if configured:
            conf = Web3.to_checksum_address(configured)
            if conf != Web3.to_checksum_address(acct.address):
                print(f"[trade_executor] WARNING: signer {acct.address} != configured {conf} for {wallet_key}")
    except Exception:
        pass

    return acct

# -----------------------------------------------------------------------------
# V3 quoting helpers
# -----------------------------------------------------------------------------
def _v3_path_bytes(token_in: str, fee: int, token_out: str) -> bytes:
    """tokenIn(20) + fee(uint24 BE) + tokenOut(20)"""
    def _addr(a: str) -> bytes: return bytes.fromhex(Web3.to_checksum_address(a)[2:])
    def _fee(f: int) -> bytes:  return struct.pack(">I", int(f))[1:]
    return _addr(token_in) + _fee(fee) + _addr(token_out)

def quote_v3_exact_input(path_bytes: bytes, amount_in_wei: int) -> int:
    """Call Quoter.quoteExactInput(path, amountIn)."""
    quoter = w3.eth.contract(address=QUOTER_V1_ADDR, abi=QUOTER_V1_ABI)
    return int(quoter.functions.quoteExactInput(path_bytes, int(amount_in_wei)).call())

# -----------------------------------------------------------------------------
# Approve-if-needed
# -----------------------------------------------------------------------------
def approve_if_needed(wallet_key: str,
                      token_addr: str,
                      spender_eth: str,
                      amount_wei: int,
                      gas_limit: int = 120_000) -> Dict[str, Any]:
    """
    If allowance < amount_wei, send approve(spender, amount_wei).
    Returns info dict; sends Telegram alerts on failure.
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

    return {"tx_hash": txh, "allowance_before": str(current)}

# -----------------------------------------------------------------------------
# V3 swap — exactInput via bytes path (single hop MVP)
# -----------------------------------------------------------------------------
def swap_exact_tokens_for_tokens(wallet_key: str,
                                 amount_in_wei: int,
                                 amount_out_min_wei: int,
                                 path_eth: List[str],
                                 deadline_ts: int | None = None,
                                 gas_limit: int = 900_000,
                                 v3_fee: int = 500) -> Dict[str, Any]:
    """
    Single-hop exactInput swap using SwapRouter02.exactInput.

    path_eth: [token_in, token_out] as addresses or symbols.
    """
    if len(path_eth) != 2:
        raise ValueError("This MVP only supports single-hop path [token_in, token_out]")

    acct = _get_account(wallet_key)
    owner_eth = Web3.to_checksum_address(acct.address)

    token_in  = _normalize_token(path_eth[0])
    token_out = _normalize_token(path_eth[1])

    # Build bytes path for v3
    path_bytes = _v3_path_bytes(token_in, v3_fee, token_out)

    # Make sure allowance exists
    approve_if_needed(wallet_key, token_in, _find_router_address(), amount_in_wei)

    router = w3.eth.contract(address=_find_router_address(), abi=ROUTER_EXACT_INPUT_ABI)

    deadline = int(deadline_ts) if deadline_ts else int(time.time()) + 600
    params = (path_bytes, owner_eth, int(deadline), int(amount_in_wei), int(amount_out_min_wei))
    fn = router.functions.exactInput(params)
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

    try:
        signed = acct.sign_transaction(tx)
        txh = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
    except Exception as e:
        alert_trade_failure(f"{token_in}->{token_out}", "swap_exact_tokens_for_tokens", f"sign/send error: {e}")
        raise

    alert_trade_success(
        f"{token_in}->{token_out} (v3 exactInput {v3_fee})",
        "swap_exact_tokens_for_tokens",
        str(amount_in_wei),
        str(amount_out_min_wei),
        txh
    )
    return {"tx_hash": txh, "path": [token_in, token_out], "amount_out_min": str(amount_out_min_wei)}

def swap_v3_exact_input_once(wallet_key: str,
                             token_in: str,
                             token_out: str,
                             amount_in_wei: int,
                             fee: int = 500,
                             slippage_bps: int = 50,
                             deadline_s: int = 600) -> Dict[str, Any]:
    """
    High-level single-hop swap helper:

    - token_in / token_out can be symbol ("1USDC") or address.
    - Computes quote via Quoter, derives minOut with slippage_bps,
      and executes SwapRouter02.exactInput with bytes path.
    """
    acct = _get_account(wallet_key)
    owner_eth = Web3.to_checksum_address(acct.address)

    t_in  = _normalize_token(token_in)
    t_out = _normalize_token(token_out)

    # Build path and quote
    path_bytes = _v3_path_bytes(t_in, fee, t_out)
    try:
        quoted = quote_v3_exact_input(path_bytes, int(amount_in_wei))
    except Exception as e:
        alert_trade_failure(f"{t_in}->{t_out}", "quote", f"Quoter revert: {e}")
        raise
    if quoted <= 0:
        alert_trade_failure(f"{t_in}->{t_out}", "quote", "Quote returned 0")
        raise RuntimeError("Quote returned 0")

    min_out = max(1, (quoted * (10_000 - int(slippage_bps))) // 10_000)
    deadline = int(time.time()) + int(deadline_s)

    # Ensure allowance
    approve_if_needed(wallet_key, t_in, _find_router_address(), amount_in_wei)

    router = w3.eth.contract(address=_find_router_address(), abi=ROUTER_EXACT_INPUT_ABI)
    params = (path_bytes, owner_eth, int(deadline), int(amount_in_wei), int(min_out))
    fn = router.functions.exactInput(params)
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
        alert_trade_failure(f"{t_in}->{t_out}", "swap", f"sign/send error: {e}")
        raise

    alert_trade_success(
        f"{t_in}->{t_out} (v3 exactInput {fee})",
        "swap",
        str(amount_in_wei),
        str(min_out),
        txh
    )
    return {"tx_hash": txh, "amount_out_min": str(min_out), "path": [t_in, t_out]}

# -----------------------------------------------------------------------------
# Self-test (no tx send)
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    try:
        print("NODE:", HMY_NODE)
        print("Router:", _find_router_address())
        print("Parsed tokens:", json.dumps(_parse_verified_addresses(), indent=2))
    except Exception as e:
        alert_preflight_fail(f"trade_executor self-test failed: {e}")
        raise

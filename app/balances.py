# app/balances.py
from __future__ import annotations
from decimal import Decimal
from typing import Dict
from web3 import Web3

# Tolerant imports: config and get_ctx from either app.* or root
try:
    from app import config as C  # type: ignore
except Exception:
    import config as C  # type: ignore

try:
    from app.chain import get_ctx  # type: ignore
except Exception:
    from chain import get_ctx  # type: ignore

# --- constants / mini ABIs ---
_ERC20_DECIMALS_ABI = [{
    "inputs": [],
    "name": "decimals",
    "outputs": [{"internalType": "uint8", "name": "", "type": "uint8"}],
    "stateMutability": "view",
    "type": "function"
}]
_ERC20_BAL_ABI = [{
    "inputs": [{"internalType": "address", "name": "account", "type": "address"}],
    "name": "balanceOf",
    "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
    "stateMutability": "view",
    "type": "function"
}]

def _ctx():
    return get_ctx(getattr(C, "HARMONY_RPC", "https://api.s0.t.hmny.io"))

def _to_checksum(a: str) -> str:
    return Web3.to_checksum_address(a)

def _decimals_for(sym: str, token_addr: str) -> int:
    """Prefer config hint; otherwise query chain safely."""
    symU = sym.upper()
    conf = getattr(C, "DECIMALS", {})
    if conf and symU in conf:
        return int(conf[symU])
    w3 = _ctx().w3
    try:
        erc = w3.eth.contract(address=_to_checksum(token_addr), abi=_ERC20_DECIMALS_ABI)
        return int(erc.functions.decimals().call())
    except Exception:
        # Conservative default
        return 18

def native_one_balance(wallet: str) -> Decimal:
    w3 = _ctx().w3
    wei = w3.eth.get_balance(_to_checksum(wallet))
    # 1 ONE = 1e18 wei
    return (Decimal(wei) / Decimal(10 ** 18)).quantize(Decimal("0.00000001"))

def erc20_balance(sym: str, wallet: str) -> Decimal:
    symU = sym.upper()
    tokens: Dict[str, str] = {k.upper(): v for k, v in getattr(C, "TOKENS", {}).items()}
    addr = tokens.get(symU)
    if not addr:
        return Decimal(0)
    dec = _decimals_for(symU, addr)
    w3 = _ctx().w3
    try:
        erc = w3.eth.contract(address=_to_checksum(addr), abi=_ERC20_BAL_ABI)
        raw = erc.functions.balanceOf(_to_checksum(wallet)).call()
        return (Decimal(raw) / Decimal(10 ** dec)).quantize(Decimal("0.00000001"))
    except Exception:
        return Decimal(0)

def all_balances() -> Dict[str, Dict[str, Decimal]]:
    """
    Returns a dict: { wallet_name: { 'ONE(native)': Decimal, 'ONE': Decimal, <ERC20>: Decimal, ... } }
    - 'ONE(native)' is always present and equals the native ONE balance.
    - 'ONE' mirrors native ONE (so UI can drop '(native)' in the column header without breaking).
    - Skips ERC-20 'ONE' and 'WONE' to avoid duplicate/confusing columns.
    """
    out: Dict[str, Dict[str, Decimal]] = {}
    wallets: Dict[str, str] = getattr(C, "WALLETS", {})
    tokens: Dict[str, str] = {k.upper(): v for k, v in getattr(C, "TOKENS", {}).items()}

    for w_name, w_addr in wallets.items():
        row: Dict[str, Decimal] = {}
        # Native ONE
        try:
            one_native = native_one_balance(w_addr)
        except Exception:
            one_native = Decimal(0)
        # Expose as both keys for UI flexibility (you can choose which to display)
        row["ONE(native)"] = one_native
        row["ONE"] = one_native

        # ERC-20 tokens — skip ONE/WONE to avoid duplicate display
        for symU in tokens.keys():
            if symU in ("ONE", "WONE"):
                continue
            try:
                row[symU] = erc20_balance(symU, w_addr)
            except Exception:
                row[symU] = Decimal(0)

        out[w_name] = row

    return out

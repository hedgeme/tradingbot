# /bot/app/chain.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
from web3 import Web3

# ---- POA middleware: handle multiple web3 versions gracefully ----
_POA_MW = None
try:
    # web3 v6+ (including v7): new name & location
    from web3.middleware.proof_of_authority import ExtraDataToPOAMiddleware  # type: ignore
    _POA_MW = ExtraDataToPOAMiddleware
except Exception:
    try:
        # web3 v5 style
        from web3.middleware import geth_poa_middleware  # type: ignore
        _POA_MW = geth_poa_middleware
    except Exception:
        _POA_MW = None  # no POA middleware available; proceed without it

# ---- Minimal ABIs ----
ERC20_ABI = [
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name":"","type":"uint8"}], "type": "function"},
    {"constant": True, "inputs": [{"name":"owner","type":"address"}], "name": "balanceOf", "outputs": [{"name":"","type":"uint256"}], "type": "function"},
    {"constant": True, "inputs": [{"name":"owner","type":"address"},{"name":"spender","type":"address"}], "name": "allowance", "outputs": [{"name":"","type":"uint256"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name":"","type":"string"}], "type": "function"},
]

QUOTER_V2_ABI = [
    {
        "inputs": [{
            "components": [
                {"internalType":"address","name":"tokenIn","type":"address"},
                {"internalType":"address","name":"tokenOut","type":"address"},
                {"internalType":"uint24","name":"fee","type":"uint24"},
                {"internalType":"address","name":"recipient","type":"address"},
                {"internalType":"uint256","name":"amountIn","type":"uint256"},
                {"internalType":"uint160","name":"sqrtPriceLimitX96","type":"uint160"},
            ],
            "internalType":"struct IQuoterV2.QuoteExactInputSingleParams",
            "name":"params","type":"tuple"
        }],
        "name":"quoteExactInputSingle",
        "outputs":[
            {"internalType":"uint256","name":"amountOut","type":"uint256"},
            {"internalType":"uint160","name":"sqrtPriceX96After","type":"uint160"},
            {"internalType":"uint32","name":"initializedTicksCrossed","type":"uint32"},
            {"internalType":"uint256","name":"gasEstimate","type":"uint256"},
        ],
        "stateMutability":"nonpayable","type":"function"
    }
]

@dataclass
class ChainCtx:
    w3: Web3
    def erc20(self, addr: str):
        return self.w3.eth.contract(address=Web3.to_checksum_address(addr), abi=ERC20_ABI)
    def quoter(self, addr: str):
        return self.w3.eth.contract(address=Web3.to_checksum_address(addr), abi=QUOTER_V2_ABI)

_ctx: Optional[ChainCtx] = None

def get_ctx(rpc_url: str) -> ChainCtx:
    """
    Create (once) and return a Web3 context.
    Inject POA middleware when available (Harmony is EVM-compatible; POA MW is harmless if unneeded).
    """
    global _ctx
    if _ctx is None:
        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 15}))
        # Try to inject POA middleware if present for this web3 version
        try:
            if _POA_MW is not None:
                # v6/v7: class; v5: function — both are callable in middleware_onion.inject
                w3.middleware_onion.inject(_POA_MW, layer=0)
        except Exception:
            # Non-fatal; proceed without POA middleware
            pass
        _ctx = ChainCtx(w3=w3)
    return _ctx

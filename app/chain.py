# /bot/app/chain.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
from web3 import Web3

# ---- POA middleware: support web3 v5–v7 ----
_POA_MW = None
try:
    from web3.middleware.proof_of_authority import ExtraDataToPOAMiddleware  # v6/v7
    _POA_MW = ExtraDataToPOAMiddleware
except Exception:
    try:
        from web3.middleware import geth_poa_middleware  # v5
        _POA_MW = geth_poa_middleware
    except Exception:
        _POA_MW = None

# ---- Minimal ABIs ----

# ERC-20 read-only
ERC20_ABI = [
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name":"","type":"uint8"}], "type": "function"},
    {"constant": True, "inputs": [{"name":"owner","type":"address"}], "name": "balanceOf", "outputs": [{"name":"","type":"uint256"}], "type": "function"},
    {"constant": True, "inputs": [{"name":"owner","type":"address"},{"name":"spender","type":"address"}], "name": "allowance", "outputs": [{"name":"","type":"uint256"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name":"","type":"string"}], "type": "function"},
]

# ✅ Correct Uniswap QuoterV2 ABI (NO recipient field)
# quoteExactInputSingle((address tokenIn, address tokenOut, uint24 fee, uint256 amountIn, uint160 sqrtPriceLimitX96))
#   -> (uint256 amountOut, uint160 sqrtPriceX96After, uint32 initializedTicksCrossed, uint256 gasEstimate)
QUOTER_V2_ABI = [
    {
        "inputs": [{
            "components": [
                {"internalType":"address","name":"tokenIn","type":"address"},
                {"internalType":"address","name":"tokenOut","type":"address"},
                {"internalType":"uint24","name":"fee","type":"uint24"},
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
    global _ctx
    if _ctx is None:
        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 15}))
        try:
            if _POA_MW is not None:
                w3.middleware_onion.inject(_POA_MW, layer=0)
        except Exception:
            pass
        _ctx = ChainCtx(w3=w3)
    return _ctx

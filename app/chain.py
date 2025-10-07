# app/chain.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
from web3 import Web3
from web3.middleware import geth_poa_middleware  # harmless if not needed

# Minimal ABIs
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
    global _ctx
    if _ctx is None:
        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 15}))
        try:
            w3.middleware_onion.inject(geth_poa_middleware, layer=0)
        except Exception:
            pass
        _ctx = ChainCtx(w3=w3)
    return _ctx

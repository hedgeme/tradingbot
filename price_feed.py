from web3 import Web3
from app.trade_executor import get_token_address
from app.wallet import w3
from app.coinbase_client import fetch_eth_usd_price

# Minimal ERC20 ABI
ERC20_ABI = [
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name":"","type":"uint8"}], "type":"function"},
    {"constant": True, "inputs": [{"name":"owner","type":"address"}], "name": "balanceOf", "outputs": [{"name":"","type":"uint256"}], "type":"function"}
]

def get_token_price_vs_one(token_symbol, router_quotes):
    """
    Derive token price in ONE using existing LP quotes.
    router_quotes is pre-validated via Quoter.
    """
    if token_symbol == "ONE":
        return 1.0
    if token_symbol == "1ETH":
        # Compare Harmony LP vs Coinbase ETH/USD
        harmony_quote = router_quotes.get(("1ETH","WONE"), None)
        eth_usd = fetch_eth_usd_price()
        return {"harmony": harmony_quote, "coinbase": eth_usd}
    return router_quotes.get((token_symbol,"WONE"), None)

def fetch_lp_quotes():
    """
    Pull LP price data for our key assets.
    Returns dict {token: price_info}.
    """
    results = {}
    # Fake stub: replace with actual quoter call
    router_quotes = {
        ("1ETH","WONE"): 4255.20,
        ("1USDC","WONE"): 0.999,
        ("TEC","WONE"): 0.003,
        ("1sDAI","WONE"): 1.001,
    }

    for t in ["ONE","TEC","1ETH","1USDC","1sDAI"]:
        results[t] = get_token_price_vs_one(t, router_quotes)
    return results